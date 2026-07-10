# Autotrader

Copies XAU/USD trading signals from a Telegram channel and executes them on
MT5 with a preset lot size, using the stop loss / take profit levels from
the signal.

## Architecture

```
Telegram channel --> TelegramListener --> SignalParser --> TradingEngine --> MetaApiExecutionClient --> MT5 (via MetaApi.cloud)
```

- **TelegramListener** (`bot/telegram_listener.py`) - logs in as your Telegram
  user account (MTProto, via Telethon) and streams new messages from the
  configured channel. A user session is required because bots can't read
  channels they don't administer.
- **SignalParser** (`bot/signal_parser.py`) - regex-based extraction of
  symbol / direction / entry / SL / TP from the raw message text. Signal
  formats vary a lot between providers - you will likely need to tune the
  patterns to match your channel's exact wording. Send me a handful of real
  (redacted) sample messages and I can tighten this up.
- **TradingEngine** (`bot/engine.py`) - applies safety limits (dry-run,
  max open positions, capped lot size) and calls the broker.
- **MetaApiExecutionClient** (`bot/broker/metaapi_client.py`) - places the
  order on your real MT5 account via [MetaApi.cloud](https://metaapi.cloud),
  which runs the actual MT5 terminal on their infrastructure. **No Windows
  machine or local terminal is required for this bot.**

## Why MetaApi instead of a local MT5 terminal

MT4/MT5 terminals are Windows-only binaries with no official cross-platform
automation API. MetaApi hosts the terminal for you and exposes it over a
Python SDK, so this bot can run anywhere - your Mac, this dev container, or
a plain Linux VPS - now and later, without ever installing MT5 or Wine.

If you later decide to self-host instead (e.g. broker not supported by
MetaApi), the only piece that changes is `bot/broker/metaapi_client.py` -
swap it for an `ExecutionClient` implementation that talks to a
self-hosted MT5 terminal + EA bridge on a Windows VPS. Everything else
(Telegram listener, parser, engine) is unaffected.

## Setup

1. `python3 -m venv .venv && source .venv/bin/activate`
2. `pip install -r requirements.txt`
3. `cp .env.example .env` and fill in:
   - `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` from https://my.telegram.org
   - `METAAPI_TOKEN` / `METAAPI_ACCOUNT_ID` - from a MetaApi.cloud account
     with your MT5 login (start with a **demo account**) linked
   - leave `TELEGRAM_CHANNEL` blank for now - see step 4

4. **First-time Telegram login + finding your channel's id.** If the
   channel has a stylized display name (e.g. "𝐕𝐈𝐏 𝐒𝐈𝐆𝐍𝐀𝐋𝐒🔰") you often
   can't tell its real `@username` from the app, and some channels don't
   have a public username at all - so run the discovery helper instead of
   guessing:
   ```
   python scripts/list_channels.py
   ```
   This is also where the **first-time login** happens: it will prompt in
   the terminal for your phone number, then a login code. Telegram sends
   the code as a message from the official "Telegram" account inside the
   app itself (Settings, or just check your chat list) - not always SMS.
   If you have two-step verification (cloud password) enabled, it'll ask
   for that too. Once logged in, it's cached in a local `autotrader.session`
   file next to the code, so you won't be prompted again.

   The script then prints every channel/group you're in with its numeric
   chat id and username (if public). Find the row matching the signal
   channel and copy whichever it shows - either `@username` or the numeric
   id both work as `TELEGRAM_CHANNEL` in `.env`.
5. Leave `DRY_RUN=true` and run:
   ```
   python -m bot.main
   ```
   Watch the logs - every real signal from the channel will be parsed and
   logged as `[DRY RUN] Would place ...` without touching your MT5 account.
6. Once parsing looks correct against real traffic, set `DRY_RUN=false` to
   go live. Start on a demo MT5 account before ever pointing this at a
   funded one.

## Tests

```
pytest
```

Covers the signal parser and lot-size logic (the parts that don't need
live credentials).

## Deploying later

Since execution goes through MetaApi rather than a local terminal, "deploy"
just means running this same Python process somewhere that stays online -
a small Linux VPS is enough. You do not need a Windows VPS unless you
switch away from MetaApi to a self-hosted terminal.

## Running 24/7 (systemd)

Use the unit file in `deploy/autotrader.service` (install instructions are
in its comments). Two things matter for reliability:

- **`Restart=always`** - however the process ends, the bot isn't trading,
  so systemd must always bring it back. `on-failure` is not enough: a clean
  Telegram disconnect used to exit with code 0 and leave the bot down
  silently.
- The listener runs a **connection watchdog**: every 60s it makes a real
  Telegram API call, because Telethon can sit on a dead connection without
  ever raising. After 3 consecutive failures the process exits non-zero and
  systemd restarts it with a fresh connection. A
  `Heartbeat: Telegram connection healthy` line is logged every ~30 minutes
  as proof of life - if journalctl shows no heartbeat for an hour, something
  is wrong.

## Disclaimer

This executes real trades with real money once `DRY_RUN=false`. Test
thoroughly on a demo account first. Nothing here is financial advice, and
you are responsible for validating the parser against your channel's
actual signal format before going live.
