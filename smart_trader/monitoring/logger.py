"""
Structured JSON logging with rotating file handlers.

Log files (10 MB max, 30-day retention):
  logs/main.log    — all system events
  logs/trades.log  — trade executions only
  logs/alerts.log  — alert triggers only
  logs/regime.log  — regime changes only

Every JSON entry includes trading context: timestamp, regime, probability,
equity, positions count, daily_pnl.
"""
import json
import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional

import colorlog

from smart_trader.settings.config import MonitoringConfig


class TradingContext:
    """Thread-safe trading context injected into every log record."""

    def __init__(self):
        self._lock = Lock()
        self._data: Dict[str, Any] = {
            "regime": "",
            "probability": 0.0,
            "equity": 0.0,
            "positions": 0,
            "daily_pnl": 0.0,
        }

    def update(self, **kwargs) -> None:
        with self._lock:
            self._data.update(kwargs)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)


# Global singleton
trading_context = TradingContext()


class JSONFormatter(logging.Formatter):
    """Formats log records as JSON with trading context."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        entry.update(trading_context.snapshot())

        if record.exc_info and record.exc_info[0]:
            entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(entry, default=str)


def _rotating_handler(
    path: Path,
    max_bytes: int,
    backup_count: int,
    formatter: logging.Formatter,
    level: int = logging.DEBUG,
) -> RotatingFileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        str(path), maxBytes=max_bytes, backupCount=backup_count
    )
    handler.setLevel(level)
    handler.setFormatter(formatter)
    return handler


def setup_logging(config: Optional[MonitoringConfig] = None) -> None:
    """Configure logging for the trading system."""
    config = config or MonitoringConfig()
    log_dir = Path(config.log_dir)
    max_bytes = config.max_log_size_mb * 1024 * 1024
    backup_count = config.log_retention_days

    json_fmt = JSONFormatter()

    # Root logger
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))
    root.handlers.clear()

    # Console: colored human-readable
    console = colorlog.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s [%(levelname)-8s]%(reset)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            "DEBUG": "cyan",
            "INFO": "green",
            "WARNING": "yellow",
            "ERROR": "red",
            "CRITICAL": "bold_red",
        },
    ))
    root.addHandler(console)

    # main.log — everything (JSON)
    root.addHandler(_rotating_handler(
        log_dir / "main.log", max_bytes, backup_count, json_fmt
    ))

    # trades.log — trade events only
    trade_logger = logging.getLogger("trades")
    trade_logger.addHandler(_rotating_handler(
        log_dir / "trades.log", max_bytes, backup_count, json_fmt, logging.INFO
    ))

    # alerts.log — alert events only
    alert_logger = logging.getLogger("alerts")
    alert_logger.addHandler(_rotating_handler(
        log_dir / "alerts.log", max_bytes, backup_count, json_fmt, logging.INFO
    ))

    # regime.log — regime changes only
    regime_logger = logging.getLogger("regime")
    regime_logger.addHandler(_rotating_handler(
        log_dir / "regime.log", max_bytes, backup_count, json_fmt, logging.INFO
    ))

    logging.info("Logging initialized")
