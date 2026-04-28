"""Smoke tests for the LLM call sites in src/coach.py.

The Apr 28 silent-failure bug (`StopIteration` from `next(b.text for b in
response.content if b.type == "text")` when the model emitted only thinking
blocks) is the regression these tests are designed to catch. If you change
how `_extract_text` handles missing text blocks, run these.

Run with: `.venv/bin/python -m pytest tests/`
"""

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.coach import Coach, _extract_text
from src.config import DB_PATH, TRAINING_PLAN_PATH
from src.db import Database
from src.training_plan import TrainingPlan


def _block(btype: str, text: str | None = None) -> SimpleNamespace:
    """Stand-in for an Anthropic content block with .type and .text."""
    return SimpleNamespace(type=btype, text=text or "")


def _response(blocks: list, stop_reason: str = "end_turn") -> SimpleNamespace:
    """Stand-in for an anthropic.types.Message."""
    return SimpleNamespace(
        content=blocks,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=100, output_tokens=200),
    )


# ---------------- _extract_text ----------------


def test_extract_text_returns_text_block():
    resp = _response([_block("text", "Hello, runner.")])
    assert _extract_text(resp) == "Hello, runner."


def test_extract_text_skips_thinking_block_and_returns_text():
    """Sonnet 4.6 with adaptive thinking emits both blocks; we want the text one."""
    resp = _response([
        _block("thinking", "internal reasoning"),
        _block("text", "Solid easy run."),
    ])
    assert _extract_text(resp) == "Solid easy run."


def test_extract_text_raises_when_only_thinking_block_present():
    """The Apr 28 bug: max_tokens hit during thinking, no text block emitted.

    Old code raised `StopIteration` with `str(e) == ""`, producing blank
    error logs. The new helper must raise something with stop_reason and
    block types in the message so journalctl tells us what happened.
    """
    resp = _response([_block("thinking", "ran out of tokens here")], stop_reason="max_tokens")
    with pytest.raises(RuntimeError) as exc_info:
        _extract_text(resp)
    msg = str(exc_info.value)
    # The message must surface stop_reason so the operator can act on it
    assert "max_tokens" in msg
    assert "thinking" in msg


def test_extract_text_raises_on_empty_content():
    resp = _response([])
    with pytest.raises(RuntimeError):
        _extract_text(resp)


# ---------------- analyze_run integration smoke test ----------------


@pytest.fixture
def coach():
    """Real plan + DB, mocked Anthropic client. Coach reads from DB but never
    writes during analyze_run, so this is safe against prod data."""
    plan = TrainingPlan(str(TRAINING_PLAN_PATH))
    db = Database(DB_PATH)
    c = Coach(api_key="test-key", plan=plan, db=db)
    c.client = MagicMock()
    return c


def test_analyze_run_happy_path_returns_text(coach):
    """End-to-end: analyze_run builds a prompt, calls API, extracts text."""
    coach.client.messages.create.return_value = _response([
        _block("thinking", "..."),
        _block("text", "Verdict: solid run."),
    ])
    activity = {
        "start_time": "2026-04-28 09:23:45",
        "distance_km": 4.0,
        "duration_seconds": 1670,
        "avg_pace_min_km": "6:57",
        "avg_hr": 147,
        "max_hr": 165,
        "splits_json": "[]",
        "hr_zones_json": None,
    }
    result = coach.analyze_run(activity)
    assert result == "Verdict: solid run."
    assert coach.client.messages.create.called

    # Sanity-check the request shape that the production bug depended on
    call_kwargs = coach.client.messages.create.call_args.kwargs
    assert call_kwargs["max_tokens"] >= 4096, (
        "max_tokens must stay generous to leave headroom for adaptive thinking + text"
    )
    assert call_kwargs["thinking"] == {"type": "adaptive"}, (
        "budget_tokens is rejected by the API for adaptive — see Apr 28 regression"
    )


def test_analyze_run_propagates_no_text_failure(coach):
    """If the model returns only thinking, the failure must reach the caller
    (poller's outer try/except) instead of being silently swallowed."""
    coach.client.messages.create.return_value = _response(
        [_block("thinking", "...")], stop_reason="max_tokens"
    )
    activity = {
        "start_time": "2026-04-28 09:23:45",
        "distance_km": 4.0,
        "duration_seconds": 1670,
        "avg_pace_min_km": "6:57",
        "avg_hr": 147,
        "max_hr": 165,
        "splits_json": "[]",
        "hr_zones_json": None,
    }
    with pytest.raises(RuntimeError, match="max_tokens"):
        coach.analyze_run(activity)


def test_chat_returns_text_with_rich_context(coach):
    """chat() should call the API and return the text block."""
    coach.client.messages.create.return_value = _response([_block("text", "Sure thing.")])
    result = coach.chat("how am I tracking?")
    assert result == "Sure thing."

    # The user message should have a context prefix that includes adherence /
    # weekly target etc. (added in the Apr 28 rich-chat change).
    call_kwargs = coach.client.messages.create.call_args.kwargs
    last_user_msg = call_kwargs["messages"][-1]["content"]
    # Rich context prefix is bracketed; if it's missing, chat() lost its data.
    assert "[Context for this conversation" in last_user_msg or "Recent runs" in last_user_msg
