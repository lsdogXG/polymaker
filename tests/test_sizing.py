from decimal import Decimal

from app.model.orderbook import BookLevel, Orderbook
from app.strategy.sizing import compute_optimal_size


def test_compute_optimal_size_ladder() -> None:
    ob_yes = Orderbook(token_id="yes", asks=[BookLevel(Decimal("0.45"), Decimal("100"))])
    ob_no = Orderbook(token_id="no", asks=[BookLevel(Decimal("0.45"), Decimal("100"))])

    result = compute_optimal_size(
        ob_yes=ob_yes,
        ob_no=ob_no,
        min_shares=Decimal("10"),
        entry_buffer=Decimal("0.05"),
        fee_buffer=Decimal("0.00"),
        max_usdc_per_trade=Decimal("1000"),
        max_usdc_per_market=Decimal("1000"),
        max_total_usdc=Decimal("1000"),
    )

    assert result is not None
    assert result.size == Decimal("80")
    assert result.fullset_cost == Decimal("0.90")
