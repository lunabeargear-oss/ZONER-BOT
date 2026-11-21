import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


def _parse_bool(val, default: bool = False) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _load_env_fallback(env_path: str):
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


@dataclass
class BotConfig:
    polymarket_key_id: str = ""
    polymarket_secret_key: str = ""
    dry_run: bool = False
    target_timeframe: str = "auto"

    # Position sizing & positive-gain money scaling
    sizing_mode: str = "scale_gains"  # "scale_gains" | "pct_bankroll" | "flat"
    stake_usd: float = 5.0
    max_stake_usd: float = 25.0
    bankroll_pct: float = 0.03
    gain_scale_step_usd: float = 10.0
    gain_scale_mult: float = 0.25
    starting_bankroll_usd: float = 0.0

    # Entry & risk gates
    predictor_min_score: float = 0.50
    min_spot_move_pct: float = 0.0012
    max_spread: float = 0.04
    min_ask_depth_shares: int = 50
    min_seconds_left: int = 25
    max_daily_loss_usd: float = 50.0
    max_open_positions: int = 10
    max_consecutive_losses: int = 3

    # Scalp exit rules
    scalp_enabled: bool = True
    scalp_take_profit_pct: float = 0.10
    scalp_stop_loss_pct: float = 0.05
    scalp_trailing_stop_pct: float = 0.04
    scalp_min_hold_sec: int = 8

    @classmethod
    def from_env(cls, env_path: str = None) -> "BotConfig":
        if env_path is None:
            env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if load_dotenv is not None and os.path.exists(env_path):
            load_dotenv(env_path, override=False)
        else:
            _load_env_fallback(env_path)

        return cls(
            polymarket_key_id=os.getenv("POLYMARKET_KEY_ID", "").strip(),
            polymarket_secret_key=os.getenv("POLYMARKET_SECRET_KEY", "").strip(),
            dry_run=_parse_bool(os.getenv("DRY_RUN", "false"), False),
            target_timeframe=os.getenv("TARGET_TIMEFRAME", "auto").strip() or "auto",
            sizing_mode=os.getenv("SIZING_MODE", "scale_gains").strip() or "scale_gains",
            stake_usd=float(os.getenv("STAKE_USD", "5.0")),
            max_stake_usd=float(os.getenv("MAX_STAKE_USD", "25.0")),
            bankroll_pct=float(os.getenv("BANKROLL_PCT", "0.03")),
            gain_scale_step_usd=float(os.getenv("GAIN_SCALE_STEP_USD", "10.0")),
            gain_scale_mult=float(os.getenv("GAIN_SCALE_MULT", "0.25")),
            starting_bankroll_usd=float(os.getenv("STARTING_BANKROLL_USD", "0.0")),
            predictor_min_score=float(os.getenv("PREDICTOR_MIN_SCORE", "0.50")),
            min_spot_move_pct=float(os.getenv("MIN_SPOT_MOVE_PCT", "0.0012")),
            max_spread=float(os.getenv("MAX_SPREAD", "0.04")),
            min_ask_depth_shares=int(os.getenv("MIN_ASK_DEPTH_SHARES", "50")),
            min_seconds_left=int(os.getenv("MIN_SECONDS_LEFT", "25")),
            max_daily_loss_usd=float(os.getenv("MAX_DAILY_LOSS_USD", "50.0")),
            max_open_positions=int(os.getenv("MAX_OPEN_POSITIONS", "10")),
            max_consecutive_losses=int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3")),
            scalp_enabled=_parse_bool(os.getenv("SCALP_ENABLED", "true"), True),
            scalp_take_profit_pct=float(os.getenv("SCALP_TAKE_PROFIT_PCT", "0.10")),
            scalp_stop_loss_pct=float(os.getenv("SCALP_STOP_LOSS_PCT", "0.05")),
            scalp_trailing_stop_pct=float(os.getenv("SCALP_TRAILING_STOP_PCT", "0.04")),
            scalp_min_hold_sec=int(os.getenv("SCALP_MIN_HOLD_SEC", "8")),
        )

    def keys_configured(self) -> bool:
        return bool(self.polymarket_key_id and self.polymarket_secret_key)
