"""
Alert system with rate limiting and expanded triggers.

Triggers:
  - circuit_breaker       (critical)
  - regime_change         (info)
  - trade_executed        (info)
  - drawdown_warning      (warning)   — approaching circuit breaker thresholds
  - connection_lost       (critical)
  - connection_restored   (info)
  - model_retrained       (info)
  - flicker_detected      (warning)
  - position_stopped_out  (warning)
  - daily_summary         (info)

Delivery: email (SMTP) + webhook (Slack/Discord)
Rate limit: per-trigger cooldown (default 5 min) to prevent spam.
"""
import logging
import smtplib
from collections import deque
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

import requests

from smart_trader.settings.config import MonitoringConfig

logger = logging.getLogger(__name__)
alert_logger = logging.getLogger("alerts")


class AlertManager:
    """Sends alerts with per-trigger rate limiting and history tracking."""

    def __init__(self, config: Optional[MonitoringConfig] = None):
        self.config = config or MonitoringConfig()
        self._cooldown = timedelta(seconds=self.config.alert_cooldown_seconds)
        self._last_sent: Dict[str, datetime] = {}
        self._history: deque = deque(maxlen=100)

    # ---------------------------------------------------------------- core

    def send_alert(self, trigger: str, subject: str, message: str, severity: str = "info") -> bool:
        """
        Send an alert if the trigger hasn't been rate-limited.
        Returns True if sent, False if suppressed.
        """
        now = datetime.now()
        last = self._last_sent.get(trigger)
        if last and (now - last) < self._cooldown:
            return False

        self._last_sent[trigger] = now
        entry = {
            "timestamp": now.isoformat(),
            "trigger": trigger,
            "severity": severity,
            "subject": subject,
            "message": message,
        }
        self._history.append(entry)

        # Log to alerts.log
        alert_logger.log(
            logging.CRITICAL if severity == "critical" else
            logging.WARNING if severity == "warning" else logging.INFO,
            f"[{trigger}] {subject}: {message}"
        )

        if self.config.enable_email_alerts:
            self._send_email(subject, message, severity)
        if self.config.enable_webhook_alerts and self.config.webhook_url:
            self._send_webhook(subject, message, severity)

        return True

    def get_history(self) -> List[Dict[str, Any]]:
        """Return recent alert history (last 100)."""
        return list(self._history)

    # ----------------------------------------------------------- triggers

    def alert_circuit_breaker(self, level: str, details: str) -> None:
        self.send_alert(
            trigger="circuit_breaker",
            subject=f"Circuit Breaker: {level}",
            message=details,
            severity="critical" if level in ("close_all", "halt") else "warning",
        )

    def alert_regime_change(self, old_regime: str, new_regime: str, confidence: float) -> None:
        regime_logger = logging.getLogger("regime")
        regime_logger.info(f"Regime change: {old_regime} → {new_regime} ({confidence:.1%})")
        self.send_alert(
            trigger="regime_change",
            subject=f"Regime: {old_regime} → {new_regime}",
            message=f"Confidence: {confidence:.1%}",
            severity="info",
        )

    def alert_trade_executed(self, symbol: str, side: str, quantity: int, price: float) -> None:
        trade_logger = logging.getLogger("trades")
        trade_logger.info(f"{side} {quantity} {symbol} @ ${price:.2f}")
        self.send_alert(
            trigger="trade_executed",
            subject=f"Trade: {side} {quantity} {symbol} @ ${price:.2f}",
            message="Order executed",
            severity="info",
        )

    def alert_drawdown_warning(self, dd_type: str, pct: float, threshold: float) -> None:
        self.send_alert(
            trigger="drawdown_warning",
            subject=f"Drawdown Warning: {dd_type}",
            message=f"Current: {pct:.2%}, threshold: {threshold:.0%}",
            severity="warning",
        )

    def alert_connection_lost(self) -> None:
        self.send_alert(
            trigger="connection_lost",
            subject="IBKR Connection Lost",
            message="Attempting reconnect...",
            severity="critical",
        )

    def alert_connection_restored(self) -> None:
        self.send_alert(
            trigger="connection_restored",
            subject="IBKR Connection Restored",
            message="API connection re-established",
            severity="info",
        )

    def alert_model_retrained(self, n_regimes: int, bic: float) -> None:
        self.send_alert(
            trigger="model_retrained",
            subject=f"HMM Retrained: {n_regimes} regimes",
            message=f"BIC: {bic:.2f}",
            severity="info",
        )

    def alert_flicker_detected(self, rate: float) -> None:
        self.send_alert(
            trigger="flicker_detected",
            subject="Regime Flickering",
            message=f"Change rate: {rate:.2f}/bar (threshold exceeded)",
            severity="warning",
        )

    def alert_position_stopped_out(self, symbol: str, loss: float) -> None:
        self.send_alert(
            trigger="position_stopped_out",
            subject=f"Stop Hit: {symbol}",
            message=f"Loss: ${loss:,.2f}",
            severity="warning",
        )

    def alert_daily_summary(self, equity: float, daily_pnl: float, trades: int, regime: str) -> None:
        self.send_alert(
            trigger="daily_summary",
            subject="Daily Summary",
            message=(
                f"Equity: ${equity:,.2f} | P&L: ${daily_pnl:+,.2f} | "
                f"Trades: {trades} | Regime: {regime}"
            ),
            severity="info",
        )

    # ---------------------------------------------------------- delivery

    def _send_email(self, subject: str, body: str, severity: str) -> None:
        if not self.config.email_from or not self.config.email_to:
            return
        try:
            msg = MIMEText(f"[{severity.upper()}]\n\n{body}")
            msg["Subject"] = f"[Regime Trader] {subject}"
            msg["From"] = self.config.email_from
            msg["To"] = self.config.email_to
            with smtplib.SMTP(self.config.email_smtp_host, self.config.email_smtp_port) as server:
                server.starttls()
                server.login(self.config.email_from, "")
                server.send_message(msg)
        except Exception as e:
            logger.error(f"Email failed: {e}")

    def _send_webhook(self, subject: str, body: str, severity: str) -> None:
        try:
            payload = {
                "text": f"*[{severity.upper()}] {subject}*\n{body}",
                "username": "Regime Trader Bot",
            }
            resp = requests.post(self.config.webhook_url, json=payload, timeout=10)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"Webhook failed: {e}")
