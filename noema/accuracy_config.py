"""Config knobs for research scoring. None of these arm live execution."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return float(raw)


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


@dataclass(frozen=True)
class AccuracyConfig:
    """Thresholds for calibration, influence, judging, and research backtests.

    Dollar caps default to the existing judge ($0.50/day) and cognition
    ($0.50/day in code; a deployment may set the model budget to $2/day).
    Sample count is reduced to stay inside whichever cap is tighter.
    """

    min_resolved: int = 30
    max_calibration_error: float = 0.10
    max_brier: float = 0.25
    prior_strength: float = 20.0
    max_weight: float = 1.0
    bucket_width: float = 0.10
    windows: tuple[int, ...] = (7, 30)
    base_rate: float = 0.50
    base_rate_strength: float = 0.50
    min_edge: float = 0.02
    binary_slippage: float = 0.01
    binary_fee: float = 0.03
    judge_samples: int = 3
    judge_temperature: float = 0.7
    max_disagreement: float = 0.12
    judge_budget_usd: float = 0.50
    influence_enabled: bool = True
    trench_horizon_seconds: int = 3600
    backtest_horizon_seconds: int = 86400
    lesson_chars: int = 500
    ohlcv_horizon_bars: int = 12
    backtest_kalshi_limit: int = 60
    backtest_polymarket_limit: int = 20
    backtest_pools: int = 3

    @classmethod
    def from_env(cls) -> AccuracyConfig:
        windows_raw = os.getenv("NOEMA_ACCURACY_WINDOWS", "7,30")
        windows = tuple(int(part) for part in windows_raw.split(",") if part.strip())
        if not windows or any(item <= 0 for item in windows):
            raise ValueError("NOEMA_ACCURACY_WINDOWS must be positive day counts")
        samples = _int("NOEMA_JUDGE_SELF_CONSISTENCY_SAMPLES", 3)
        if samples < 1:
            raise ValueError("judge sample count must be at least 1")
        return cls(
            min_resolved=_int("NOEMA_ACCURACY_MIN_RESOLVED", 30),
            max_calibration_error=_float("NOEMA_ACCURACY_MAX_CALIBRATION_ERROR", 0.10),
            max_brier=_float("NOEMA_ACCURACY_MAX_BRIER", 0.25),
            prior_strength=_float("NOEMA_ACCURACY_PRIOR_STRENGTH", 20.0),
            max_weight=_float("NOEMA_ACCURACY_MAX_WEIGHT", 1.0),
            bucket_width=_float("NOEMA_ACCURACY_BUCKET_WIDTH", 0.10),
            windows=windows,
            base_rate=_float("NOEMA_ACCURACY_BASE_RATE", 0.50),
            base_rate_strength=_float("NOEMA_ACCURACY_BASE_RATE_STRENGTH", 0.50),
            min_edge=_float("NOEMA_ACCURACY_MIN_EDGE", 0.02),
            binary_slippage=_float("NOEMA_ACCURACY_BINARY_SLIPPAGE", 0.01),
            binary_fee=_float("NOEMA_ACCURACY_BINARY_FEE", 0.03),
            judge_samples=samples,
            judge_temperature=_float("NOEMA_JUDGE_TEMPERATURE", 0.7),
            max_disagreement=_float("NOEMA_JUDGE_MAX_DISAGREEMENT", 0.12),
            judge_budget_usd=_float("NOEMA_DESK_JUDGEMENT_MAX_USD_PER_DAY", 0.50),
            influence_enabled=os.getenv("NOEMA_ACCURACY_INFLUENCE_ENABLED", "1") == "1",
            trench_horizon_seconds=_int("NOEMA_ACCURACY_TRENCH_HORIZON_SECONDS", 3600),
            backtest_horizon_seconds=_int("NOEMA_ACCURACY_BACKTEST_HORIZON_SECONDS", 86400),
            lesson_chars=_int("NOEMA_ACCURACY_LESSON_CHARS", 500),
            ohlcv_horizon_bars=_int("NOEMA_ACCURACY_OHLCV_HORIZON_BARS", 12),
            backtest_kalshi_limit=_int("NOEMA_ACCURACY_BACKTEST_KALSHI_LIMIT", 60),
            backtest_polymarket_limit=_int("NOEMA_ACCURACY_BACKTEST_POLYMARKET_LIMIT", 20),
            backtest_pools=_int("NOEMA_ACCURACY_BACKTEST_POOLS", 3),
        )

    def public_dict(self) -> dict[str, object]:
        return {
            "min_resolved": self.min_resolved,
            "max_calibration_error": self.max_calibration_error,
            "max_brier": self.max_brier,
            "prior_strength": self.prior_strength,
            "max_weight": self.max_weight,
            "bucket_width": self.bucket_width,
            "windows_days": list(self.windows),
            "base_rate": self.base_rate,
            "base_rate_strength": self.base_rate_strength,
            "min_edge": self.min_edge,
            "binary_slippage": self.binary_slippage,
            "binary_fee": self.binary_fee,
            "judge_samples": self.judge_samples,
            "judge_temperature": self.judge_temperature,
            "max_disagreement": self.max_disagreement,
            "judge_budget_usd_per_day": self.judge_budget_usd,
            "influence_enabled": self.influence_enabled,
            "trench_horizon_seconds": self.trench_horizon_seconds,
            "backtest_horizon_seconds": self.backtest_horizon_seconds,
            "lesson_chars": self.lesson_chars,
            "ohlcv_horizon_bars": self.ohlcv_horizon_bars,
            "backtest_kalshi_limit": self.backtest_kalshi_limit,
            "backtest_polymarket_limit": self.backtest_polymarket_limit,
            "backtest_pools": self.backtest_pools,
            "paper_costs_note": (
                "Binary fee and slippage match the paper CostModel and are scoring "
                "assumptions, not a venue invoice. They do not move live orders."
            ),
        }


def agent_family(venue: str) -> str:
    lowered = venue.lower()
    if lowered.startswith("kalshi"):
        return "kalshi"
    if "polymarket" in lowered:
        return "polymarket_us"
    return lowered.replace(":", "_") or "unknown"
