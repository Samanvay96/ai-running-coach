"""Regression tests for poller.extract_subjective_fields.

Post-run RPE + Feel (the watch's "How did that feel?" prompt) live in the
activity-DETAIL summaryDTO, not the activity-list summary the poller iterates.
Before May 2026 the poller read activity.get("directWorkoutRpe") off the list
payload, where those keys never appear — so every run stored rpe=None/feel=None
even when the runner logged them. Garmin also stores RPE on a 0–100 scale
(logged 3/10 → 30), which must be rescaled to the 1–10 the coach reports.
"""

from src.poller import extract_subjective_fields


# Real summaryDTO slice captured from activity 22944722639 (2026-05-20).
DETAIL_WITH_SUBJECTIVE = {
    "activityId": 22944722639,
    "summaryDTO": {
        "distance": 5004.47,
        "directWorkoutRpe": 30,   # 0–100 scale → RPE 3/10
        "directWorkoutFeel": 75,  # 0–100 scale → "Strong"
    },
}


def test_real_payload_rescales_rpe_and_passes_feel():
    fields = extract_subjective_fields(DETAIL_WITH_SUBJECTIVE)
    assert fields["rpe"] == 3.0      # 30 → 3.0, not 30/10
    assert fields["feel"] == 75       # left on the 0–100 scale format_feel expects


def test_missing_subjective_fields_are_none():
    fields = extract_subjective_fields({"summaryDTO": {"distance": 5004.47}})
    assert fields == {"rpe": None, "feel": None}


def test_missing_summary_dto_is_safe():
    assert extract_subjective_fields({}) == {"rpe": None, "feel": None}
    assert extract_subjective_fields(None) == {"rpe": None, "feel": None}


def test_rpe_zero_is_preserved_not_dropped():
    # 0 is a valid logged RPE; must not be coerced to None by truthiness.
    fields = extract_subjective_fields({"summaryDTO": {"directWorkoutRpe": 0, "directWorkoutFeel": 0}})
    assert fields["rpe"] == 0.0
    assert fields["feel"] == 0
