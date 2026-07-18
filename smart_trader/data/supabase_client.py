"""Shared Supabase REST client for all data stores.

Thin wrapper around requests that handles auth headers, error logging,
and common query patterns (select, insert/upsert, delete).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests

from smart_trader.settings.credentials import load_credentials

logger = logging.getLogger(__name__)

_TIMEOUT = 30


class SupabaseClient:
    """Lightweight Supabase REST API client."""

    def __init__(self):
        creds = load_credentials()
        self.url = creds.get("supabase_url", "")
        self.key = creds.get("supabase_key", "")
        if not self.url or not self.key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_KEY must be set in .env"
            )

    @property
    def _headers(self) -> Dict[str, str]:
        return {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------ read

    def select(
        self,
        table: str,
        columns: str = "*",
        params: Optional[Dict[str, str]] = None,
    ) -> List[Dict[str, Any]]:
        """GET rows from a table. Returns list of dicts (may be empty)."""
        p = {"select": columns}
        if params:
            p.update(params)
        resp = requests.get(
            f"{self.url}/rest/v1/{table}",
            headers=self._headers,
            params=p,
            timeout=_TIMEOUT,
        )
        if resp.status_code >= 400:
            logger.warning(f"Supabase SELECT {table} failed {resp.status_code}: {resp.text[:300]}")
            return []
        return resp.json()

    # ------------------------------------------------------------------ write

    def upsert(
        self,
        table: str,
        rows: List[Dict[str, Any]],
        on_conflict: Optional[str] = None,
    ) -> bool:
        """POST rows with merge-duplicates (upsert). Returns True on success."""
        if not rows:
            return True
        prefer = "resolution=merge-duplicates,return=minimal"
        headers = {**self._headers, "Prefer": prefer}
        # Supabase needs on_conflict columns for upsert when table has
        # identity columns. Pass via query param if specified.
        params = {}
        if on_conflict:
            params["on_conflict"] = on_conflict
        resp = requests.post(
            f"{self.url}/rest/v1/{table}",
            headers=headers,
            params=params,
            json=rows,
            timeout=_TIMEOUT,
        )
        if resp.status_code >= 400:
            logger.warning(
                f"Supabase UPSERT {table} failed {resp.status_code}: {resp.text[:300]}"
            )
            return False
        return True

    def insert(
        self,
        table: str,
        rows: List[Dict[str, Any]],
        return_rows: bool = False,
    ) -> Optional[List[Dict[str, Any]]]:
        """POST rows (insert only, no upsert). Returns inserted rows if requested."""
        if not rows:
            return [] if return_rows else None
        prefer = "return=representation" if return_rows else "return=minimal"
        headers = {**self._headers, "Prefer": prefer}
        resp = requests.post(
            f"{self.url}/rest/v1/{table}",
            headers=headers,
            json=rows,
            timeout=_TIMEOUT,
        )
        if resp.status_code >= 400:
            logger.warning(
                f"Supabase INSERT {table} failed {resp.status_code}: {resp.text[:300]}"
            )
            return None
        if return_rows:
            return resp.json()
        return None

    # ------------------------------------------------------------------ delete

    def delete(self, table: str, params: Dict[str, str]) -> bool:
        """DELETE rows matching filter params. Returns True on success."""
        resp = requests.delete(
            f"{self.url}/rest/v1/{table}",
            headers=self._headers,
            params=params,
            timeout=_TIMEOUT,
        )
        if resp.status_code >= 400:
            logger.warning(
                f"Supabase DELETE {table} failed {resp.status_code}: {resp.text[:300]}"
            )
            return False
        return True
