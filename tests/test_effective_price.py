from decimal import Decimal

from app.model.orderbook import effective_buy_no, effective_buy_yes


def test_effective_buy_yes() -> None:
    yes_ask = Decimal("0.45")
    no_bid = Decimal("0.60")
    assert effective_buy_yes(yes_ask, no_bid) == Decimal("0.40")


def test_effective_buy_no() -> None:
    no_ask = Decimal("0.55")
    yes_bid = Decimal("0.52")
    assert effective_buy_no(no_ask, yes_bid) == Decimal("0.48")
