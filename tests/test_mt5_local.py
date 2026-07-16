import sys
import types
from types import SimpleNamespace

import pytest

# The MetaTrader5 package only installs on Windows; stub it so the request
# building logic is testable everywhere.
try:
    import MetaTrader5  # noqa: F401
except ImportError:
    mt5_stub = types.ModuleType("MetaTrader5")
    mt5_stub.TRADE_ACTION_DEAL = "DEAL"
    mt5_stub.TRADE_ACTION_PENDING = "PENDING"
    mt5_stub.ORDER_TYPE_BUY = "BUY"
    mt5_stub.ORDER_TYPE_SELL = "SELL"
    mt5_stub.ORDER_TYPE_BUY_LIMIT = "BUY_LIMIT"
    mt5_stub.ORDER_TYPE_SELL_LIMIT = "SELL_LIMIT"
    mt5_stub.ORDER_TIME_GTC = "GTC"
    mt5_stub.ORDER_FILLING_IOC = "IOC"
    mt5_stub.ORDER_FILLING_FOK = "FOK"
    mt5_stub.ORDER_FILLING_RETURN = "RETURN"
    mt5_stub.TRADE_RETCODE_DONE = 10009
    mt5_stub.TRADE_RETCODE_INVALID_FILL = 10030
    mt5_stub.last_error = lambda: (0, "ok")
    sys.modules["MetaTrader5"] = mt5_stub

import bot.broker.mt5_local_client as mt5_client_mod
from bot.broker.mt5_local_client import Mt5LocalExecutionClient
from bot.models import Direction, TradeSignal

mt5 = sys.modules["MetaTrader5"]


def make_signal(direction=Direction.SELL, **overrides):
    defaults = dict(
        symbol="XAUUSDm",
        direction=direction,
        entry_low=4014.0,
        entry_high=4018.0,
        stop_loss=4023.0,
        take_profits=[4009.0],
        raw_text="test",
    )
    defaults.update(overrides)
    return TradeSignal(**defaults)


@pytest.fixture
def sent_orders(monkeypatch):
    sent = []
    monkeypatch.setattr(mt5, "symbol_select", lambda s, e=True: True, raising=False)
    monkeypatch.setattr(
        mt5, "symbol_info_tick",
        lambda s: SimpleNamespace(bid=4016.0, ask=4016.3), raising=False,
    )

    def order_send(request):
        sent.append(request)
        return SimpleNamespace(retcode=mt5.TRADE_RETCODE_DONE, order=42, deal=0, comment="ok")

    monkeypatch.setattr(mt5, "order_send", order_send, raising=False)
    return sent


@pytest.mark.asyncio
async def test_market_sell_uses_bid_and_deal_action(sent_orders):
    client = Mt5LocalExecutionClient()

    result = await client.place_order(make_signal(Direction.SELL), 0.01, entry_price=None)

    assert result.success
    assert result.order_id == "42"
    request = sent_orders[0]
    assert request["action"] == mt5.TRADE_ACTION_DEAL
    assert request["type"] == mt5.ORDER_TYPE_SELL
    assert request["price"] == 4016.0  # bid for a sell
    assert request["sl"] == 4023.0
    assert request["tp"] == 4009.0


@pytest.mark.asyncio
async def test_pending_buy_places_limit_order_at_entry(sent_orders):
    client = Mt5LocalExecutionClient()

    result = await client.place_order(
        make_signal(Direction.BUY, stop_loss=4009.0, take_profits=[4030.0]),
        0.01,
        entry_price=4018.0,
    )

    assert result.success
    request = sent_orders[0]
    assert request["action"] == mt5.TRADE_ACTION_PENDING
    assert request["type"] == mt5.ORDER_TYPE_BUY_LIMIT
    assert request["price"] == 4018.0


@pytest.mark.asyncio
async def test_filling_mode_fallback_walks_candidates(monkeypatch):
    monkeypatch.setattr(mt5, "symbol_select", lambda s, e=True: True, raising=False)
    monkeypatch.setattr(
        mt5, "symbol_info_tick",
        lambda s: SimpleNamespace(bid=4016.0, ask=4016.3), raising=False,
    )
    sent = []

    def order_send(request):
        sent.append(request)
        if request.get("type_filling") == mt5.ORDER_FILLING_IOC:
            return SimpleNamespace(retcode=mt5.TRADE_RETCODE_INVALID_FILL, comment="bad fill")
        return SimpleNamespace(retcode=mt5.TRADE_RETCODE_DONE, order=7, deal=0, comment="ok")

    monkeypatch.setattr(mt5, "order_send", order_send, raising=False)
    client = Mt5LocalExecutionClient()

    result = await client.place_order(make_signal(), 0.01, entry_price=None)

    assert result.success
    assert sent[0]["type_filling"] == mt5.ORDER_FILLING_IOC  # rejected
    assert sent[1]["type_filling"] == mt5.ORDER_FILLING_FOK  # accepted


@pytest.mark.asyncio
async def test_rejected_order_reports_failure(monkeypatch):
    monkeypatch.setattr(mt5, "symbol_select", lambda s, e=True: True, raising=False)
    monkeypatch.setattr(
        mt5, "symbol_info_tick",
        lambda s: SimpleNamespace(bid=4016.0, ask=4016.3), raising=False,
    )
    monkeypatch.setattr(
        mt5, "order_send",
        lambda request: SimpleNamespace(retcode=10019, comment="no money"),
        raising=False,
    )
    client = Mt5LocalExecutionClient()

    result = await client.place_order(make_signal(), 0.01, entry_price=None)

    assert not result.success
    assert "10019" in result.message
    assert "no money" in result.message
