# Running the bot on a Windows VPS with a local MT5 terminal

This removes the MetaApi cloud middleman entirely: the bot and the MT5
terminal run on the SAME Windows machine and talk over local IPC via the
official `MetaTrader5` Python package. No bot<->cloud websocket to drop.

```
Telegram --> bot (Python, this repo) --> MT5 terminal (same machine) --> broker
```

## 1. Get a Windows VPS

Any Windows Server 2019/2022 VPS with **2+ GB RAM** works (1 GB is too
tight for Windows + MT5 + Python). Options:

- AWS EC2: `t3.small` or `t3.medium`, "Microsoft Windows Server 2022 Base"
  AMI (plain Base - NOT a SQL Server bundle). Connect via RDP (the EC2
  console shows how to get the Administrator password from your .pem key).
- Or any cheap Windows VPS / "forex VPS" provider. Pick a region close to
  your broker's servers if you can.

## 2. Install MT5 and log in

1. RDP into the VPS.
2. Download MT5 from your broker (e.g. Exness client area) and install it.
3. Log in to your trading account (start with the demo) and leave the
   terminal RUNNING. Enable: Tools > Options > Expert Advisors >
   "Allow algorithmic trading".
4. Make MT5 start automatically after a reboot: create a shortcut to
   `terminal64.exe` inside `shell:startup` (Win+R, type `shell:startup`).

## 3. Install the bot

1. Install Python 3.12 (python.org installer, tick "Add python.exe to PATH")
   and Git for Windows.
2. In PowerShell:
   ```powershell
   cd $HOME
   git clone https://github.com/akanaradigitalsolutions-cmd/autotrader
   cd autotrader
   git checkout claude/bot-startup-logging-sko7g7
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -r requirements-windows.txt
   ```
3. Copy `.env` and the `*.session` file from the old server (scp them to
   your machine first, or recreate the session by running
   `python scripts/list_channels.py` once and logging in).
4. Edit `.env`:
   ```
   BROKER_BACKEND=mt5local
   MT5_LOGIN=0        # attach to the account the terminal is logged into
   DRY_RUN=true       # ALWAYS dry-run first on a new setup
   ```
   (`SYMBOL`/`BROKER_SYMBOL` stay the same; METAAPI_* lines can stay, they
   are ignored by this backend.)

## 4. Test, then go live

```powershell
python -m bot.main
```
You should see "Connected to local MT5 terminal" and the Telegram startup
message. Let a real signal arrive in DRY_RUN and check the log lines, then
set `DRY_RUN=false` and restart.

## 5. Keep it running 24/7 (the systemd equivalent)

Use Task Scheduler:
1. Task Scheduler > Create Task.
2. General: "Run whether user is logged on or not", "Run with highest
   privileges".
3. Triggers: "At startup", plus "On a schedule / repeat every 5 minutes
   indefinitely" (acts as the restart-if-dead loop; a second instance
   exits immediately if one is already running is NOT built in, so
   instead set "If the task is already running: Do not start a new
   instance").
4. Actions: Start a program:
   - Program: `C:\Users\<you>\autotrader\.venv\Scripts\python.exe`
   - Arguments: `-m bot.main`
   - Start in: `C:\Users\<you>\autotrader`
5. Settings: enable "If the task fails, restart every 1 minute" with
   "Attempt to restart up to 99 times".

Also disable automatic Windows Update reboots during trading hours
(Settings > Windows Update > Advanced > active hours), or trades can be
missed while the VPS reboots.

All the bot's protections (watchdogs, retries, Telegram alerts, /status)
work identically on this backend.
