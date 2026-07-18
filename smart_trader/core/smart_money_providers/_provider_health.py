"""Provider health tracking for smart-money data sources.

Tracks per-provider last-successful-non-empty fetch timestamps and surfaces
silent regressions where a provider's parser or upstream API has broken
without raising an exception. The 2026-04 Capitol Trades / Insider Cluster /
Berkshire 13F outages were each silent for days because each provider's
fetch wrapped exceptions and returned an empty list — indistinguishable
from "the upstream genuinely had nothing to report this cycle." This guard
makes that distinction observable.

State persists to {cache_dir}/provider_health.json so streaks survive
across trader restarts. Health updates only on fresh fetches; cache HITs
do not change state because they replay an earlier observation rather
than producing a new one.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ProviderHealth:
    """Per-provider health snapshot."""
    provider_name: str
    last_fetch_at: Optional[datetime] = None
    last_fetch_count: int = 0
    last_non_empty_at: Optional[datetime] = None
    last_non_empty_count: int = 0
    consecutive_empty_fetches: int = 0


class ProviderHealthTracker:
    """Tracks per-provider fetch outcomes and reports degraded providers."""

    _STATE_FILE = "provider_health.json"

    def __init__(self, cache_dir: Path, max_empty_days: Dict[str, float]):
        self.cache_dir = Path(cache_dir)
        self.max_empty_days: Dict[str, float] = dict(max_empty_days)
        self.state: Dict[str, ProviderHealth] = self._load()

    def record_fetch(self, provider_name: str, count: int) -> None:
        """Record the outcome of a fresh fetch.

        Increments the empty-streak counter when count == 0 and resets it
        when count > 0. Updates last_non_empty_at only when count > 0,
        which is the timestamp the degraded-providers check is based on.
        """
        h = self.state.get(provider_name) or ProviderHealth(provider_name=provider_name)
        now = datetime.now()
        h.last_fetch_at = now
        h.last_fetch_count = count
        if count > 0:
            h.last_non_empty_at = now
            h.last_non_empty_count = count
            h.consecutive_empty_fetches = 0
        else:
            h.consecutive_empty_fetches += 1
        self.state[provider_name] = h
        self._save()

    def degraded_providers(self) -> List[Tuple[str, str]]:
        """Return [(provider_name, reason)] for providers past their threshold.

        Providers not present in max_empty_days are not health-checked.
        Providers that have never been fetched are skipped — there is no
        observation to base a verdict on.
        """
        out: List[Tuple[str, str]] = []
        now = datetime.now()
        for name, threshold_days in self.max_empty_days.items():
            h = self.state.get(name)
            if h is None or h.last_fetch_at is None:
                continue

            if h.last_non_empty_at is None:
                age_days = (now - h.last_fetch_at).total_seconds() / 86400
                if age_days >= threshold_days:
                    out.append((
                        name,
                        f"never returned data ({h.consecutive_empty_fetches} "
                        f"empty fetches over {age_days:.1f}d)",
                    ))
                continue

            age_days = (now - h.last_non_empty_at).total_seconds() / 86400
            if age_days >= threshold_days:
                out.append((
                    name,
                    f"silent for {age_days:.1f}d "
                    f"(last had {h.last_non_empty_count} filings on "
                    f"{h.last_non_empty_at.date()}, "
                    f"{h.consecutive_empty_fetches} consecutive empty fetches since)",
                ))
        return out

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> Dict[str, ProviderHealth]:
        path = self.cache_dir / self._STATE_FILE
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text())
        except Exception as e:
            logger.warning(f"Provider health: state file corrupt ({e}); starting fresh")
            return {}

        out: Dict[str, ProviderHealth] = {}
        for name, raw in data.items():
            try:
                out[name] = ProviderHealth(
                    provider_name=name,
                    last_fetch_at=_parse_iso(raw.get("last_fetch_at")),
                    last_fetch_count=int(raw.get("last_fetch_count", 0) or 0),
                    last_non_empty_at=_parse_iso(raw.get("last_non_empty_at")),
                    last_non_empty_count=int(raw.get("last_non_empty_count", 0) or 0),
                    consecutive_empty_fetches=int(
                        raw.get("consecutive_empty_fetches", 0) or 0
                    ),
                )
            except Exception:
                continue
        return out

    def _save(self) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            data = {
                name: {
                    "last_fetch_at": _iso(h.last_fetch_at),
                    "last_fetch_count": h.last_fetch_count,
                    "last_non_empty_at": _iso(h.last_non_empty_at),
                    "last_non_empty_count": h.last_non_empty_count,
                    "consecutive_empty_fetches": h.consecutive_empty_fetches,
                }
                for name, h in self.state.items()
            }
            (self.cache_dir / self._STATE_FILE).write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.warning(f"Provider health: failed to persist state ({e})")


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
