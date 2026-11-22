import os
from dataclasses import dataclass, asdict

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


def _parse_bool(val, default=False):
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _load_env_fallback(env_path):
    if not env_path or not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


RISK_PROFILES = {
    "conservative": {
        "label": "Small % Gains / Low Risk",
        "description": "Tight stop-loss, modest profit target, strict entry filters, and low daily drawdown.",
        "sizing_mode": "pct_bankroll",
        "stake_usd": 3.0,
        "max_stake_usd": 10.0,
        "bankroll_pct": 0.015,
        "scalp_take_profit_pct": 0.06,
        "scalp_stop_loss_pct": 0.03,
        "scalp_trailing_stop_pct": 0.025,
        "scalp_max_hold_sec": 180,
        "scalp_min_hold_sec": 8,
        "predictor_min_score": 0.65,
        "min_spot_move_pct": 0.0015,
        "max_spread": 0.03,
        "min_ask_depth_shares": 75,
        "min_seconds_left": 45,
        "max_daily_loss_usd": 20.0,
        "daily_profit_target_usd": 25.0,
        "max_open_positions": 3,
        "max_consecutive_losses": 2,
        "max_drawdown_pct": 0.10,
    },
    "moderate": {
        "label": "Moderate Gain & Risk",
        "description": "Balanced passive-income scalping with 2:1 reward-to-risk ratio and trailing lock.",
        "sizing_mode": "scale_gains",
        "stake_usd": 5.0,
        "max_stake_usd": 25.0,
        "bankroll_pct": 0.03,
        "scalp_take_profit_pct": 0.10,
        "scalp_stop_loss_pct": 0.05,
        "scalp_trailing_stop_pct": 0.04,
        "scalp_max_hold_sec": 300,
        "scalp_min_hold_sec": 8,
        "predictor_min_score": 0.55,
        "min_spot_move_pct": 0.0012,
        "max_spread": 0.04,
        "min_ask_depth_shares": 50,
        "min_seconds_left": 30,
        "max_daily_loss_usd": 50.0,
        "daily_profit_target_usd": 60.0,
        "max_open_positions": 5,
        "max_consecutive_losses": 3,
        "max_drawdown_pct": 0.15,
    },
    "aggressive": {
        "label": "Higher % Gains / Higher Risk",
        "description": "Wider stops and larger profit targets to capture bigger BTC swings across timeframes.",
        "sizing_mode": "scale_gains",
        "stake_usd": 10.0,
        "max_stake_usd": 50.0,
        "bankroll_pct": 0.06,
        "scalp_take_profit_pct": 0.18,
        "scalp_stop_loss_pct": 0.09,
        "scalp_trailing_stop_pct": 0.06,
        "scalp_max_hold_sec": 600,
        "scalp_min_hold_sec": 6,
        "predictor_min_score": 0.45,
        "min_spot_move_pct": 0.0008,
        "max_spread": 0.05,
        "min_ask_depth_shares": 30,
        "min_seconds_left": 20,
        "max_daily_loss_usd": 100.0,
        "daily_profit_target_usd": 150.0,
        "max_open_positions": 8,
        "max_consecutive_losses": 4,
        "max_drawdown_pct": 0.25,
    },
    "micro_scalp": {
        "label": "Micro-Scalp / Ultra-Safe",
        "description": "Fast in-and-out 4% scalps with 2% hard stop, 90s max hold, and strictest liquidity gates.",
        "sizing_mode": "flat",
        "stake_usd": 2.0,
        "max_stake_usd": 8.0,
        "bankroll_pct": 0.01,
        "scalp_take_profit_pct": 0.04,
        "scalp_stop_loss_pct": 0.02,
        "scalp_trailing_stop_pct": 0.015,
        "scalp_max_hold_sec": 90,
        "scalp_min_hold_sec": 5,
        "predictor_min_score": 0.70,
        "min_spot_move_pct": 0.0018,
        "max_spread": 0.025,
        "min_ask_depth_shares": 100,
        "min_seconds_left": 45,
        "max_daily_loss_usd": 15.0,
        "daily_profit_target_usd": 15.0,
        "max_open_positions": 2,
        "max_consecutive_losses": 2,
        "max_drawdown_pct": 0.08,
    },
}


