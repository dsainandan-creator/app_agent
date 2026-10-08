import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _no_real_side_effects(monkeypatch):
    """Belt and braces for every test: no Slack webhook, no Gemini key."""
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    import slack_notify
    monkeypatch.setattr(slack_notify, "SLACK_WEBHOOK_URL", "")

