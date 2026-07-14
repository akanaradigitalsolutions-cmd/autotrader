import asyncio
import logging
from typing import Optional

from metaapi_cloud_sdk import MetaApi

from bot.aio import externally_cancelled
from bot.models import Direction, ExecutionResult, TradeSignal
from bot.broker.base import ExecutionClient

logger = logging.getLogger(__name__)


class MetaApiExecutionClient(ExecutionClient):
    """Places orders on a real MT5 account via MetaApi.cloud's hosted terminal.

    Requires a MetaApi account (metaapi.cloud) with your MT5 login already
    linked. No local MT4/5 terminal or Windows machine is needed for this
    client - MetaApi runs the terminal on their side.

    The exact RPC method signatures below match the metaapi-cloud-sdk
    Python package at the time this was written; verify against the
    installed package version's docs if orders fail unexpectedly.
    """

    def __init__(self, token: str, account_id: str):
        self._api = MetaApi(token)
        self._account_id = account_id
        self._connection = None

    async def connect(self) -> None:
        account = await self._api.metatrader_account_api.get_account(self._account_id)
        if account.state not in ("DEPLOYED", "DEPLOYING"):
            await account.deploy()
        await account.wait_connected()

        self._connection = account.get_rpc_connection()
        await self._connection.connect()
        await self._connection.wait_synchronized()
        logger.info("Connected to MetaApi account %s", self._account_id)

    async def disconnect(self) -> None:
        if self._connection:
            await self._connection.close()

    async def count_open_positions(self, symbol: str) -> int:
        positions = await self._connection.get_positions()
        return sum(1 for p in positions if p.get("symbol") == symbol)

    async def get_current_price(self, symbol: str) -> tuple[float, float]:
        price = await self._connection.get_symbol_price(symbol)
        return float(price["bid"]), float(price["ask"])

    async def place_order(
        self, signal: TradeSignal, volume: float, entry_price: Optional[float] = None
    ) -> ExecutionResult:
        try:
            options = {}
            if signal.stop_loss is not None:
                options["stopLoss"] = signal.stop_loss
            if signal.primary_take_profit is not None:
                options["takeProfit"] = signal.primary_take_profit

            if entry_price is None:
                if signal.direction == Direction.BUY:
                    result = await self._connection.create_market_buy_order(
                        signal.symbol, volume, **options
                    )
                else:
                    result = await self._connection.create_market_sell_order(
                        signal.symbol, volume, **options
                    )
            else:
                # Entry zone hasn't been reached yet - place a pending limit
                # order that fills once price gets there instead of chasing
                # the market now.
                if signal.direction == Direction.BUY:
                    result = await self._connection.create_limit_buy_order(
                        signal.symbol, volume, entry_price, **options
                    )
                else:
                    result = await self._connection.create_limit_sell_order(
                        signal.symbol, volume, entry_price, **options
                    )

            order_id = str(result.get("orderId") or result.get("positionId") or "")
            return ExecutionResult(
                success=True,
                message="Order placed",
                order_id=order_id,
                signal=signal,
                dry_run=False,
            )
        except asyncio.CancelledError:
            if externally_cancelled():
                raise
            # The SDK cancelled its own request future (socket reconnect).
            # CancelledError bypasses `except Exception`, so without this the
            # whole calling task dies silently mid-order.
            logger.error("Order call was cancelled by the MetaApi client (connection reset)")
            return ExecutionResult(
                success=False,
                message="cancelled by MetaApi client (connection reset)",
                signal=signal,
                dry_run=False,
            )
        except Exception as exc:  # noqa: BLE001 - surface any broker error to the caller
            logger.exception("Order execution failed")
            return ExecutionResult(
                success=False, message=str(exc), signal=signal, dry_run=False
            )
