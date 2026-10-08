"""agent._BoundedChat / _send_with_retry: a stalled Gemini call is abandoned and retried,
without corrupting the conversation history. Gemini is faked; no network."""

import asyncio
import threading
import time

import pytest
from google.genai import types

import agent


def _response(text):
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=[types.Part(text=text)]))])


class FakeModels:
    def __init__(self, plan):
        self.plan = list(plan)          # per call: ("ok", text) or ("stall", seconds)
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append(list(contents))
        kind, value = self.plan.pop(0)
        if kind == "stall":
            time.sleep(value)
            return _response("late reply that must be ignored")
        return _response(value)


class FakeClient:
    def __init__(self, plan):
        self.models = FakeModels(plan)


def test_success_advances_history():
    chat = agent._BoundedChat(FakeClient([("ok", "a1"), ("ok", "a2")]), "m", None)
    chat.send_message("q1")
    chat.send_message([types.Part(text="tool result")])
    texts = [c.parts[0].text for c in chat.history]
    assert texts == ["q1", "a1", "tool result", "a2"]
    assert [c.role for c in chat.history] == ["user", "model", "user", "model"]


def test_stall_times_out_and_late_reply_does_not_touch_history():
    chat = agent._BoundedChat(FakeClient([("ok", "a1"), ("stall", 0.5)]), "m", None)
    chat.send_message("q1")
    with pytest.raises(TimeoutError):
        chat.send_message("q2", timeout_s=0.05)
    time.sleep(0.7)                                  # the abandoned call has now returned
    assert [c.parts[0].text for c in chat.history] == ["q1", "a1"]


def test_send_with_retry_recovers_from_a_stall(monkeypatch):
    monkeypatch.setattr(agent, "GEMINI_CALL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(agent._BoundedChat.send_message, "__defaults__", (0.05,))
    sleeps = []

    async def no_sleep(s):
        sleeps.append(s)
    monkeypatch.setattr(agent.asyncio, "sleep", no_sleep)

    client = FakeClient([("stall", 0.3), ("ok", "answer")])
    chat = agent._BoundedChat(client, "m", None)
    resp = asyncio.run(agent._send_with_retry(chat, "question"))
    assert resp.candidates[0].content.parts[0].text == "answer"
    assert len(client.models.calls) == 2
    assert client.models.calls[0] == client.models.calls[1]          # same request resent
    assert [c.parts[0].text for c in chat.history] == ["question", "answer"]
    assert sleeps == [5]


def test_send_with_retry_gives_up_after_bounded_retries(monkeypatch):
    monkeypatch.setattr(agent._BoundedChat.send_message, "__defaults__", (0.02,))

    async def no_sleep(s):
        pass
    monkeypatch.setattr(agent.asyncio, "sleep", no_sleep)
    chat = agent._BoundedChat(FakeClient([("stall", 0.2)] * 3), "m", None)
    with pytest.raises(TimeoutError):
        asyncio.run(agent._send_with_retry(chat, "q"))


class _Replay:
    """Minimal stand-in for google-genai's ReplayResponse (what APIError reads)."""
    def __init__(self, body):
        self.body_segments = [body]


def test_rate_limit_retry_unchanged(monkeypatch):
    from google.genai.errors import ClientError
    calls = []

    class RateLimitedOnce:
        def send_message(self, message):
            calls.append(message)
            if len(calls) == 1:
                raise ClientError(429, _Replay({"error": {"code": 429, "message": "quota",
                                                          "status": "RESOURCE_EXHAUSTED"}}))
            return "ok"

    async def no_sleep(s):
        pass
    monkeypatch.setattr(agent.asyncio, "sleep", no_sleep)
    assert asyncio.run(agent._send_with_retry(RateLimitedOnce(), "q")) == "ok"
    assert len(calls) == 2


def test_daemon_thread_does_not_block_exit():
    with pytest.raises(TimeoutError):
        agent._call_with_deadline(lambda: time.sleep(5), 0.01)
    stuck = [t for t in threading.enumerate() if t.name == "gemini-call"]
    assert stuck and all(t.daemon for t in stuck)
