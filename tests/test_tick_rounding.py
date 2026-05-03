from decimal import Decimal

from app.model.orderbook import ceil_to_tick, floor_to_tick


def test_tick_rounding_floor_ceil() -> None:
    price = Decimal("0.123")
    tick = Decimal("0.01")
    assert floor_to_tick(price, tick) == Decimal("0.12")
    assert ceil_to_tick(price, tick) == Decimal("0.13")


def test_tick_rounding_on_tick() -> None:
    price = Decimal("0.50")
    tick = Decimal("0.01")
    assert floor_to_tick(price, tick) == Decimal("0.50")
    assert ceil_to_tick(price, tick) == Decimal("0.50")
