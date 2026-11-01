"""One-off check: connect to the local MT5 terminal and fetch a few candles on
each configured timeframe, to confirm the market-snapshot recorder will get
real OHLC data. READ-ONLY - it places no orders and changes nothing.

Run it on the VPS with the bot's own Python, while the bot is stopped (it
shares the MT5 terminal):

    Stop-ScheduledTask -TaskName "autotrader"
    Get-Process python,terminal64 -ErrorAction SilentlyContinue | Stop-Process -Force
    & "C:\\Users\\Administrator\\autotrader\\.venv\\Scripts\\python.exe" scripts\\test_candles.py
    Start-ScheduledTask -TaskName "autotrader"
"""

import asyncio

from bot.config import load_settings


async def main() -> None:
    settings = load_settings()
    if settings.broker_backend.lower() != "mt5local":
        print(f"BROKER_BACKEND is '{settings.broker_backend}', not 'mt5local' - "
              "this check is only for the local MT5 backend.")
        return

    from bot.broker.mt5_local_client import Mt5LocalExecutionClient

    broker = Mt5LocalExecutionClient(
        login=settings.mt5_login,
        password=settings.mt5_password,
        server=settings.mt5_server,
        terminal_path=settings.mt5_terminal_path,
    )
    print("Connecting to MT5 terminal...")
    await broker.connect()
    symbol = settings.broker_symbol or settings.symbol
    timeframes = [t.strip().upper() for t in settings.market_data_timeframes.split(",") if t.strip()]
    print(f"Symbol: {symbol}   Timeframes: {timeframes}\n")

    all_ok = True
    for tf in timeframes:
        candles = await broker.get_candles(symbol, tf, 3)
        if not candles:
            all_ok = False
            print(f"{tf:>4}: ❌ NO DATA (empty) - MT5 returned nothing for this timeframe")
            continue
        last = candles[-1]
        print(f"{tf:>4}: ✅ {len(candles)} candles. Latest "
              f"O={last['o']} H={last['h']} L={last['l']} C={last['c']} V={last['v']}")

    print("\n" + ("ALL TIMEFRAMES OK - the recorder will capture real candles. ✅"
                  if all_ok else
                  "Some timeframes returned no data - check the symbol name and that "
                  "MT5 has history for them. ⚠️"))
    await broker.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
