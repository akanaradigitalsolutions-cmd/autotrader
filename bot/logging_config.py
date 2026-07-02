import logging
import os
from logging.handlers import RotatingFileHandler

from bot.config import Settings


def setup_logging(settings: Settings) -> None:
    os.makedirs(os.path.dirname(settings.log_file), exist_ok=True)

    root = logging.getLogger()
    root.setLevel(settings.log_level)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        settings.log_file, maxBytes=5_000_000, backupCount=5
    )
    file_handler.setFormatter(formatter)

    root.handlers = [console_handler, file_handler]
