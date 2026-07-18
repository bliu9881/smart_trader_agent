"""Tests for the market regime classifier (smart_trader.core.regime)."""
import numpy as np
import pandas as pd

from smart_trader.core.regime import (
    classify_regime, RegimeState, BULL, AMBIGUOUS, BEAR, DEFENSIVE, AGGRESSIVE,
)


def _series(values):
    return pd.Series(values, dtype=float)


def _rising(n=300, start=100.0, step=0.5):
    """Steadily rising series → price well above both SMAs (bull)."""
    return _series([start + i * step for i in range(n)])


def _falling(n=300, start=250.0, step=0.5):
    """Steadily falling series → price below both SMAs (bear)."""
    return _series([start - i * step for i in range(n)])


class TestInsufficientData:
    def test_too_short_returns_none(self):
        assert classify_regime(_series([100.0] * 100)) is None

    def test_none_returns_none(self):
        assert classify_regime(None) is None

    def test_just_under_threshold_none(self):
        # need 200 + slope_lookback(20) = 220
        assert classify_regime(_rising(n=219), slope_lookback=20) is None


class TestBull:
    def test_uptrend_is_bull_and_allows(self):
        st = classify_regime(_rising())
        assert st is not None
        assert st.zone == BULL
        assert st.above_200 and st.above_50
        assert st.entries_allowed  # bull always allows
        assert st.sma200_rising

    def test_bull_allows_regardless_of_posture(self):
        for posture in (DEFENSIVE, AGGRESSIVE):
            st = classify_regime(_rising(), posture=posture)
            assert st.zone == BULL and st.entries_allowed


class TestBear:
    def test_downtrend_is_bear_and_blocks(self):
        st = classify_regime(_falling())
        assert st.zone == BEAR
        assert not st.above_50 and not st.above_200
        assert not st.entries_allowed

    def test_bear_blocks_regardless_of_posture(self):
        for posture in (DEFENSIVE, AGGRESSIVE):
            st = classify_regime(_falling(), posture=posture)
            assert st.zone == BEAR and not st.entries_allowed


class TestAmbiguousPostureGate:
    def _ambiguous_series(self):
        """High plateau → sharp drop → modest bounce, engineered so the final
        close sits ABOVE the fast 50-SMA but BELOW the still-elevated 200-SMA
        (close≈178, 50-SMA≈171, 200-SMA≈226)."""
        plateau = [250.0] * 200
        drop = [250.0 - (i + 1) * (100.0 / 60) for i in range(60)]      # 250 → 150
        bounce = [drop[-1] + (i + 1) * (28.0 / 20) for i in range(20)]  # 150 → 178
        return _series(plateau + drop + bounce)

    def test_zone_is_ambiguous(self):
        st = classify_regime(self._ambiguous_series())
        assert st is not None
        assert st.zone == AMBIGUOUS
        assert st.above_50 and not st.above_200

    def test_defensive_blocks_ambiguous(self):
        st = classify_regime(self._ambiguous_series(), posture=DEFENSIVE)
        assert st.zone == AMBIGUOUS
        assert not st.entries_allowed  # strict 50+200 behavior

    def test_aggressive_allows_ambiguous(self):
        st = classify_regime(self._ambiguous_series(), posture=AGGRESSIVE)
        assert st.zone == AMBIGUOUS
        assert st.entries_allowed  # plain 50-SMA behavior

    def test_unknown_posture_defaults_to_defensive(self):
        st = classify_regime(self._ambiguous_series(), posture="nonsense")
        assert st.posture == DEFENSIVE
        assert not st.entries_allowed


class TestConfigContract:
    """The live loop reads these off `self.config.smart_money` (main._cycle and
    _refresh_regime). This guards against them silently drifting to another
    dataclass — the exact bug that crashed every cycle on first deploy.
    """

    def test_smart_money_config_exposes_regime_and_gate_fields(self):
        from smart_trader.settings.config import load_config

        sm = load_config().smart_money
        assert isinstance(sm.scanner_standalone_entries_enabled, bool)
        assert isinstance(sm.regime_filter_enabled, bool)
        assert sm.regime_posture in (DEFENSIVE, AGGRESSIVE)
        assert isinstance(sm.regime_symbol, str) and sm.regime_symbol
        assert isinstance(sm.regime_slope_lookback, int) and sm.regime_slope_lookback > 0


class TestStateShape:
    def test_to_dict_round_trips_fields(self):
        st = classify_regime(_rising())
        d = st.to_dict()
        assert d["zone"] == BULL
        assert set(d) == {
            "zone", "posture", "entries_allowed", "close", "sma_50", "sma_200",
            "above_50", "above_200", "sma200_rising", "reason",
        }
        assert isinstance(st, RegimeState)
