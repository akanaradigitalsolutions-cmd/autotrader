from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram (MTProto user session, required to read a channel you only subscribe to)
    telegram_api_id: int
    telegram_api_hash: str
    telegram_session_name: str = "autotrader"
    # e.g. "@some_signal_channel" or numeric chat id. Comma-separate multiple
    # channels to monitor more than one signal source at once.
    telegram_channel: str

    # Which broker backend to use:
    #   "metaapi"  - MetaApi.cloud hosted terminal (works from any OS)
    #   "mt5local" - MT5 terminal running on THIS machine (Windows only,
    #                official MetaTrader5 package, no cloud middleman)
    broker_backend: str = "metaapi"

    # MetaApi.cloud (hosted MT5 connection, no local terminal/Windows required)
    metaapi_token: str = ""
    metaapi_account_id: str = ""

    # mt5local backend. Leave MT5_LOGIN=0 to attach to whatever account the
    # running terminal is already logged into (recommended: log in once in
    # the MT5 app on the VPS and keep it running).
    mt5_login: int = 0
    mt5_password: str = ""
    mt5_server: str = ""
    # Optional full path to terminal64.exe if MT5 is not in the default location.
    mt5_terminal_path: str = ""

    # Trading
    symbol: str = "XAUUSD"
    # Exact symbol name your MT5 broker uses for orders/price lookups - may
    # differ from `symbol` above (e.g. "XAUUSDm" on micro/cent accounts).
    # Defaults to `symbol` if not set.
    broker_symbol: str = ""
    lot_size: float = 0.01
    max_lot_size: float = 1.0
    # How many separate trades to open per signal, each using one of the
    # signal's take-profit levels (same entry/SL, different TP). Capped at
    # however many TP levels the signal actually contains.
    trades_per_signal: int = 1

    # Safety
    dry_run: bool = True
    max_open_positions: int = 3
    # Refuse to trade a signal when no stop loss could be parsed from it -
    # a naked position from a misread message is worse than a missed trade.
    require_stop_loss: bool = True
    # Some channels repost the same signal as a reminder minutes later; a
    # signal with identical levels seen again within this window is not
    # traded twice. 0 disables the guard.
    duplicate_signal_window_minutes: int = 120
    # Conflict guard: never hold a BUY and a SELL on the same symbol at once
    # (following several channels, they contradict each other and you get
    # stopped out both ways). Skips a signal opposite to what's already open.
    prevent_opposite_positions: bool = True
    # After the nearest take-profit (TP1) is hit, move the remaining
    # position(s) stop loss to their entry price, so a runner can't turn a
    # banked winner back into a loss.
    breakeven_after_tp1: bool = True
    # Circuit breaker: stop opening new trades once the day's realized loss
    # reaches this percent of the day-start balance. 0 disables. Resets at
    # UTC midnight (and on restart).
    daily_loss_limit_percent: float = 5.0
    # Every signal's reward:risk (TP1 distance vs stop distance) is logged;
    # a signal below this ratio is flagged as low quality (TP1 nearer than
    # the stop). Monitoring only - it does not block the trade.
    min_reward_risk: float = 1.0
    # Hard filter: skip (don't trade) any signal whose reward:risk is below
    # this. 0 disables (default). Set SKIP_REWARD_RISK_BELOW=1.0 in .env to
    # skip signals where TP1 is closer than the stop. A signal with no
    # computable R:R (no entry/stop/TP) is not filtered here.
    skip_reward_risk_below: float = 0.0
    # Trend filter: skip signals against the higher-timeframe trend (a BUY
    # below the EMA, or a SELL above it). "off" | "shadow" (only logs what it
    # would skip, still trades - use this to measure it first) | "on".
    trend_filter: str = "off"
    trend_ema_period: int = 50
    trend_timeframe: str = "H1"
    # Cancel a pending (limit) order that hasn't filled within this many
    # minutes, so a stale signal can't fill hours later into a reversed
    # market. 0 disables (pending orders stay good-till-cancelled).
    pending_order_expiry_minutes: int = 0
    # Honour channel cancellations ("don't trade it", "cancel", "no trade"):
    # skip a not-yet-opened signal and close any position already opened for
    # the cancelled signal (matched by its price levels).
    honor_cancellations: bool = True
    # Trailing stop (profit lock). Once a position is this many pips in
    # profit, trail its stop this far behind the best price, so a reversal
    # exits in profit instead of at the original stop. 1 gold pip = $0.10.
    # trailing_activate_pips = 0 disables it. Note: the stop is trailed on
    # the monitor's polling interval, so it locks sustained moves - it can't
    # catch a fast spike-and-reverse between checks.
    trailing_activate_pips: float = 50.0
    trailing_distance_pips: float = 15.0

    # Logging
    log_level: str = "INFO"
    log_file: str = "logs/autotrader.log"
    # CSV journal of every trade (open + close P/L) for the /report command.
    trades_log_file: str = "logs/trades.csv"

    @model_validator(mode="after")
    def _default_broker_symbol(self) -> "Settings":
        if not self.broker_symbol:
            self.broker_symbol = self.symbol
        return self


def load_settings() -> Settings:
    return Settings()
