"""Regression tests for poller.extract_weather_fields.

Garmin's /activity/{id}/weather endpoint returns Fahrenheit and mph regardless
of the account's measurementSystem preference, with no unit field in the
payload. This was discovered May 18 2026 when a London run was reported as
"50°C / 76% humidity" — the 50 was 50°F (10°C). These tests pin the unit
conversion in place so a future "simplification" doesn't reintroduce the bug.
"""

import pytest

from src.poller import extract_weather_fields


# Real payload captured from London City Airport station on 2026-05-18 08:00.
LONDON_PAYLOAD = {
    "issueDate": "2026-05-18T06:20:00.000+00:00",
    "temp": 50,
    "apparentTemp": 47,
    "dewPoint": 43,
    "relativeHumidity": 76,
    "windDirection": 230,
    "windDirectionCompassPoint": "sw",
    "windSpeed": 7,
    "windGust": None,
    "latitude": 51.505279960110784,
    "longitude": 0.05527496337890625,
    "weatherStationDTO": {"id": "EGLC", "name": "London City Airport", "timezone": None},
    "weatherTypeDTO": {"weatherTypePk": None, "desc": "Fair", "image": None},
}


def test_real_london_payload_converted_to_metric():
    fields = extract_weather_fields(LONDON_PAYLOAD)
    assert fields["temp_c"] == 10.0
    assert fields["apparent_temp_c"] == 8.3
    assert fields["humidity_pct"] == 76
    assert fields["wind_kph"] == 11.3
    assert fields["label"] == "Fair"


def test_none_inputs_pass_through():
    fields = extract_weather_fields(None)
    assert fields == {"temp_c": None, "apparent_temp_c": None,
                      "humidity_pct": None, "wind_kph": None, "label": None}


def test_missing_temp_keys_stay_none():
    fields = extract_weather_fields({"relativeHumidity": 60})
    assert fields["temp_c"] is None
    assert fields["apparent_temp_c"] is None
    assert fields["wind_kph"] is None
    assert fields["humidity_pct"] == 60


def test_freezing_point_converts_cleanly():
    # 32°F = 0°C exactly. Catches sign or off-by-one regressions.
    fields = extract_weather_fields({"temp": 32, "apparentTemp": 32, "windSpeed": 0})
    assert fields["temp_c"] == 0.0
    assert fields["apparent_temp_c"] == 0.0
    assert fields["wind_kph"] == 0.0


def test_label_fallback_to_top_level():
    fields = extract_weather_fields({"temp": 68, "weatherTypeDesc": "Cloudy"})
    assert fields["label"] == "Cloudy"
    assert fields["temp_c"] == 20.0
