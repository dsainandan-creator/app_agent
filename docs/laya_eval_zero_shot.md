# Laya zero-shot baseline (typed-decisions)

- Checkpoint: `typed-decisions` (laya 0.3.22), device `cpu`
- Data: synthetic `test` split, 300 services x 4 questions (1200 decisions); labels from `laya_triage/training/RUBRIC.md`
- Time: checkpoint load 2.3 s, evaluation 125.6 s (419 ms per service, batch size 16); host arm64 / Darwin
- Calibration for the 'after' numbers: `/Users/saina/Desktop/app_agent/laya_triage/training/out/calibration.json` (fitted on 2500 val states)

## Accuracy and calibration

| Question | Options | Accuracy | Chance | ECE (shipped temps) | ECE (calibrated) |
|----------|---------|----------|--------|---------------------|------------------|
| severity | 4 | 49.0% | 25% | 0.138 | 0.100 |
| user_impact | 3 | 54.0% | 33% | 0.083 | 0.117 |
| failure_mode | 6 | 73.0% | 17% | 0.334 | 0.160 |
| page_now | 2 | 78.3% | 50% | 0.216 | 0.080 |

Overall: choice accuracy 63.6%, mean confidence 0.444, ECE 0.192 (shipped temperatures). Temperature scaling does not change which option wins, so accuracy is the same before and after calibration.

## Severity confusion matrix (rows: label, columns: Laya)

| label \ Laya | sev1 | sev2 | sev3 | sev4 |
|---|---|---|---|---|
| **sev1** | 0 | 56 | 1 | 0 |
| **sev2** | 0 | 84 | 12 | 0 |
| **sev3** | 0 | 7 | 61 | 0 |
| **sev4** | 0 | 1 | 76 | 2 |

## Paging at LAYA_PAGE_HIGH=0.8 / LAYA_PAGE_LOW=0.4

Laya's P(sev1) alone, before the hard floor, minimum volume or Gemini are applied.

| Probabilities | SEV-1 recall (P >= HIGH) | Missed SEV-1 (P < LOW) | False-page rate (non-SEV-1, P >= HIGH) | Non-SEV-1 held (P < LOW) |
|---|---|---|---|---|
| shipped | 0.0% | 100.0% | 0.0% | 100.0% |
| calibrated | 0.0% | 100.0% | 0.0% | 100.0% |

(57 SEV-1 and 243 other services in the test split.)

## Threshold sweep (shipped probabilities)

| t | SEV-1 recall (P >= t) | Missed SEV-1 (P < t) | False-page rate (P >= t) |
|---|---|---|---|
| 0.05 | 100.0% | 0.0% | 100.0% |
| 0.10 | 100.0% | 0.0% | 99.6% |
| 0.15 | 93.0% | 7.0% | 43.6% |
| 0.20 | 82.5% | 17.5% | 18.1% |
| 0.25 | 42.1% | 57.9% | 6.2% |
| 0.30 | 3.5% | 96.5% | 0.8% |
| 0.35 | 0.0% | 100.0% | 0.0% |
| 0.40 | 0.0% | 100.0% | 0.0% |
| 0.45 | 0.0% | 100.0% | 0.0% |
| 0.50 | 0.0% | 100.0% | 0.0% |
| 0.55 | 0.0% | 100.0% | 0.0% |
| 0.60 | 0.0% | 100.0% | 0.0% |
| 0.65 | 0.0% | 100.0% | 0.0% |
| 0.70 | 0.0% | 100.0% | 0.0% |
| 0.75 | 0.0% | 100.0% | 0.0% |
| 0.80 | 0.0% | 100.0% | 0.0% |
| 0.85 | 0.0% | 100.0% | 0.0% |
| 0.90 | 0.0% | 100.0% | 0.0% |
| 0.95 | 0.0% | 100.0% | 0.0% |

Suggested LAYA_PAGE_HIGH (lowest t with false-page rate <= 5%): **0.30** (SEV-1 recall 3.5%)

Suggested LAYA_PAGE_LOW (highest t with missed SEV-1 <= 5%): **0.10** (holds 0.4% of non-SEV-1 services below it)

## Threshold sweep (calibrated probabilities)

| t | SEV-1 recall (P >= t) | Missed SEV-1 (P < t) | False-page rate (P >= t) |
|---|---|---|---|
| 0.05 | 93.0% | 7.0% | 49.8% |
| 0.10 | 86.0% | 14.0% | 24.3% |
| 0.15 | 70.2% | 29.8% | 11.5% |
| 0.20 | 45.6% | 54.4% | 5.8% |
| 0.25 | 12.3% | 87.7% | 1.2% |
| 0.30 | 1.8% | 98.2% | 0.8% |
| 0.35 | 0.0% | 100.0% | 0.4% |
| 0.40 | 0.0% | 100.0% | 0.0% |
| 0.45 | 0.0% | 100.0% | 0.0% |
| 0.50 | 0.0% | 100.0% | 0.0% |
| 0.55 | 0.0% | 100.0% | 0.0% |
| 0.60 | 0.0% | 100.0% | 0.0% |
| 0.65 | 0.0% | 100.0% | 0.0% |
| 0.70 | 0.0% | 100.0% | 0.0% |
| 0.75 | 0.0% | 100.0% | 0.0% |
| 0.80 | 0.0% | 100.0% | 0.0% |
| 0.85 | 0.0% | 100.0% | 0.0% |
| 0.90 | 0.0% | 100.0% | 0.0% |
| 0.95 | 0.0% | 100.0% | 0.0% |

Suggested LAYA_PAGE_HIGH (lowest t with false-page rate <= 5%): **0.25** (SEV-1 recall 12.3%)

Suggested LAYA_PAGE_LOW (highest t with missed SEV-1 <= 5%): **none meets it**
