"""Tests for the retry behaviour around send_coaching_message and
send_backup_to_telegram in src/telegram_bot.py.

The functions wrap async Telegram calls in `asyncio.run`. The retry happens at
the sync boundary, so we mock `asyncio.run` (referenced from the module) to
control success / failure on each attempt.

Every fake closes the coroutine it receives — both on success AND on raise —
so pytest doesn't emit "coroutine was never awaited" RuntimeWarnings.

Two contracts to verify:
- send_coaching_message: retries 3x, RE-RAISES on final failure (so the
  poller's send_error_alert triggers — missed analyses must stay visible)
- send_backup_to_telegram: retries 3x, returns None on final failure
  (best-effort — daily backup will catch up)

Run: .venv/bin/python -m pytest tests/test_telegram_retry.py -v
"""

from pathlib import Path

import pytest

from src import telegram_bot


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    """No actual backoff sleeps — keeps the test suite snappy."""
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)


def _make_run(behaviours):
    """Build a fake asyncio.run that closes the incoming coroutine on every
    call (success or failure) and follows a per-attempt behaviour list.

    `behaviours` is a list of either None (succeed, return None) or an
    Exception instance (raise it). The fake records its own call count.
    """
    state = {"n": 0}

    def fake_run(coro):
        # Always close the coroutine to silence "never awaited" warnings
        try:
            coro.close()
        except Exception:
            pass
        state["n"] += 1
        idx = state["n"] - 1
        outcome = behaviours[idx] if idx < len(behaviours) else behaviours[-1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    fake_run.calls = state  # expose for assertions
    return fake_run


# ---------------- send_coaching_message ----------------


def test_send_coaching_succeeds_on_first_attempt(monkeypatch):
    fake = _make_run([None])
    monkeypatch.setattr(telegram_bot.asyncio, "run", fake)
    telegram_bot.send_coaching_message("hello")
    assert fake.calls["n"] == 1


def test_send_coaching_succeeds_on_retry_after_transient_failure(monkeypatch):
    """A single 502 (or any transient error) on attempt 1 must not lose the message."""
    fake = _make_run([RuntimeError("simulated 502"), None])
    monkeypatch.setattr(telegram_bot.asyncio, "run", fake)
    telegram_bot.send_coaching_message("hello")
    assert fake.calls["n"] == 2


def test_send_coaching_retries_three_times_then_reraises(monkeypatch):
    """If all attempts fail, the exception must propagate so the poller's
    send_error_alert path triggers. Silent swallow would re-introduce the
    Apr 28 silent-failure regression."""
    fake = _make_run([
        RuntimeError("attempt 1 fail"),
        RuntimeError("attempt 2 fail"),
        RuntimeError("attempt 3 fail"),
    ])
    monkeypatch.setattr(telegram_bot.asyncio, "run", fake)
    with pytest.raises(RuntimeError, match="attempt 3 fail"):
        telegram_bot.send_coaching_message("hello")
    assert fake.calls["n"] == 3


# ---------------- send_backup_to_telegram ----------------


def test_send_backup_succeeds_on_first_attempt(monkeypatch):
    fake = _make_run([None])
    monkeypatch.setattr(telegram_bot.asyncio, "run", fake)
    telegram_bot.send_backup_to_telegram(Path("/tmp/fake.gz"), caption="x")
    assert fake.calls["n"] == 1


def test_send_backup_retries_three_times_then_returns_none(monkeypatch):
    """Backup is best-effort: a final failure must NOT raise (the poller's
    backup block already wraps in try/except, but the contract is no-raise)."""
    fake = _make_run([
        RuntimeError("backup attempt 1 fail"),
        RuntimeError("backup attempt 2 fail"),
        RuntimeError("backup attempt 3 fail"),
    ])
    monkeypatch.setattr(telegram_bot.asyncio, "run", fake)
    # Must NOT raise
    result = telegram_bot.send_backup_to_telegram(Path("/tmp/fake.gz"), caption="x")
    assert result is None
    assert fake.calls["n"] == 3


def test_send_backup_succeeds_on_retry_after_transient_failure(monkeypatch):
    fake = _make_run([RuntimeError("simulated 502"), None])
    monkeypatch.setattr(telegram_bot.asyncio, "run", fake)
    telegram_bot.send_backup_to_telegram(Path("/tmp/fake.gz"), caption="x")
    assert fake.calls["n"] == 2
