import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import openpyxl


@dataclass
class PrescribedRun:
    workout_type: str       # "easy", "tempo", "intervals", "mp_tempo", "long", "rest", "race", "shakeout"
    distance_km: float
    target_pace: str        # e.g. "6:15/km" or "5:30/km"
    description: str        # Full cell text


@dataclass
class PaceZone:
    run_type: str
    pace: str
    hr_zone: str
    feel: str


@dataclass
class RaceSplit:
    segment: str
    target_pace: str
    cumulative_time: str


@dataclass
class FuelingItem:
    when: str
    what: str
    notes: str


@dataclass
class Benchmark:
    distance: str
    target_time: str
    target_pace: str
    when_to_test: str


@dataclass
class TrainingWeek:
    week_number: int
    dates: str
    start_date: date
    end_date: date
    phase: str
    monday: str
    tuesday: PrescribedRun
    thursday: PrescribedRun
    saturday: PrescribedRun
    weekly_km_target: float
    notes: str


class TrainingPlan:
    def __init__(self, xlsx_path: str):
        self.weeks: list[TrainingWeek] = []
        self.pace_zones: list[PaceZone] = []
        self.benchmarks: list[Benchmark] = []
        self.race_splits: list[RaceSplit] = []
        self.fueling: list[FuelingItem] = []
        self._parse(xlsx_path)

    def _parse(self, path: str):
        wb = openpyxl.load_workbook(path, data_only=True)
        self._parse_training_sheet(wb["Training Plan"])
        self._parse_pace_guide(wb["Pace Guide"])
        self._parse_race_day(wb["Race Day"])

    def _parse_training_sheet(self, ws):
        plan_start = date(2026, 3, 2)
        for row in ws.iter_rows(min_row=5, max_row=ws.max_row, values_only=False):
            week_val = row[0].value  # Column A
            if not isinstance(week_val, (int, float)):
                continue  # Skip phase header rows
            week_num = int(week_val)
            week_start = plan_start + timedelta(weeks=week_num - 1)
            week_end = week_start + timedelta(days=6)

            self.weeks.append(TrainingWeek(
                week_number=week_num,
                dates=str(row[1].value or ""),
                start_date=week_start,
                end_date=week_end,
                phase=str(row[2].value or ""),
                monday=str(row[3].value or ""),
                tuesday=self._parse_run_cell(str(row[4].value or ""), "tuesday"),
                thursday=self._parse_run_cell(str(row[5].value or ""), "thursday"),
                saturday=self._parse_run_cell(str(row[6].value or ""), "saturday"),
                weekly_km_target=float(row[7].value or 0),
                notes=str(row[8].value or ""),
            ))

    def _parse_run_cell(self, text: str, day: str) -> PrescribedRun:
        if not text or text == "None":
            return PrescribedRun("rest", 0, "", "Rest")

        text_lower = text.lower()

        # Race day
        if "race day" in text_lower:
            return PrescribedRun("race", 42.2, "5:40/km", text)

        # Shakeout
        if "shakeout" in text_lower:
            dist = self._extract_distance(text)
            return PrescribedRun("shakeout", dist, "", text)

        # Determine workout type from first word
        if text_lower.startswith("mp tempo"):
            wtype = "mp_tempo"
        elif text_lower.startswith("intervals"):
            wtype = "intervals"
        elif text_lower.startswith("tempo"):
            wtype = "tempo"
        elif text_lower.startswith("long"):
            wtype = "long"
        elif text_lower.startswith("easy"):
            wtype = "easy"
        else:
            wtype = "easy"

        dist = self._extract_distance(text)
        pace = self._extract_pace(text)

        return PrescribedRun(wtype, dist, pace, text)

    def _extract_distance(self, text: str) -> float:
        m = re.search(r"(\d+(?:\.\d+)?)\s*km", text)
        return float(m.group(1)) if m else 0

    def _extract_pace(self, text: str) -> str:
        # Match ~6:30/km style
        m = re.search(r"~?(\d:\d{2})/km", text)
        if m:
            return f"{m.group(1)}/km"
        # Match @5:25 style (tempo/interval target pace)
        m = re.search(r"@(\d:\d{2})", text)
        if m:
            return f"{m.group(1)}/km"
        return ""

    def _parse_pace_guide(self, ws):
        # Rows 3-7: pace zones
        for row in ws.iter_rows(min_row=3, max_row=7, values_only=True):
            if row[0]:
                self.pace_zones.append(PaceZone(
                    run_type=str(row[0]),
                    pace=str(row[1] or ""),
                    hr_zone=str(row[2] or ""),
                    feel=str(row[3] or ""),
                ))
        # Rows 12-14: benchmarks
        for row in ws.iter_rows(min_row=12, max_row=14, values_only=True):
            if row[0]:
                self.benchmarks.append(Benchmark(
                    distance=str(row[0]),
                    target_time=str(row[1] or ""),
                    target_pace=str(row[2] or ""),
                    when_to_test=str(row[3] or ""),
                ))

    def _parse_race_day(self, ws):
        # Rows 3-11: race splits
        for row in ws.iter_rows(min_row=3, max_row=11, values_only=True):
            if row[0]:
                self.race_splits.append(RaceSplit(
                    segment=str(row[0]),
                    target_pace=str(row[1] or ""),
                    cumulative_time=str(row[2] or ""),
                ))
        # Rows 15-21: fueling
        for row in ws.iter_rows(min_row=15, max_row=21, values_only=True):
            if row[0]:
                self.fueling.append(FuelingItem(
                    when=str(row[0]),
                    what=str(row[1] or ""),
                    notes=str(row[2] or ""),
                ))

    def get_week_for_date(self, d: date) -> TrainingWeek | None:
        for week in self.weeks:
            if week.start_date <= d <= week.end_date:
                return week
        return None

    def get_prescribed_run(self, d: date) -> PrescribedRun | None:
        week = self.get_week_for_date(d)
        if not week:
            return None
        weekday = d.weekday()  # 0=Mon, 1=Tue, ...
        if weekday == 1:
            return week.tuesday
        elif weekday == 3:
            return week.thursday
        elif weekday == 5:
            return week.saturday
        return None  # Rest/cross-training day

    def get_plan_summary(self) -> str:
        lines = ["32-week sub-4:00 Lisbon Marathon plan (Oct 10, 2026)", ""]
        lines.append("PACE ZONES:")
        for pz in self.pace_zones:
            lines.append(f"  {pz.run_type}: {pz.pace} | {pz.hr_zone} | {pz.feel}")
        lines.append("")
        lines.append("BENCHMARKS:")
        for b in self.benchmarks:
            lines.append(f"  {b.distance}: {b.target_time} ({b.target_pace}) by {b.when_to_test}")
        lines.append("")
        lines.append("WEEKS:")
        for w in self.weeks:
            lines.append(
                f"  Wk {w.week_number} ({w.phase}): "
                f"Tue={w.tuesday.description} | "
                f"Thu={w.thursday.description} | "
                f"Sat={w.saturday.description} | "
                f"Target={w.weekly_km_target}km"
            )
        return "\n".join(lines)

    def get_week_summary(self, week: TrainingWeek) -> str:
        return (
            f"Week {week.week_number} ({week.phase}) — {week.dates}\n"
            f"Mon: {week.monday}\n"
            f"Tue: {week.tuesday.description}\n"
            f"Thu: {week.thursday.description}\n"
            f"Sat: {week.saturday.description}\n"
            f"Target: {week.weekly_km_target} km\n"
            f"Notes: {week.notes}"
        )
