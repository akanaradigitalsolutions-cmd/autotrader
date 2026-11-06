import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from bot.config import Settings


def setup_logging(settings: Settings) -> None:
    os.makedirs(os.path.dirname(settings.log_file), exist_ok=True)

    # Windows consoles default to a legacy code page (cp1252) that cannot
    # encode the emoji used in alert texts - without this, logging such a
    # message raises UnicodeEncodeError instead of logging.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    root = logging.getLogger()
    root.setLevel(settings.log_level)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        settings.log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    root.handlers = [console_handler, file_handler]
