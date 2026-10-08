# Labelling rubric for synthetic triage scenarios

`generate_scenarios.py` labels every synthetic service with this rubric. It is
derived from the agent's `SYSTEM_PROMPT` (severity definitions, the "drive
severity from the more alarming signal" correlation rule, service criticality)
and from `dynatrace-alerts.txt`. The labels are **synthetic**: they encode these
rules, not on-call engineers' judgement, so a model trained on them can at best
learn the rules.

The thresholds intentionally line up with the band edges in
`laya_triage/triage_config.json`, so a label is recoverable from what Laya sees.

## Inputs (raw numbers from the scenario)

| Symbol | Meaning |
|--------|---------|
| `e_pg` | PostgreSQL 5xx rate, % |
| `e_dt` | Dynatrace 5xx rate, % (absent when Dynatrace is unavailable) |
| `e`    | Effective 5xx rate: `max(e_pg, e_dt)` when Dynatrace is `DT-higher`, else `e_pg` (PostgreSQL is authoritative; a lower Dynatrace rate is treated as ingestion lag) |
| `L`    | PostgreSQL average latency, ms |
| `c`    | PostgreSQL 4xx rate, % |
| `n`    | PostgreSQL requests in the window |
| `crit` | Service is in `CRITICAL_SERVICES` (default `payment-service`) |

## severity

Checked top to bottom; the first match wins.

| Label | Rule |
|-------|------|
| `sev1` | `crit` and `e >= 50` (the hard floor, any volume) |
| `sev2` | `n < 20` and the volume-free rules below would give `sev1` (rates on very low volume are too noisy to page on) |
| `sev1` | `e >= 30`; or `L >= 2000` and `e >= 5`; or `crit` and `e >= 15` |
| `sev2` | `e >= 15`; or `L >= 1000`; or `crit` and `e >= 5` |
| `sev3` | `e >= 5`; or `L >= 300`; or `c >= 30` |
| `sev4` | otherwise |

## page_now

`A` (yes, wake someone now) exactly when `severity` is `sev1`; otherwise `B`.

## user_impact

| Label | Rule |
|-------|------|
| `widespread` | `e >= 30` or `L >= 2000` |
| `partial` | `e >= 5` or `L >= 1000` or `c >= 30` |
| `minimal` | otherwise |

## failure_mode

The failure mode the generator injected (it also chooses the matching
`error_detail` texts): `db_pool`, `upstream_timeout`, `payment_gateway`, `auth`
or `resource_exhaustion`. A service with `e < 1` (no meaningful 5xx) is labelled
`other`, as is the generator's own `other` mode. `payment_gateway` is only
injected for `payment-service`.
