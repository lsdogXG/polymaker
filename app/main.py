from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from datetime import datetime, timezone
from decimal import Decimal

from app.clients.clob import ClobClientWrapper
from app.clients.gamma import init_gamma, poll_new_markets
from app.clients.price_feed import init_price_feed
from app.clients.ws_market import MarketWS
from app.clients.ws_user import UserWS
from app.config import load_settings, validate_required
from app.db.mongo import connect_mongo, ensure_indexes
from app.db.repo import Repo
from app.execution.executor import ExecutionEngine
from app.execution.risk import RiskManager
from app.log_config import setup_logging
from app.model.market import MarketMeta
from app.model.intent import LegIntent, TradeIntent
from app.monitor import RuntimeStats
from app.strategy.context import set_settings
from app.strategy.fullset import evaluate_fullset
from app.strategy.single_leg import evaluate_single_leg
from app.strategy.enhanced import EnhancedRiskControls

# Dashboard API imports
from app.api import get_bridge, run_api_server

logger = logging.getLogger(__name__)


class Coordinator:
    def __init__(
        self,
        settings,
        stats: RuntimeStats,
        repo: Repo,
        market_ws: MarketWS,
        user_ws: UserWS,
        executor: ExecutionEngine,
        risk: RiskManager,
        clob,
        dashboard_bridge=None,
    ) -> None:
        self.settings = settings
        self.stats = stats
        self.repo = repo
        self.market_ws = market_ws
        self.user_ws = user_ws
        self.executor = executor
        self.risk = risk
        self.clob = clob
        self.dashboard_bridge = dashboard_bridge
        self.markets: dict[str, MarketMeta] = {}
        self.condition_tokens: dict[str, tuple[str, str]] = {}
        self.token_to_condition: dict[str, str] = {}
        self.last_stale_log: dict[str, float] = {}
        # Position tracking for dashboard
        self._position_up_qty = Decimal("0")
        self._position_up_cost = Decimal("0")
        self._position_down_qty = Decimal("0")
        self._position_down_cost = Decimal("0")

        # Enhanced risk controls (from poly-maker / spike-bot)
        self.enhanced = EnhancedRiskControls()
        self.enhanced.configure(
            min_merge_size=settings.min_merge_size,
            max_3h_volatility=settings.max_volatility_pct,
            max_spread_pct=settings.max_spread_pct,
            stop_loss_pct=settings.stop_loss_pct,
            cooldown_sec=settings.trade_cooldown_sec,
            max_hold_sec=settings.max_hold_sec,
        )

    def _extract_asset_from_slug(self, slug: str) -> str:
        """Extract asset symbol from market slug.

        Examples:
            btc-updown-15m-1735963200 -> BTC
            eth-updown-15m-1735963200 -> ETH
            sol-updown-15m-1735963200 -> SOL
            xrp-updown-15m-1735963200 -> XRP
        """
        slug_lower = slug.lower()
        if slug_lower.startswith("btc"):
            return "BTC"
        elif slug_lower.startswith("eth"):
            return "ETH"
        elif slug_lower.startswith("sol"):
            return "SOL"
        elif slug_lower.startswith("xrp"):
            return "XRP"
        # Fallback: try to extract first part
        parts = slug.split("-")
        if parts:
            return parts[0].upper()
        return "BTC"  # Default to BTC

    async def add_market(self, market: MarketMeta, discovered_via: str) -> None:
        self.markets[market.condition_id] = market
        await self.repo.upsert_market(market, discovered_via)
        if not market.is_active():
            return
        logger.info(
            "market discovered: condition=%s slug=%s outcomes=%s via=%s",
            market.condition_id,
            market.slug,
            "/".join(market.outcomes),
            discovered_via,
        )
        token_a = market.token_ids[market.outcomes[0]]
        token_b = market.token_ids[market.outcomes[1]]
        self.condition_tokens[market.condition_id] = (token_a, token_b)
        self.token_to_condition[token_a] = market.condition_id
        self.token_to_condition[token_b] = market.condition_id
        self._refresh_subscriptions()

    def _refresh_subscriptions(self) -> None:
        token_ids = {token for pair in self.condition_tokens.values() for token in pair}
        condition_ids = set(self.condition_tokens.keys())
        self.market_ws.set_subscriptions(token_ids)
        self.user_ws.set_subscriptions(condition_ids)
        logger.info(
            "subscriptions updated: markets=%d conditions=%d tokens=%d",
            len(self.markets),
            len(condition_ids),
            len(token_ids),
        )

    def cleanup_expired_markets(self) -> None:
        """Remove markets that are no longer in the current 15-min window."""
        now = time.time()
        current_window_start = int(now // 900) * 900

        # Supported asset prefixes
        asset_prefixes = ["btc-updown-15m-", "eth-updown-15m-", "sol-updown-15m-", "xrp-updown-15m-"]

        expired_conditions = []
        for condition_id, market in list(self.markets.items()):
            slug = market.slug.lower()
            # Check if it's a 15-min market for any supported asset
            for prefix in asset_prefixes:
                if slug.startswith(prefix):
                    try:
                        slug_timestamp = int(slug.split("-")[-1])
                        if slug_timestamp != current_window_start:
                            expired_conditions.append(condition_id)
                    except ValueError:
                        pass
                    break

        for condition_id in expired_conditions:
            market = self.markets.pop(condition_id, None)
            if market:
                logger.info("Removed expired market: %s", market.slug)
                # Clean up token mappings
                tokens = self.condition_tokens.pop(condition_id, ())
                for token in tokens:
                    self.token_to_condition.pop(token, None)

                # Reset market data in dashboard (clear old data)
                if self.dashboard_bridge:
                    asset = self._extract_asset_from_slug(market.slug)
                    if asset in self.dashboard_bridge.state.markets:
                        # Reset to empty state
                        self.dashboard_bridge.state.markets[asset] = self.dashboard_bridge.state._empty_market_data()
                    # Clear orderbook
                    if asset in self.dashboard_bridge.state.orderbooks:
                        self.dashboard_bridge.state.orderbooks[asset] = {
                            "yes_bids": [], "yes_asks": [], "no_bids": [], "no_asks": []
                        }

        if expired_conditions:
            self._refresh_subscriptions()

    def is_round_ending_soon(self, buffer_sec: float = 30.0) -> bool:
        """Check if current 15-min round is ending soon (within buffer_sec)."""
        now = time.time()
        current_window_end = (int(now // 900) + 1) * 900
        remaining = current_window_end - now
        return remaining <= buffer_sec

    def get_round_remaining_sec(self) -> float:
        """Get seconds remaining in current 15-min round."""
        now = time.time()
        current_window_end = (int(now // 900) + 1) * 900
        return current_window_end - now

    async def on_book_update(self, token_id: str) -> None:
        condition_id = self.token_to_condition.get(token_id)
        if not condition_id:
            return
        market = self.markets.get(condition_id)
        if not market or not market.is_active():
            return
        token_a, token_b = self.condition_tokens[condition_id]
        ob_a = self.market_ws.get_orderbook(token_a)
        ob_b = self.market_ws.get_orderbook(token_b)
        if not ob_a or not ob_b:
            return
        self.stats.mark_book_update()

        # Round ending lock: skip trading in last 30 seconds of round
        remaining_sec = self.get_round_remaining_sec()
        round_ending = remaining_sec <= 30.0
        if round_ending:
            # Still update dashboard but don't trade
            if self.dashboard_bridge:
                asset = self._extract_asset_from_slug(market.slug)
                # Add round_locked status
                if asset in self.dashboard_bridge.state.markets:
                    self.dashboard_bridge.state.markets[asset]["round_locked"] = True
                    self.dashboard_bridge.state.markets[asset]["remaining_sec"] = remaining_sec

        # Update dashboard with orderbook data
        if self.dashboard_bridge:
            up_ask = ob_a.best_ask()
            up_bid = ob_a.best_bid()
            down_ask = ob_b.best_ask()
            down_bid = ob_b.best_bid()

            # Extract asset from slug (e.g., btc-updown-15m-xxx -> BTC)
            asset = self._extract_asset_from_slug(market.slug)

            self.dashboard_bridge.update_orderbook(
                up_best_ask=up_ask.price if up_ask else None,
                up_best_bid=up_bid.price if up_bid else None,
                down_best_ask=down_ask.price if down_ask else None,
                down_best_bid=down_bid.price if down_bid else None,
                asset=asset,
                slug=market.slug,
                condition_id=condition_id,
                ob_yes=ob_a,  # Pass full orderbook for depth analysis
                ob_no=ob_b,
            )
        if self.risk.is_book_stale(ob_a, ob_b):
            now_ts = datetime.now(timezone.utc).timestamp()
            last = self.last_stale_log.get(condition_id, 0)
            if now_ts - last > 10:
                await self.repo.log_audit(
                    "stale_book",
                    {"condition_id": condition_id, "token_ids": [token_a, token_b]},
                )
                self.last_stale_log[condition_id] = now_ts
            return

        # Strict freshness check: both orderbooks must be updated within max_book_age_ms
        # This ensures we only trade on fresh data (default 200ms)
        max_age = self.settings.max_book_age_ms
        age_a = ob_a.age_ms()
        age_b = ob_b.age_ms()
        if age_a > max_age or age_b > max_age:
            logger.debug(
                "Orderbook too old for trading: %s age=%dms, %s age=%dms (max=%dms)",
                token_a, age_a, token_b, age_b, max_age
            )
            return

        # Skip trading if round is ending soon (last 30 seconds)
        if round_ending:
            logger.debug("Round ending in %.1fs, skipping trade evaluation", remaining_sec)
            return

        if self.executor.has_active_cycle(condition_id):
            return

        # Enhanced: Record prices for volatility tracking
        best_ask_a = ob_a.best_ask()
        best_bid_a = ob_a.best_bid()
        if best_ask_a and self.settings.enable_volatility_filter:
            self.enhanced.volatility_filter.record_price(token_a, best_ask_a.price)
            self.enhanced.volatility_filter.record_price(token_b, ob_b.best_ask().price if ob_b.best_ask() else Decimal("0"))

        # Enhanced: Pre-trade volatility/cooldown check
        if self.settings.enable_volatility_filter:
            can_trade, reason = self.enhanced.pre_trade_check(
                condition_id,
                token_a,
                best_bid_a.price if best_bid_a else None,
                best_ask_a.price if best_ask_a else None,
            )
            if not can_trade:
                logger.debug("Trade blocked: %s", reason)
                return

        # Calculate combined price for signal tracking
        up_ask_price = ob_a.best_ask().price if ob_a.best_ask() else Decimal("0")
        down_ask_price = ob_b.best_ask().price if ob_b.best_ask() else Decimal("0")
        combined = up_ask_price + down_ask_price
        spread = Decimal("1") - combined

        intent = evaluate_fullset(market, ob_a, ob_b)
        if intent:
            intent = self._apply_chunking(intent)
            self.stats.mark_intent(
                {
                    "mode": intent.mode,
                    "condition_id": intent.condition_id,
                    "size": float(intent.target_shares),
                    "limits": {k: float(v) for k, v in intent.limits.items()},
                    "edge": float(intent.expected_edge),
                },
                intent.mode,
            )
            logger.info(
                "intent fullset: condition=%s size=%s limits=%s edge=%s",
                intent.condition_id,
                intent.target_shares,
                {k: float(v) for k, v in intent.limits.items()},
                float(intent.expected_edge),
            )

            # Add signal to dashboard
            if self.dashboard_bridge:
                asset = self._extract_asset_from_slug(market.slug)
                self.dashboard_bridge.add_signal(
                    asset=asset,
                    signal_type="FULLSET",
                    combined=combined,
                    spread=spread,
                    reason=f"Size: {intent.target_shares}, Edge: {intent.expected_edge:.4f}",
                )

            await self.executor.execute_intent(intent)
            return

        intent = evaluate_single_leg(market, ob_a, ob_b, datetime.now(timezone.utc))
        if intent:
            self.stats.mark_intent(
                {
                    "mode": intent.mode,
                    "condition_id": intent.condition_id,
                    "size": float(intent.target_shares),
                    "limits": {k: float(v) for k, v in intent.limits.items()},
                    "edge": float(intent.expected_edge),
                },
                intent.mode,
            )
            logger.info(
                "intent single_leg: condition=%s size=%s limits=%s edge=%s",
                intent.condition_id,
                intent.target_shares,
                {k: float(v) for k, v in intent.limits.items()},
                float(intent.expected_edge),
            )

            # Add signal to dashboard
            if self.dashboard_bridge:
                asset = self._extract_asset_from_slug(market.slug)
                self.dashboard_bridge.add_signal(
                    asset=asset,
                    signal_type="SINGLE_LEG",
                    combined=combined,
                    spread=spread,
                    reason=f"Size: {intent.target_shares}, Edge: {intent.expected_edge:.4f}",
                )

            await self.executor.execute_intent(intent)

    def _apply_chunking(self, intent: TradeIntent) -> TradeIntent:
        if intent.mode != "FULLSET":
            return intent
        chunk_max = Decimal(str(self.settings.chunk_max_shares))
        if intent.target_shares <= chunk_max:
            return intent
        new_legs = tuple(
            LegIntent(
                token_id=leg.token_id,
                outcome=leg.outcome,
                side=leg.side,
                price=leg.price,
                size=chunk_max,
                order_type=leg.order_type,
                tif_seconds=leg.tif_seconds,
            )
            for leg in intent.legs
        )
        return TradeIntent(
            mode=intent.mode,
            condition_id=intent.condition_id,
            slug=intent.slug,
            target_shares=chunk_max,
            limits=intent.limits,
            expected_edge=intent.expected_edge,
            order_type=intent.order_type,
            legs=new_legs,
            created_at=intent.created_at,
        )


async def gamma_poll_loop(coordinator: Coordinator, stats: RuntimeStats, interval_sec: float) -> None:
    while True:
        try:
            # Clean up expired markets first
            coordinator.cleanup_expired_markets()

            markets = await poll_new_markets()
            new_count = 0
            for market in markets:
                if market.condition_id not in coordinator.markets:
                    await coordinator.add_market(market, "gamma_poll")
                    new_count += 1
            stats.mark_gamma_poll(new_count)
            if new_count:
                logger.info("gamma poll discovered %d new markets", new_count)
        except Exception as exc:
            logger.warning("gamma poll failed: %s", exc)
        await asyncio.sleep(interval_sec)


async def load_active_markets(repo: Repo, coordinator: Coordinator) -> None:
    """Load only markets that are still live (within current 15-min window)."""
    docs = await repo.list_active_markets()
    now = time.time()
    current_window_start = int(now // 900) * 900

    # Supported asset prefixes
    asset_prefixes = ["btc-updown-15m-", "eth-updown-15m-", "sol-updown-15m-", "xrp-updown-15m-"]

    for doc in docs:
        slug = doc.get("slug", "").lower()

        # Check if it's a 15-min market for any supported asset
        is_15m_market = False
        for prefix in asset_prefixes:
            if slug.startswith(prefix):
                is_15m_market = True
                try:
                    slug_timestamp = int(slug.split("-")[-1])
                    # Only recover if it's the current window
                    if slug_timestamp != current_window_start:
                        logger.debug("Skipping old market from recovery: %s", slug)
                        is_15m_market = False
                except ValueError:
                    is_15m_market = False
                break

        if not is_15m_market:
            continue

        market = MarketMeta(
            condition_id=doc["condition_id"],
            slug=doc.get("slug", ""),  # Use original case
            question=doc.get("question", ""),
            created_at=datetime.fromisoformat(doc["created_at"]),
            end_at=datetime.fromisoformat(doc["end_at"]) if doc.get("end_at") else None,
            outcomes=tuple(doc.get("outcomes", [])),
            token_ids=doc.get("token_ids", {}),
            status=doc.get("status", "ACTIVE"),
            discovered_via=tuple(doc.get("discovered_via", [])),
            order_min_size=doc.get("order_min_size"),
        )
        await coordinator.add_market(market, "recovery")


async def recover_open_cycles(repo: Repo, executor: ExecutionEngine) -> None:
    # First, close stale SUBMITTED/CREATED cycles directly in DB
    # FOK orders either fill immediately or don't - old SUBMITTED cycles are dead
    db_cleaned = await repo.close_stale_submitted_cycles(max_age_sec=60.0)
    if db_cleaned:
        logger.info("Closed %d stale SUBMITTED/CREATED cycles in database", db_cleaned)

    # Now recover only truly open cycles (PARTIAL, HEDGED, or very recent SUBMITTED)
    cycles = await repo.find_open_cycles()
    for doc in cycles:
        executor.recover_cycle(doc)
        await repo.log_audit("recovered_cycle", {"cycle_id": doc.get("cycle_id")})

    # Clean up any in-memory cycles that are stale
    mem_cleaned = await executor.cleanup_stale_cycles(max_age_sec=60.0)
    if mem_cleaned:
        logger.info("Cleaned up %d stale in-memory cycles", mem_cleaned)


async def main_async() -> None:
    settings = load_settings()
    setup_logging(log_dir=settings.log_dir)
    validate_required(settings)
    set_settings(settings)
    logger.info(
        "signature_type map: 0=EOA 1=POLY_PROXY 2=GNOSIS_SAFE; funder=%s",
        settings.funder_address,
    )

    db = await connect_mongo(settings.mongodb_uri, settings.mongodb_db)
    await ensure_indexes(db)
    repo = Repo(db)

    clob = ClobClientWrapper(settings)
    clob.init_api_creds()
    clob.check_allowances()

    api_creds = clob.get_api_creds()
    if not api_creds:
        raise RuntimeError("Missing API creds for user WS")

    init_gamma(settings)

    market_ws = MarketWS()
    user_ws = UserWS(
        api_key=api_creds["apiKey"],
        api_secret=api_creds["apiSecret"],
        api_passphrase=api_creds["apiPassphrase"],
    )

    stats = RuntimeStats()
    risk = RiskManager(settings)
    executor = ExecutionEngine(settings, repo, clob, risk, market_ws.get_orderbook)

    # Initialize price feed for real-time crypto prices
    price_feed = await init_price_feed()
    logger.info("Price feed initialized")

    # Initialize dashboard bridge
    dashboard_bridge = get_bridge()
    dashboard_bridge.set_wallet(settings.funder_address)
    dashboard_bridge.set_repo(repo)  # Connect to MongoDB
    dashboard_bridge.set_clob(clob)  # Connect to CLOB client for balance
    dashboard_bridge.state.set_price_feed(price_feed)  # Connect price feed
    dashboard_bridge.start()

    coordinator = Coordinator(settings, stats, repo, market_ws, user_ws, executor, risk, clob, dashboard_bridge)

    await load_active_markets(repo, coordinator)
    await recover_open_cycles(repo, executor)

    async def _on_book_update(token_id: str, _ob: object) -> None:
        await coordinator.on_book_update(token_id)

    async def _on_order_event(payload: dict) -> None:
        stats.mark_order_event()
        await executor.handle_order_event(payload)

    async def _on_trade_event(payload: dict) -> None:
        stats.mark_trade_event()
        await executor.handle_trade_event(payload)

        # Push transaction to dashboard
        if dashboard_bridge:
            side = payload.get("side", "")
            price = payload.get("price")
            size = payload.get("size")
            tx_hash = payload.get("transactionHash") or payload.get("tx_hash") or ""
            if price and size:
                dashboard_bridge.add_transaction(
                    timestamp=datetime.now(timezone.utc),
                    side="UP" if side.upper() == "BUY" else "DOWN",
                    price=Decimal(str(price)),
                    size=Decimal(str(size)),
                    tx_hash=tx_hash,
                )

    async def _on_new_market(payload: dict) -> None:
        await repo.log_audit("ws_new_market", {"payload": payload})
        try:
            markets = await poll_new_markets()
            for market in markets:
                if market.condition_id not in coordinator.markets:
                    await coordinator.add_market(market, "ws_new_market")
        except Exception as exc:
            logger.warning("ws new_market poll failed: %s", exc)

    market_ws.set_new_market_handler(lambda payload: asyncio.create_task(_on_new_market(payload)))

    async def status_reporter() -> None:
        prev = stats.snapshot()
        while True:
            await asyncio.sleep(settings.status_log_interval_sec)
            snap = stats.snapshot()
            now = time.time()
            age_gamma = now - snap["last_gamma_poll_at"] if snap["last_gamma_poll_at"] else None
            age_market = now - snap["last_market_event_at"] if snap["last_market_event_at"] else None
            age_user = now - snap["last_user_event_at"] if snap["last_user_event_at"] else None
            logger.info(
                "status: markets=%d conditions=%d tokens=%d cycles=%d book_updates=%d (+%d) "
                "orders=%d (+%d) trades=%d (+%d) intents=%d/%d last_gamma=%.1fs last_market=%.1fs last_user=%.1fs",
                len(coordinator.markets),
                len(coordinator.condition_tokens),
                len({t for pair in coordinator.condition_tokens.values() for t in pair}),
                len(executor.cycles),
                snap["book_updates"],
                snap["book_updates"] - prev["book_updates"],
                snap["order_events"],
                snap["order_events"] - prev["order_events"],
                snap["trade_events"],
                snap["trade_events"] - prev["trade_events"],
                snap["intents_fullset"],
                snap["intents_single_leg"],
                age_gamma or -1.0,
                age_market or -1.0,
                age_user or -1.0,
            )

            # Log risk status
            risk_status = risk.get_risk_status()
            cb = risk_status["circuit_breaker"]
            logger.info(
                "risk: spend=%.2f/%.2f (%.1f%%) daily_pnl=%.2f loss=%.2f/%s cb=%s failures=%d",
                risk_status["total_spend"],
                risk_status["max_total"],
                risk_status["utilization"],
                cb["daily_pnl"],
                cb["daily_loss"],
                cb["max_daily_loss"],
                "TRIPPED" if cb["is_tripped"] else "OK",
                cb["consecutive_failures"],
            )
            prev = snap

            # Update dashboard with stats
            dashboard_bridge.update_from_stats(snap)
            dashboard_bridge.update_from_coordinator(
                coordinator.markets,
                coordinator.condition_tokens,
                executor.cycles,
            )

    async def cycle_cleanup_task() -> None:
        """Periodically clean up stuck cycles."""
        while True:
            await asyncio.sleep(30)  # Check every 30 seconds
            try:
                cleaned = await executor.cleanup_stale_cycles(max_age_sec=60.0)
                if cleaned:
                    logger.info("Periodic cleanup: removed %d stale cycles", cleaned)
            except Exception as exc:
                logger.warning("Cycle cleanup failed: %s", exc)

    async def position_merge_task() -> None:
        """Periodically check and merge opposing positions (from poly-maker)."""
        if not settings.enable_position_merge:
            logger.info("Position merging disabled")
            return

        while True:
            await asyncio.sleep(60)  # Check every minute
            try:
                total_freed = Decimal("0")
                for condition_id, (token_a, token_b) in coordinator.condition_tokens.items():
                    market = coordinator.markets.get(condition_id)
                    neg_risk = False  # Default, could be from market config
                    freed = await coordinator.enhanced.position_merger.check_and_merge(
                        clob, condition_id, token_a, token_b, neg_risk
                    )
                    total_freed += freed

                if total_freed > 0:
                    logger.info("Position merge freed $%.2f total", float(total_freed))
            except Exception as exc:
                logger.warning("Position merge task failed: %s", exc)

    # Get API port from environment or use default
    api_port = int(os.getenv("DASHBOARD_PORT", "8080"))
    logger.info("Starting dashboard API server on port %d", api_port)

    # Graceful shutdown handling
    shutdown_event = asyncio.Event()

    def signal_handler(sig: int) -> None:
        logger.info("Received signal %s, initiating shutdown...", signal.Signals(sig).name)
        shutdown_event.set()

    # Register signal handlers (Unix-style, skip on Windows if not supported)
    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda s=sig: signal_handler(s))
    except NotImplementedError:
        # Windows doesn't support add_signal_handler
        pass

    tasks = [
        asyncio.create_task(market_ws.run(lambda tid, ob: asyncio.create_task(_on_book_update(tid, ob))), name="market_ws"),
        asyncio.create_task(user_ws.run(lambda p: asyncio.create_task(_on_order_event(p)), lambda p: asyncio.create_task(_on_trade_event(p))), name="user_ws"),
        asyncio.create_task(gamma_poll_loop(coordinator, stats, settings.gamma_poll_interval_sec), name="gamma_poll"),
        asyncio.create_task(status_reporter(), name="status_reporter"),
        asyncio.create_task(cycle_cleanup_task(), name="cycle_cleanup"),
        asyncio.create_task(position_merge_task(), name="position_merge"),
        asyncio.create_task(run_api_server(host="0.0.0.0", port=api_port), name="api_server"),
    ]

    # Wait for shutdown signal or task completion
    async def wait_for_shutdown() -> None:
        await shutdown_event.wait()
        logger.info("Shutdown initiated, cancelling tasks...")
        for task in tasks:
            task.cancel()

    shutdown_task = asyncio.create_task(wait_for_shutdown(), name="shutdown_watcher")
    all_tasks = tasks + [shutdown_task]

    try:
        done, pending = await asyncio.wait(all_tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        # Wait for cancellation to complete
        if pending:
            await asyncio.wait(pending, timeout=5.0)
    except asyncio.CancelledError:
        pass

    # Log final risk status
    risk_status = risk.get_risk_status()
    logger.info(
        "Shutdown complete. Final risk status: daily_pnl=%.2f daily_loss=%.2f",
        risk_status["circuit_breaker"]["daily_pnl"],
        risk_status["circuit_breaker"]["daily_loss"],
    )


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logging.info("Interrupted by user")


if __name__ == "__main__":
    main()
