"""
laya_config.py – Settings for the Laya severity-triage layer.

Every value comes from .env (or the process environment) and has a default, so
an existing .env with no LAYA_* keys keeps the agent exactly as it was:
LAYA_ENABLED defaults to false.

Modes:
  shadow    Laya runs and every decision is logged; nothing the agent does changes.
  advisory  Laya's verdicts go to Gemini as a prior and the paging-policy decisions
            appear in the report; Slack pages still follow today's behaviour.
  enforce   The paging policy decides whether a Slack page goes out. Falls back to
            advisory when the calibration file is missing.
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).parent

MODES  = ("shadow", "advisory", "enforce")
MODELS = ("english", "multilingual", "typed-decisions")

DEFAULTS = {
    "LAYA_ENABLED":          "false",
    "LAYA_MODE":             "shadow",
    "LAYA_MODEL":            "typed-decisions",
    "LAYA_BASE_URL":         "",
    "LAYA_API_KEY":          "",
    "LAYA_TIMEOUT_S":        "5",
    "LAYA_PAGE_HIGH":        "0.80",
    "LAYA_PAGE_LOW":         "0.40",
    "LAYA_CALIBRATION_FILE": "laya/calibration.json",
    "HARD_FLOOR_ERR_PCT":    "50",
    "CRITICAL_SERVICES":     "payment-service",
}


def _get(env, key: str) -> str:
    value = env.get(key)
    if value is None or not str(value).strip():
        return DEFAULTS[key]
    return str(value).strip()


def _as_bool(raw: str) -> bool:
    return raw.lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class LayaConfig:
    enabled: bool
    mode: str
    model: str
    base_url: str
    api_key: str
    timeout_s: float
    page_high: float
    page_low: float
    calibration_file: Path
    hard_floor_err_pct: float
    critical_services: tuple

    @property
    def calibrated(self) -> bool:
        return self.calibration_file.is_file()

    def effective_mode(self, warn: bool = True) -> str:
        """The mode actually applied: enforce needs a calibration file, else advisory."""
        if self.mode == "enforce" and not self.calibrated:
            if warn:
                print(
                    f"[Laya] WARNING: LAYA_MODE=enforce but {self.calibration_file} is missing. "
                    "Running as advisory until calibration is fitted (laya/training/calibrate.py).",
                    file=sys.stderr,
                )
            return "advisory"
        return self.mode

    def is_critical(self, service: str) -> bool:
        return service in self.critical_services


def load_config(env=None) -> LayaConfig:
    """Read the Laya settings. Values are validated only when LAYA_ENABLED is true,
    so a disabled layer can never break today's agent."""
    env = os.environ if env is None else env
    enabled = _as_bool(_get(env, "LAYA_ENABLED"))

    calibration = Path(_get(env, "LAYA_CALIBRATION_FILE"))
    if not calibration.is_absolute():
        calibration = PROJECT_ROOT / calibration

    cfg = LayaConfig(
        enabled=enabled,
        mode=_get(env, "LAYA_MODE").lower(),
        model=_get(env, "LAYA_MODEL").lower(),
        base_url=_get(env, "LAYA_BASE_URL").rstrip("/"),
        api_key=_get(env, "LAYA_API_KEY"),
        timeout_s=float(_get(env, "LAYA_TIMEOUT_S")),
        page_high=float(_get(env, "LAYA_PAGE_HIGH")),
        page_low=float(_get(env, "LAYA_PAGE_LOW")),
        calibration_file=calibration,
        hard_floor_err_pct=float(_get(env, "HARD_FLOOR_ERR_PCT")),
        critical_services=tuple(
            s.strip() for s in _get(env, "CRITICAL_SERVICES").split(",") if s.strip()
        ),
    )
    if enabled:
        _validate(cfg)
    return cfg


def _validate(cfg: LayaConfig) -> None:
    if cfg.mode not in MODES:
        raise ValueError(f"LAYA_MODE={cfg.mode!r}; choose one of {MODES}")
    if cfg.model not in MODELS:
        raise ValueError(f"LAYA_MODEL={cfg.model!r}; choose one of {MODELS}")
    if not 0.0 <= cfg.page_low <= cfg.page_high <= 1.0:
        raise ValueError(
            f"need 0 <= LAYA_PAGE_LOW ({cfg.page_low}) <= LAYA_PAGE_HIGH ({cfg.page_high}) <= 1"
        )
    if cfg.timeout_s <= 0:
        raise ValueError(f"LAYA_TIMEOUT_S must be positive, got {cfg.timeout_s}")


if __name__ == "__main__":
    c = load_config()
    print(c)
    print("effective mode:", c.effective_mode(warn=False))
