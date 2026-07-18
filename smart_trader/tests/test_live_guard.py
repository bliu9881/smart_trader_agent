"""Tests for the live-account opt-in guard (SmartTrader._verify_live_opt_in).

Paper is always the default. Live trading requires BOTH ALLOW_LIVE_TRADING and a
matching LIVE_ACCOUNT_ID — two independent gates so a single stray flag can never
put real money at risk.
"""
import pytest

from smart_trader.main import SmartTrader


def _creds(allow=False, live_id=""):
    return {"allow_live_trading": allow, "live_account_id": live_id}


class TestLiveOptIn:
    def test_no_opt_in_aborts(self):
        """Live account with no opt-in → abort (the safe default)."""
        with pytest.raises(SystemExit):
            SmartTrader._verify_live_opt_in(_creds(allow=False, live_id="U123"), "U123")

    def test_allow_but_account_mismatch_aborts(self):
        """Opt-in on but account id doesn't match LIVE_ACCOUNT_ID → abort."""
        with pytest.raises(SystemExit):
            SmartTrader._verify_live_opt_in(_creds(allow=True, live_id="U999"), "U123")

    def test_allow_but_expected_unset_aborts(self):
        """Opt-in on but LIVE_ACCOUNT_ID unset → abort (can't confirm account)."""
        with pytest.raises(SystemExit):
            SmartTrader._verify_live_opt_in(_creds(allow=True, live_id=""), "U123")

    def test_allow_and_match_proceeds(self):
        """Both gates satisfied → no SystemExit (live trading permitted)."""
        SmartTrader._verify_live_opt_in(_creds(allow=True, live_id="U123"), "U123")

    def test_whitespace_in_account_is_tolerated(self):
        SmartTrader._verify_live_opt_in(_creds(allow=True, live_id="U123"), "  U123 ")
