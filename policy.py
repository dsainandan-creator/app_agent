"""
policy.py – Deterministic paging policy (Laya + Gemini + safety rules).

decide() is pure: it takes whether Gemini asked to page, Laya's triage result for
the service, its evidence pack and the config, and returns the outcome. Rules, in
order:

  1. Hard floor   service in CRITICAL_SERVICES, Postgres 5xx >= HARD_FLOOR_ERR_PCT,
                  and Dynatrace agrees (agreement / DT-higher) or is unavailable
                  -> always page: PAGE if Gemini paged, PAGE_FORCED if not.
  2. Min volume   fewer than PAGE_MIN_REQUESTS Postgres requests in the window
                  -> never page: PAGE_HELD if Gemini paged, NO_PAGE if not.
  3. Laya unavailable
                  -> today's behaviour: PAGE if Gemini paged, NO_PAGE if not.
  4. Laya P(sev1) >= LAYA_PAGE_HIGH
                  -> PAGE if Gemini paged; PAGE_FORCED if not.
     LAYA_PAGE_LOW <= P(sev1) < LAYA_PAGE_HIGH
                  -> PAGE ("model disagreement") if Gemini paged; NO_PAGE if not.
     P(sev1) < LAYA_PAGE_LOW
                  -> PAGE_HELD if Gemini paged; NO_PAGE if not.

Modes: shadow and advisory only record the decision (alert_type POLICY_SHADOW);
enforce makes it happen (PAGE / PAGE_FORCED send a page, PAGE_HELD does not).
"""

from dataclasses import asdict, dataclass
from typing import Optional

PAGE        = "PAGE"
PAGE_FORCED = "PAGE_FORCED"
PAGE_HELD   = "PAGE_HELD"
NO_PAGE     = "NO_PAGE"

SENDS_PAGE = (PAGE, PAGE_FORCED)

# Dynatrace "agrees" for the hard floor: it sees at least as many errors, or has no data.
HARD_FLOOR_DT_OK = ("agreement", "DT-higher", "DT-unavailable")


@dataclass(frozen=True)
class Decision:
    outcome: str
    reason: str
    rule: str
    gemini_paged: bool
    laya_p_sev1: Optional[float]
    hard_floor: bool
    below_min_volume: bool

    @property
    def sends_page(self) -> bool:
        return self.outcome in SENDS_PAGE

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        p = "n/a" if self.laya_p_sev1 is None else f"{self.laya_p_sev1:.2f}"
        return (f"{self.outcome} ({self.reason}) | Laya P(sev1)={p} | "
                f"Gemini paged={'yes' if self.gemini_paged else 'no'}")


def is_hard_floor(pack: dict, cfg) -> bool:
    pg = (pack.get("raw") or {}).get("pg") or {}
    resolution = (pack.get("source") or {}).get("resolution", "DT-unavailable")
    return (
        pack.get("service") in cfg.critical_services
        and float(pg.get("error_rate_pct") or 0.0) >= cfg.hard_floor_err_pct
        and resolution in HARD_FLOOR_DT_OK
    )


def is_below_min_volume(pack: dict, cfg) -> bool:
    pg = (pack.get("raw") or {}).get("pg") or {}
    return int(pg.get("total_requests") or 0) < cfg.page_min_requests


def decide(gemini_pages: bool, laya: Optional[dict], pack: dict, cfg) -> Decision:
    """
    gemini_pages: Gemini called call_on_call_engineer for this service.
    laya:         the service's normalised Laya result (client.normalise_answers
                  output), or None when Laya was unavailable for it.
    pack:         the service's evidence pack (evidence.build_evidence_packs).
    cfg:          laya_config.LayaConfig.
    """
    p_sev1 = None if laya is None else float(laya["p"]["sev1"])
    floor = is_hard_floor(pack, cfg)
    low_volume = is_below_min_volume(pack, cfg)

    def d(outcome, reason, rule):
        return Decision(outcome, reason, rule, bool(gemini_pages), p_sev1, floor, low_volume)

    if floor:
        pg_err = pack["raw"]["pg"]["error_rate_pct"]
        reason = (f"hard floor: critical service at {pg_err}% 5xx "
                  f"(>= {cfg.hard_floor_err_pct:g}%), Dynatrace {pack['source']['resolution']}")
        if gemini_pages:
            return d(PAGE, reason, "hard_floor")
        return d(PAGE_FORCED, reason + "; Gemini did not page", "hard_floor")

    if low_volume:
        n = pack["raw"]["pg"]["total_requests"]
        reason = f"below minimum volume: {n} requests < PAGE_MIN_REQUESTS={cfg.page_min_requests}"
        if gemini_pages:
            return d(PAGE_HELD, reason + "; held page, needs human review", "min_volume")
        return d(NO_PAGE, reason, "min_volume")

    if p_sev1 is None:
        if gemini_pages:
            return d(PAGE, "Laya unavailable", "laya_unavailable")
        return d(NO_PAGE, "Laya unavailable; Gemini did not page", "laya_unavailable")

    if p_sev1 >= cfg.page_high:
        if gemini_pages:
            return d(PAGE, f"models agree: Laya P(sev1) >= {cfg.page_high:g}", "laya_high")
        return d(PAGE_FORCED, f"Laya P(sev1) >= {cfg.page_high:g}; Gemini did not page", "laya_high")

    if p_sev1 >= cfg.page_low:
        if gemini_pages:
            return d(PAGE, "model disagreement: Laya P(sev1) between "
                           f"{cfg.page_low:g} and {cfg.page_high:g}", "laya_mid")
        return d(NO_PAGE, "Laya P(sev1) below LAYA_PAGE_HIGH; Gemini did not page", "laya_mid")

    if gemini_pages:
        return d(PAGE_HELD, f"Laya P(sev1) < {cfg.page_low:g}: held page, needs human review", "laya_low")
    return d(NO_PAGE, f"Laya P(sev1) < {cfg.page_low:g}; Gemini did not page", "laya_low")