@dataclass
class BotConfig:
    polymarket_key_id: str = ""
    polymarket_secret_key: str = ""
    dry_run: bool = False
    target_timeframe: str = "all"
    risk_profile: str = "moderate"
    auto_scalp_enabled: bool = True
    scalp_enabled: bool = True
    require_pre_trade_triggers: bool = True
    sizing_mode: str = "scale_gains"
    stake_usd: float = 5.0
    max_stake_usd: float = 25.0
    bankroll_pct: float = 0.03
    gain_scale_step_usd: float = 10.0
    gain_scale_mult: float = 0.25
    starting_bankroll_usd: float = 0.0
    predictor_min_score: float = 0.55
    min_spot_move_pct: float = 0.0012
    max_spread: float = 0.04
    min_ask_depth_shares: int = 50
    min_seconds_left: int = 30
    max_daily_loss_usd: float = 50.0
    daily_profit_target_usd: float = 60.0
    max_open_positions: int = 5
    max_consecutive_losses: int = 3
    max_drawdown_pct: float = 0.15
    scalp_take_profit_pct: float = 0.10
    scalp_stop_loss_pct: float = 0.05
    scalp_trailing_stop_pct: float = 0.04
    scalp_min_hold_sec: int = 8
    scalp_max_hold_sec: int = 300
    pre_expiry_exit_sec: int = 15

    @classmethod
    def from_env(cls, env_path=None):
        if env_path:
            if load_dotenv:
                load_dotenv(env_path)
            else:
                _load_env_fallback(env_path)
        elif load_dotenv:
            load_dotenv()

        return cls(
            polymarket_key_id=os.environ.get("POLYMARKET_KEY_ID", "").strip(),
            polymarket_secret_key=os.environ.get("POLYMARKET_SECRET_KEY", "").strip(),
            dry_run=_parse_bool(os.environ.get("DRY_RUN"), False),
            target_timeframe=os.environ.get("TARGET_TIMEFRAME", "all").strip().lower() or "all",
            risk_profile=os.environ.get("RISK_PROFILE", "moderate").strip().lower() or "moderate",
            auto_scalp_enabled=_parse_bool(os.environ.get("AUTO_SCALP_ENABLED"), True),
            scalp_enabled=_parse_bool(os.environ.get("SCALP_ENABLED"), True),
            require_pre_trade_triggers=_parse_bool(os.environ.get("REQUIRE_PRE_TRADE_TRIGGERS"), True),
            sizing_mode=os.environ.get("SIZING_MODE", "scale_gains").strip(),
            stake_usd=float(os.environ.get("STAKE_USD", 5.0)),
            max_stake_usd=float(os.environ.get("MAX_STAKE_USD", 25.0)),
            bankroll_pct=float(os.environ.get("BANKROLL_PCT", 0.03)),
            gain_scale_step_usd=float(os.environ.get("GAIN_SCALE_STEP_USD", 10.0)),
            gain_scale_mult=float(os.environ.get("GAIN_SCALE_MULT", 0.25)),
            starting_bankroll_usd=float(os.environ.get("STARTING_BANKROLL_USD", 0.0)),
            predictor_min_score=float(os.environ.get("PREDICTOR_MIN_SCORE", 0.55)),
            min_spot_move_pct=float(os.environ.get("MIN_SPOT_MOVE_PCT", 0.0012)),
            max_spread=float(os.environ.get("MAX_SPREAD", 0.04)),
            min_ask_depth_shares=int(os.environ.get("MIN_ASK_DEPTH_SHARES", 50)),
            min_seconds_left=int(os.environ.get("MIN_SECONDS_LEFT", 30)),
            max_daily_loss_usd=float(os.environ.get("MAX_DAILY_LOSS_USD", 50.0)),
            daily_profit_target_usd=float(os.environ.get("DAILY_PROFIT_TARGET_USD", 60.0)),
            max_open_positions=int(os.environ.get("MAX_OPEN_POSITIONS", 5)),
            max_consecutive_losses=int(os.environ.get("MAX_CONSECUTIVE_LOSSES", 3)),
            max_drawdown_pct=float(os.environ.get("MAX_DRAWDOWN_PCT", 0.15)),
            scalp_take_profit_pct=float(os.environ.get("SCALP_TAKE_PROFIT_PCT", 0.10)),
            scalp_stop_loss_pct=float(os.environ.get("SCALP_STOP_LOSS_PCT", 0.05)),
            scalp_trailing_stop_pct=float(os.environ.get("SCALP_TRAILING_STOP_PCT", 0.04)),
            scalp_min_hold_sec=int(os.environ.get("SCALP_MIN_HOLD_SEC", 8)),
            scalp_max_hold_sec=int(os.environ.get("SCALP_MAX_HOLD_SEC", 300)),
            pre_expiry_exit_sec=int(os.environ.get("PRE_EXPIRY_EXIT_SEC", 15)),
        )

    def apply_risk_profile(self, profile_name: str) -> bool:
        key = str(profile_name or "").strip().lower()
        if key not in RISK_PROFILES:
            return False
        preset = RISK_PROFILES[key]
        self.risk_profile = key
        for field_name, val in preset.items():
            if hasattr(self, field_name):
                setattr(self, field_name, val)
        return True

    def keys_configured(self) -> bool:
        return bool(self.polymarket_key_id and self.polymarket_secret_key)

    def to_safe_dict(self) -> dict:
        data = asdict(self)
        data["polymarket_key_id"] = (
            f"{self.polymarket_key_id[:8]}..." if self.polymarket_key_id else ""
        )
        data["polymarket_secret_key"] = "***" if self.polymarket_secret_key else ""
        data["keys_configured"] = self.keys_configured()
        return data
