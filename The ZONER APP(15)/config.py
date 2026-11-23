import os
from dataclasses import dataclass, asdict
from dotenv import load_dotenv

load_dotenv()

RISK_PRESETS = {
    "low": {
        "label": "1. Small % Gains / Low Risk",
        "scalp_take_profit_pct": 0.06,
        "scalp_stop_loss_pct": 0.03,
        "scalp_trailing_stop_pct": 0.025,
        "scalp_breakeven_pct": 0.03,
        "scalp_max_hold_sec": 180,
        "kelly_fraction": 0.15,
        "min_ev_edge": 0.04,
        "min_ask_depth_shares": 50,
        "predictor_min_score": 0.58,
    },
    "moderate": {
        "label": "2. Moderate Gain & Risk",
        "scalp_take_profit_pct": 0.10,
        "scalp_stop_loss_pct": 0.05,
        "scalp_trailing_stop_pct": 0.04,
        "scalp_breakeven_pct": 0.05,
        "scalp_max_hold_sec": 300,
        "kelly_fraction": 0.25,
        "min_ev_edge": 0.03,
        "min_ask_depth_shares": 35,
        "predictor_min_score": 0.54,
    },
    "high": {
        "label": "3. Higher % Gains / Higher Risk",
        "scalp_take_profit_pct": 0.18,
        "scalp_stop_loss_pct": 0.09,
        "scalp_trailing_stop_pct": 0.06,
        "scalp_breakeven_pct": 0.08,
        "scalp_max_hold_sec": 600,
        "kelly_fraction": 0.35,
        "min_ev_edge": 0.02,
        "min_ask_depth_shares": 20,
        "predictor_min_score": 0.51,
    },
    "micro": {
        "label": "4. Micro-Scalp / Ultra-Safe",
        "scalp_take_profit_pct": 0.04,
        "scalp_stop_loss_pct": 0.15,
        "scalp_trailing_stop_pct": 0.015,
        "scalp_breakeven_pct": 0.02,
        "scalp_max_hold_sec": 90,
        "kelly_fraction": 0.10,
        "min_ev_edge": 0.05,
        "min_ask_depth_shares": 60,
        "predictor_min_score": 0.62,
    },
}


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class BotConfig:
    polymarket_key_id: str = ""
    polymarket_secret_key: str = ""
    dry_run: bool = False
    target_timeframe: str = "all"
    risk_profile: str = "moderate"

    # Sizing & Growth
    stake_usd: float = 5.0
    max_stake_usd: float = 25.0
    kelly_fraction: float = 0.25
    min_ev_edge: float = 0.03

    # Pre-Trade Sell Triggers
    scalp_enabled: bool = True
    scalp_take_profit_pct: float = 0.10
    scalp_stop_loss_pct: float = 0.05
    scalp_trailing_stop_pct: float = 0.04
    scalp_breakeven_pct: float = 0.05
    scalp_min_hold_sec: int = 8
    scalp_max_hold_sec: int = 300

    # Safety Guardrails
    max_daily_loss_usd: float = 25.0
    anti_greed_profit_usd: float = 40.0
    profit_lock_pct: float = 0.50
    max_open_positions: int = 3
    max_consecutive_losses: int = 3
    max_drawdown_pct: float = 0.20

    # Market & Book Filters
    predictor_min_score: float = 0.54
    min_spot_move_pct: float = 0.0008
    max_spread: float = 0.06
    min_ask_depth_shares: int = 35
    min_seconds_left: int = 30

    @classmethod
    def from_env(cls) -> "BotConfig":
        return cls(
            polymarket_key_id=os.getenv("POLYMARKET_KEY_ID", "").strip(),
            polymarket_secret_key=os.getenv("POLYMARKET_SECRET_KEY", "").strip(),
            dry_run=_env_bool("DRY_RUN", False),
            target_timeframe=os.getenv("TARGET_TIMEFRAME", "all").strip().lower(),
        )

    def keys_configured(self) -> bool:
        return bool(self.polymarket_key_id and self.polymarket_secret_key)

    def apply_preset(self, profile_key: str) -> bool:
        preset = RISK_PRESETS.get(profile_key)
        if not preset:
            return False
        self.risk_profile = profile_key
        for k, v in preset.items():
            if k != "label" and hasattr(self, k):
                setattr(self, k, v)
        return True

    def to_public_dict(self) -> dict:
        d = asdict(self)
        d.pop("polymarket_key_id", None)
        d.pop("polymarket_secret_key", None)
        return d
