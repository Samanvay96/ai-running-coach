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
    monday: PrescribedRun
    tuesday: PrescribedRun
    wednesday: PrescribedRun
    thursday: PrescribedRun
    friday: PrescribedRun
    saturday: PrescribedRun
    sunday: PrescribedRun
    weekly_km_target: float
    notes: str

    def day(self, weekday: int) -> PrescribedRun:
        """Return the prescribed slot for a Python weekday (0=Mon ... 6=Sun)."""
        return (
            self.monday, self.tuesday, self.wednesday, self.thursday,
            self.friday, self.saturday, self.sunday,
        )[weekday]


class TrainingPlan:
    def __init__(self, xlsx_path: str):
        self._xlsx_path = xlsx_path
        self._mtime: float = 0.0
        self.weeks: list[TrainingWeek] = []
        self.pace_zones: list[PaceZone] = []
        self.benchmarks: list[Benchmark] = []
        self.race_splits: list[RaceSplit] = []
        self.fueling: list[FuelingItem] = []
        self._parse(xlsx_path)

    def _parse(self, path: str):
        wb = openpyxl.load_workbook(path, data_only=True)
        self.weeks = []
        self.pace_zones = []
        self.benchmarks = []
        self.race_splits = []
        self.fueling = []
        self._parse_training_sheet(wb["Training Plan"])
        self._parse_pace_guide(wb["Pace Guide"])
        if "Benchmarks" in wb.sheetnames:
            self._parse_benchmarks(wb["Benchmarks"])
        self._parse_race_day(wb["Race Day Plan"])
        self._mtime = Path(path).stat().st_mtime

    def reload_if_changed(self) -> bool:
        """Reload the plan if the xlsx file has been modified. Returns True if reloaded."""
        try:
            current_mtime = Path(self._xlsx_path).stat().st_mtime
        except OSError:
            return False
        if current_mtime > self._mtime:
            self._parse(self._xlsx_path)
            return True
        return False

    def _parse_training_sheet(self, ws):
        plan_start_fallback = date(2026, 3, 2)
        for row in ws.iter_rows(min_row=5, max_row=ws.max_row, values_only=False):
            week_val = row[0].value  # Column A
            # v5 stores week numbers as strings ('1', '2', ...); older versions
            # used ints. Accept either; non-numeric cells are phase headers.
            if isinstance(week_val, (int, float)):
                week_num = int(week_val)
            elif isinstance(week_val, str) and week_val.strip().isdigit():
                week_num = int(week_val.strip())
            else:
                continue  # Phase header / blank row
            dates_str = str(row[1].value or "")
            parsed = self._parse_date_range(dates_str, year=plan_start_fallback.year)
            if parsed:
                week_start, week_end = parsed
            else:
                week_start = plan_start_fallback + timedelta(weeks=week_num - 1)
                week_end = week_start + timedelta(days=6)

            # v5 layout: columns 3-9 are Mon..Sun (all 7 days listed); column
            # 10 is the weekly km target; column 19 is notes. Run/strength/rest
            # can fall on any day, so strict=True on every slot — anything
            # without an explicit km figure parses as rest.
            self.weeks.append(TrainingWeek(
                week_number=week_num,
                dates=dates_str,
                start_date=week_start,
                end_date=week_end,
                phase=str(row[2].value or ""),
                monday=self._parse_run_cell(str(row[3].value or ""), "monday", strict=True),
                tuesday=self._parse_run_cell(str(row[4].value or ""), "tuesday", strict=True),
                wednesday=self._parse_run_cell(str(row[5].value or ""), "wednesday", strict=True),
                thursday=self._parse_run_cell(str(row[6].value or ""), "thursday", strict=True),
                friday=self._parse_run_cell(str(row[7].value or ""), "friday", strict=True),
                saturday=self._parse_run_cell(str(row[8].value or ""), "saturday", strict=True),
                sunday=self._parse_run_cell(str(row[9].value or ""), "sunday", strict=True),
                weekly_km_target=float(row[10].value or 0),
                notes=str(row[19].value or ""),
            ))

    def _parse_run_cell(self, text: str, day: str, strict: bool = False) -> PrescribedRun:
        if not text or text == "None":
            return PrescribedRun("rest", 0, "", "Rest")

        text_lower = text.lower()

        # Race day — the v5 plan flags races with the 🏁 emoji and/or
        # "MARATHON"/"HALF" in the cell (and may omit a km figure on the
        # Lisbon cell). Detect any of those so race day doesn't fall through
        # to rest under strict mode.
        if "🏁" in text or "race day" in text_lower or "marathon" in text_lower or "half" in text_lower:
            dist = self._extract_distance(text)
            if dist == 0:
                # Default: full marathon unless the cell says "half"
                dist = 21.1 if "half" in text_lower else 42.2
            return PrescribedRun("race", dist, "5:40/km", text)

        # Shakeout
        if "shakeout" in text_lower:
            dist = self._extract_distance(text)
            return PrescribedRun("shakeout", dist, "", text)

        dist = self._extract_distance(text)
        pace = self._extract_pace(text)

        # Strict mode (Mon column): only treat as a run if a km distance is present.
        # Cross-training/strength/rest cells (e.g. "F45 Weights", "REST or bodyweight")
        # have no km figure and should fall through to rest so they aren't matched
        # against actual runs.
        if strict and dist == 0:
            return PrescribedRun("rest", 0, "", text)

        # Strip a leading day-label prefix like "Mon:" so keyword detection works.
        keyword_text = re.sub(r"^\s*(mon|tue|wed|thu|fri|sat|sun)[a-z]*\s*:\s*", "", text_lower)

        if keyword_text.startswith("mp tempo"):
            wtype = "mp_tempo"
        elif keyword_text.startswith("intervals"):
            wtype = "intervals"
        elif keyword_text.startswith("tempo"):
            wtype = "tempo"
        elif keyword_text.startswith("long"):
            wtype = "long"
        elif keyword_text.startswith("easy"):
            wtype = "easy"
        else:
            wtype = "easy"

        return PrescribedRun(wtype, dist, pace, text)

    _MONTHS = {m: i + 1 for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    )}

    def _parse_date_range(self, text: str, year: int) -> tuple[date, date] | None:
        """Parse strings like 'Mar 02 – Mar 08' or 'Apr 27 - May 03' into (start, end)."""
        m = re.match(
            r"\s*([A-Z][a-z]{2})\s+(\d{1,2})\s*[–—\-]\s*([A-Z][a-z]{2})\s+(\d{1,2})\s*$",
            text,
        )
        if not m:
            return None
        s_mo, s_dy, e_mo, e_dy = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
        if s_mo not in self._MONTHS or e_mo not in self._MONTHS:
            return None
        start = date(year, self._MONTHS[s_mo], s_dy)
        end_year = year + 1 if self._MONTHS[e_mo] < self._MONTHS[s_mo] else year
        end = date(end_year, self._MONTHS[e_mo], e_dy)
        return start, end

    def _extract_distance(self, text: str) -> float:
        m = re.search(r"(\d+(?:\.\d+)?)\s*km", text)
        return float(m.group(1)) if m else 0

    def get_z2_bounds(self, max_hr: int, rhr: int | None = None) -> tuple[int, int] | None:
        """Return absolute (low, high) BPM bounds for Zone 2.

        Uses the Karvonen / %HRR formula: HR = ((max_hr - rhr) * pct) + rhr.
        If rhr is not provided, falls back to %MaxHR. Percentages come from the
        Pace Guide sheet (e.g. '60-70% max HR' is interpreted as 60-70% HRR).
        """
        for pz in self.pace_zones:
            if "zone 2" in pz.hr_zone.lower() or "easy" in pz.run_type.lower() or "recovery" in pz.run_type.lower():
                pct = self._parse_hr_zone_pct(pz.hr_zone)
                if pct:
                    low_pct, high_pct = pct
                    if rhr is not None:
                        reserve = max_hr - rhr
                        return (
                            int(round(reserve * low_pct / 100 + rhr)),
                            int(round(reserve * high_pct / 100 + rhr)),
                        )
                    return int(round(max_hr * low_pct / 100)), int(round(max_hr * high_pct / 100))
        return None

    @staticmethod
    def _parse_hr_zone_pct(zone_str: str) -> tuple[int, int] | None:
        """Extract (low, high) percentages from strings like 'Zone 2 (60-70% max HR)'."""
        m = re.search(r"(\d{2,3})\s*[-–—]\s*(\d{2,3})\s*%", zone_str)
        if m:
            return int(m.group(1)), int(m.group(2))
        return None

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
        # Pace zones live in a contiguous block under the header row.
        # v5 grew this from 5 zones (E/MP/T/I/S) to 7 (adds Hill Reps, Strides);
        # walk until the first blank row to stay robust to future tweaks.
        for row in ws.iter_rows(min_row=4, max_row=ws.max_row, values_only=True):
            run_type = row[0] if len(row) > 0 else None
            if not run_type:
                break
            self.pace_zones.append(PaceZone(
                run_type=str(run_type),
                pace=str(row[1] or "") if len(row) > 1 else "",
                hr_zone=str(row[2] or "") if len(row) > 2 else "",
                feel=str(row[3] or "") if len(row) > 3 else "",
            ))

    def _parse_benchmarks(self, ws):
        # v5 moved benchmarks to their own sheet. Header is row 3, entries
        # start at row 4. Walk until the first blank row.
        for row in ws.iter_rows(min_row=4, max_row=ws.max_row, values_only=True):
            distance = row[0] if len(row) > 0 else None
            if not distance:
                break
            self.benchmarks.append(Benchmark(
                distance=str(distance),
                target_time=str(row[1] or "") if len(row) > 1 else "",
                target_pace=str(row[2] or "") if len(row) > 2 else "",
                when_to_test=str(row[3] or "") if len(row) > 3 else "",
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
        slot = week.day(d.weekday())
        return slot if slot.workout_type != "rest" else None

    _DAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

    def get_plan_summary(self) -> str:
        total = len(self.weeks)
        lines = [f"{total}-week sub-4:00 Lisbon Marathon plan (Oct 10, 2026)", ""]
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
            run_days = [
                f"{label}={w.day(i).description}"
                for i, label in enumerate(self._DAY_LABELS)
                if w.day(i).workout_type != "rest"
            ]
            lines.append(
                f"  Wk {w.week_number} ({w.phase}): "
                + " | ".join(run_days)
                + f" | Target={w.weekly_km_target}km"
            )
        return "\n".join(lines)

    def get_week_summary(self, week: TrainingWeek) -> str:
        lines = [f"Week {week.week_number} ({week.phase}) — {week.dates}"]
        for i, label in enumerate(self._DAY_LABELS):
            lines.append(f"{label}: {week.day(i).description}")
        lines.append(f"Target: {week.weekly_km_target} km")
        if week.notes:
            lines.append(f"Notes: {week.notes}")
        return "\n".join(lines)
