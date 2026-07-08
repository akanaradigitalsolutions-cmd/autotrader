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

    # MetaApi.cloud (hosted MT5 connection, no local terminal/Windows required)
    metaapi_token: str
    metaapi_account_id: str

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

    # Logging
    log_level: str = "INFO"
    log_file: str = "logs/autotrader.log"

    @model_validator(mode="after")
    def _default_broker_symbol(self) -> "Settings":
        if not self.broker_symbol:
            self.broker_symbol = self.symbol
        return self


def load_settings() -> Settings:
    return Settings()
