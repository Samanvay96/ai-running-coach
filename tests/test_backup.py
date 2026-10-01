"""Tests for the plan.yaml backup in src/backup.py.

plan.yaml is gitignored, so these copies are its only history. The contract:
a nightly local copy with 14-day rotation, and an off-Pi (Telegram) upload
whenever the plan differs from the last version that actually reached
Telegram — so a failed upload is retried rather than silently skipped.
"""

from datetime import date, timedelta

import pytest

from src import backup


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated backup dir + plan file; the DB snapshot is stubbed out."""
    backup_dir = tmp_path / "backups"
    plan = tmp_path / "plan.yaml"
    plan.write_text("title: v1\n")
    monkeypatch.setattr(backup, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(backup, "TRAINING_PLAN_PATH", plan)
    monkeypatch.setattr(backup, "run_backup", lambda: None)
    sent = []

    def fake_send(path, caption=""):
        sent.append((path.name, path.read_text()))
        return fake_send.ok
    fake_send.ok = True
    monkeypatch.setattr("src.telegram_bot.send_backup_to_telegram", fake_send)
    return plan, backup_dir, sent, fake_send


def test_backup_plan_copies_the_plan_under_todays_date(env):
    plan, backup_dir, _, _ = env
    copy = backup.backup_plan(plan)
    assert copy == backup_dir / f"plan-{date.today().isoformat()}.yaml"
    assert copy.read_text() == "title: v1\n"
    assert not list(backup_dir.glob("*.tmp"))


def test_backup_plan_without_a_plan_file_returns_none(env, tmp_path):
    assert backup.backup_plan(tmp_path / "missing.yaml") is None


def test_rotation_drops_old_plan_copies_and_leaves_other_files(env):
    plan, backup_dir, _, _ = env
    backup_dir.mkdir()
    old = backup_dir / f"plan-{(date.today() - timedelta(days=15)).isoformat()}.yaml"
    recent = backup_dir / f"plan-{(date.today() - timedelta(days=3)).isoformat()}.yaml"
    unrelated = backup_dir / "Lisbon_Marathon_Finish_Plan_v7.pre-notes.xlsx"
    db = backup_dir / f"coach-{(date.today() - timedelta(days=3)).isoformat()}.db.gz"
    for f in (old, recent, unrelated, db):
        f.write_text("x")
    backup.backup_plan(plan)
    assert not old.exists()
    assert recent.exists() and unrelated.exists() and db.exists()


def test_daily_uploads_the_plan_the_first_time(env):
    _, _, sent, _ = env
    backup.run_daily()
    assert sent == [(f"plan-{date.today().isoformat()}.yaml", "title: v1\n")]


def test_daily_skips_the_upload_when_the_plan_is_unchanged(env):
    _, _, sent, _ = env
    backup.run_daily()
    backup.run_daily()
    assert len(sent) == 1


def test_daily_uploads_again_after_the_plan_changes(env):
    plan, _, sent, _ = env
    backup.run_daily()
    plan.write_text("title: v2\n")
    backup.run_daily()
    assert [body for _, body in sent] == ["title: v1\n", "title: v2\n"]


def test_a_failed_upload_is_retried_next_time(env):
    """Comparing against yesterday's local copy would call an unchanged plan
    'already backed up' and never send it. The marker only moves on success."""
    _, _, sent, fake_send = env
    fake_send.ok = False
    backup.run_daily()
    fake_send.ok = True
    backup.run_daily()
    assert len(sent) == 2
    backup.run_daily()
    assert len(sent) == 2  # now marked as sent


def test_daily_with_no_plan_still_runs_the_db_backup(env, monkeypatch):
    plan, _, sent, _ = env
    plan.unlink()
    calls = []
    monkeypatch.setattr(backup, "run_backup", lambda: calls.append(1))
    backup.run_daily()
    assert calls == [1] and sent == []
