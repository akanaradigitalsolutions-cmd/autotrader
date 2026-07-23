import asyncio
import logging
from typing import Optional

import MetaTrader5 as mt5

from bot.broker.base import ExecutionClient
from bot.models import Direction, ExecutionResult, OpenPosition, TradeSignal

logger = logging.getLogger(__name__)

# Max slippage for market orders, in points.
DEVIATION_POINTS = 20
MAGIC_NUMBER = 20260701


class Mt5LocalExecutionClient(ExecutionClient):
    """Executes orders on a locally running MT5 terminal (Windows only).

    Uses the official MetaTrader5 Python package, which talks to the
    terminal over local IPC - the terminal must be installed, running and
    logged in to the trading account on the SAME machine as this bot.
    No cloud middleman: this removes the flaky bot<->MetaApi network hop.

    The MetaTrader5 API is synchronous, so every call runs in a worker
    thread to keep the asyncio event loop responsive.
    """

    def __init__(
        self,
        login: int = 0,
        password: str = "",
        server: str = "",
        terminal_path: str = "",
    ):
        self._login = login
        self._password = password
        self._server = server
        self._terminal_path = terminal_path

    async def connect(self) -> None:
        await asyncio.to_thread(self._initialize)

    def _initialize(self) -> None:
        kwargs = {}
        if self._terminal_path:
            kwargs["path"] = self._terminal_path
        if self._login:
            kwargs.update(
                login=self._login, password=self._password, server=self._server
            )
        if not mt5.initialize(**kwargs):
            raise ConnectionError(f"MT5 initialize failed: {mt5.last_error()}")
        info = mt5.account_info()
        if info is None:
            raise ConnectionError(
                f"MT5 terminal is running but no account is logged in: {mt5.last_error()}"
            )
        logger.info(
            "Connected to local MT5 terminal: account %s on %s (balance %s %s)",
            info.login, info.server, info.balance, info.currency,
        )

    async def reconnect(self) -> None:
        logger.warning("Reinitializing local MT5 connection")
        await asyncio.to_thread(mt5.shutdown)
        await asyncio.to_thread(self._initialize)

    async def disconnect(self) -> None:
        await asyncio.to_thread(mt5.shutdown)

    async def count_open_positions(self, symbol: str) -> int:
        positions = await asyncio.to_thread(mt5.positions_get, symbol=symbol)
        return len(positions or ())

    async def get_positions(self, symbol: str) -> list[OpenPosition]:
        raw = await asyncio.to_thread(mt5.positions_get, symbol=symbol)
        return [self._to_open_position(p) for p in (raw or ())]

    @staticmethod
    def _to_open_position(p) -> OpenPosition:
        direction = Direction.BUY if p.type == mt5.POSITION_TYPE_BUY else Direction.SELL
        return OpenPosition(
            id=str(p.ticket),
            symbol=p.symbol,
            direction=direction,
            volume=float(p.volume),
            open_price=float(p.price_open),
            stop_loss=float(p.sl) or None,
            take_profit=float(p.tp) or None,
            profit=float(p.profit),
        )

    async def get_account_balance(self) -> Optional[float]:
        info = await asyncio.to_thread(mt5.account_info)
        return float(info.balance) if info is not None else None

    async def modify_stop_loss(self, position_id: str, stop_loss: float) -> bool:
        return await asyncio.to_thread(self._modify_sl_sync, position_id, stop_loss)

    def _modify_sl_sync(self, position_id: str, stop_loss: float) -> bool:
        ticket = int(position_id)
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            logger.warning("Cannot modify SL: position %s no longer open", position_id)
            return False
        pos = positions[0]
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": pos.symbol,
            "position": ticket,
            "sl": stop_loss,
            "tp": pos.tp,  # keep the existing take profit
            "magic": MAGIC_NUMBER,
        }
        result = mt5.order_send(request)
        if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
            return True
        logger.error(
            "Modify SL failed for %s: %s",
            position_id,
            getattr(result, "comment", None) or mt5.last_error(),
        )
        return False

    async def get_current_price(self, symbol: str) -> tuple[float, float]:
        return await asyncio.to_thread(self._get_tick, symbol)

    def _get_tick(self, symbol: str) -> tuple[float, float]:
        mt5.symbol_select(symbol, True)
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise ConnectionError(f"No price for {symbol}: {mt5.last_error()}")
        return float(tick.bid), float(tick.ask)

    async def place_order(
        self, signal: TradeSignal, volume: float, entry_price: Optional[float] = None
    ) -> ExecutionResult:
        try:
            return await asyncio.to_thread(
                self._place_order_sync, signal, volume, entry_price
            )
        except Exception as exc:  # noqa: BLE001 - surface any broker error to the caller
            logger.exception("Order execution failed")
            return ExecutionResult(
                success=False, message=str(exc), signal=signal, dry_run=False
            )

    def _place_order_sync(
        self, signal: TradeSignal, volume: float, entry_price: Optional[float]
    ) -> ExecutionResult:
        symbol = signal.symbol
        mt5.symbol_select(symbol, True)

        request: dict = {
            "symbol": symbol,
            "volume": volume,
            "deviation": DEVIATION_POINTS,
            "magic": MAGIC_NUMBER,
            "comment": "autotrader",
            "type_time": mt5.ORDER_TIME_GTC,
        }
        if signal.stop_loss is not None:
            request["sl"] = signal.stop_loss
        if signal.primary_take_profit is not None:
            request["tp"] = signal.primary_take_profit

        if entry_price is None:
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                raise ConnectionError(f"No price for {symbol}: {mt5.last_error()}")
            request["action"] = mt5.TRADE_ACTION_DEAL
            if signal.direction == Direction.BUY:
                request["type"] = mt5.ORDER_TYPE_BUY
                request["price"] = float(tick.ask)
            else:
                request["type"] = mt5.ORDER_TYPE_SELL
                request["price"] = float(tick.bid)
            # Market orders: most brokers want IOC or FOK.
            filling_candidates = (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, None)
        else:
            # Entry zone not reached yet - pending limit order at its edge.
            request["action"] = mt5.TRADE_ACTION_PENDING
            request["type"] = (
                mt5.ORDER_TYPE_BUY_LIMIT
                if signal.direction == Direction.BUY
                else mt5.ORDER_TYPE_SELL_LIMIT
            )
            request["price"] = entry_price
            # Pending orders: RETURN is the usual mode.
            filling_candidates = (
                mt5.ORDER_FILLING_RETURN, mt5.ORDER_FILLING_IOC,
                mt5.ORDER_FILLING_FOK, None,
            )

        result = self._send_with_filling_fallback(request, filling_candidates)
        if result is None:
            raise ConnectionError(f"order_send returned nothing: {mt5.last_error()}")
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return ExecutionResult(
                success=False,
                message=f"retcode={result.retcode} {getattr(result, 'comment', '')}".strip(),
                signal=signal,
                dry_run=False,
            )
        order_id = str(getattr(result, "order", 0) or getattr(result, "deal", 0) or "")
        return ExecutionResult(
            success=True, message="Order placed", order_id=order_id,
            signal=signal, dry_run=False,
        )

    @staticmethod
    def _send_with_filling_fallback(request: dict, filling_candidates: tuple):
        """Brokers disagree on the required order filling mode; walk the
        likely candidates until one is accepted (None = terminal default)."""
        invalid_fill = getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030)
        result = None
        for filling in filling_candidates:
            attempt = dict(request)
            if filling is not None:
                attempt["type_filling"] = filling
            result = mt5.order_send(attempt)
            if result is not None and result.retcode == invalid_fill:
                continue
            return result
        return result
