# Fine-tuning Laya for observability triage (Kaggle, 2× T4)

Training is not run locally. Laya's own notebook,
[`notebooks/laya_finetune_typed_decisions_2xT4_kaggle.ipynb`](https://github.com/NandhaKishorM/laya/blob/main/notebooks/laya_finetune_typed_decisions_2xT4_kaggle.ipynb),
does the whole loop on Kaggle's free GPUs: build items, DDP-train, fit temperatures and
save a checkpoint. It reads the `LocalLLaMA/typed-decisions` dataset from the Hub;
our generator writes the same row format, so only the data-loading lines change.

## 1. Generate the data

```bash
python -m laya_triage.training.generate_scenarios          # train 3000 / val 2500 / test 300, seed 7
```

The notebook needs `laya_triage/training/data/notebook_train.jsonl` (and `notebook_test.jsonl`
for its evaluation cell). Each row matches `LocalLLaMA/typed-decisions`:

| Field | Content |
|-------|---------|
| `id` | `observability_triage-train-00000` |
| `workflow` | `observability_triage` |
| `state` | JSON string of the banded evidence state Laya sees at runtime |
| `questions` | JSON string of the four questions in `laya_triage/triage_config.json` |
| `gold` | JSON string, `{qid: {"label": <option>, "probabilities": {<option>: 1.0 or 0.0}}}` |

The notebook's `build_training_item` reads exactly these keys: `gold[qid]["probabilities"]`
per option for `choice` questions, and `label` for evaluation.

## 2. Run it on Kaggle

1. Upload the two files as a private Kaggle dataset, e.g. `laya-observability-triage`.
2. Open the notebook on Kaggle. Settings: **Accelerator: GPU T4 ×2**, **Internet: On**.
   Add your dataset to the notebook.
3. Edit two cells:

   **Cell 6 (download and preprocess).** Start from the typed-decisions checkpoint rather
   than the base English one, and read our file instead of the Hub dataset:

   ```python
   MODEL_ID = "convaiinnovations/laya"
   model_dir = os.path.join(snapshot_download(MODEL_ID, allow_patterns=["typed-decisions/*"]),
                            "typed-decisions")
   _fix_tokenizer_config(model_dir)
   ...
   ds_train = load_dataset("json",
       data_files="/kaggle/input/laya-observability-triage/notebook_train.jsonl", split="train")
   ```

   (`build_sequence` then uses that checkpoint's `max_len` of 1024 tokens; keep it, our states
   are about 150 tokens.)

   **Cell 12 (evaluation).** Same change for the test split:

   ```python
   ds_test = load_dataset("json",
       data_files="/kaggle/input/laya-observability-triage/notebook_test.jsonl", split="train")
   ```

   Cell 14 computes per-workflow accuracy for the four benchmark workflows; our rows are all
   `observability_triage`, so read the overall numbers there and ignore the per-workflow table.

4. **Do not run cell 16** ("Push to Hugging Face Hub"). It publishes to
   `convaiinnovations/laya-typed-decisions`. To keep a copy on the Hub, change `NEW_REPO` to a
   private repo of your own first.
5. Run all other cells. Training takes a few minutes on 2× T4. The checkpoint lands in
   `/kaggle/working/laya_finetuned_typed_decisions/`: `model.safetensors`, `encoder/`,
   `tokenizer/`, `rl_agent_config.json` (with the temperatures the notebook fitted).

## 3. Use the checkpoint here

1. Download that folder from the notebook's Output tab to, for example,
   `models/laya-obs-triage/` (it is about 840 MB; keep it out of git).
2. Point the agent at it. This works in-process only; `laya-serve` can only serve the
   published checkpoints:

   ```
   LAYA_MODEL=typed-decisions
   LAYA_MODEL_PATH=models/laya-obs-triage
   LAYA_BASE_URL=
   ```

3. Re-fit our calibration for the new weights on the val split, then evaluate:

   ```bash
   python -m laya_triage.training.calibrate                      # -> laya_triage/training/out/calibration.json
   python -m laya_triage.training.evaluate --title "Laya fine-tuned" \
       --out-md docs/laya_eval_finetuned.md \
       --baseline docs/laya_eval_zero_shot.json --tolerance choice_accuracy=1.0
   ```

4. Only when the evaluation meets the agreed thresholds (SEV-1 recall, false-page rate),
   install the calibration so `LAYA_MODE=enforce` can take effect:

   ```bash
   cp laya_triage/training/out/calibration.json laya_triage/calibration.json
   ```

Keep the generator seed and the test split fixed between runs so `--baseline` compares the
same measurement (laya.evals refuses a comparison when the dataset or questions differ).
