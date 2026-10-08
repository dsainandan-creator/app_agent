# Running laya-serve on port 8100

By default the triage layer loads Laya in-process. To run it as a separate HTTP
service instead, start `laya-serve` and point `LAYA_BASE_URL` at it.

`laya-serve` has no command-line flags: it is configured entirely through
environment variables (see the docstring of `laya/serve.py` in the installed
package). Its default port, 8000, collides with the mock app, so set `LAYA_PORT`.

```bash
source .venv/bin/activate
USE_TF=0 \
LAYA_PORT=8100 \
LAYA_HOST=127.0.0.1 \
LAYA_MODELS=typed-decisions \
LAYA_MAX_LOADED=1 \
laya-serve
```

| Variable | Why |
|----------|-----|
| `LAYA_PORT=8100` | Avoids the mock app on 8000 |
| `LAYA_HOST=127.0.0.1` | Default is `0.0.0.0` (all interfaces); keep it local |
| `LAYA_MODELS=typed-decisions` | Preload only the checkpoint we use (default preloads all three, ~2.5 GB) |
| `LAYA_MAX_LOADED=1` | Keep one checkpoint resident |
| `LAYA_API_KEY=<secret>` | Optional. If set, clients must send `Authorization: Bearer <secret>`; put the same value in our `LAYA_API_KEY` |
| `USE_TF=0` | Laya's README: avoids a hang when TensorFlow is installed |

Check it:

```bash
curl -s http://127.0.0.1:8100/health
```

Then in `.env`:

```
LAYA_BASE_URL=http://localhost:8100
```

Endpoints used by `laya/client.py`: `POST /v1/systemone/batch`
(`{"model", "states", "questions"}` → `{"results": [...]}`), with
`POST /v1/systemone` as the single-request form.

**Calibration over HTTP.** `laya-serve` applies the checkpoint's own temperatures
and has no setting for a calibration file. When `laya/calibration.json` exists,
`laya/client.py` re-scales the returned probabilities on the client. This gives
exactly `softmax(z / T_fitted)`, because the server returned `softmax(z / T_shipped)`
and the calibration file records both temperatures.
