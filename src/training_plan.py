"""The training plan, loaded from a hand-edited YAML file.

Schema (all dates ISO, all paces QUOTED strings — PyYAML reads a bare 6:45 as
the base-60 integer 405):

    title: str
    revision: str                      # why the plan looks the way it does
    race: {name, date, goal, goal_pace?}
    zones: [{type, label, pace, hr, feel, km?: [min, max]}]
    benchmarks: [{checkpoint, target, why, when}]
    guidance: {TITLE: [line, ...]}
    race_day: {splits: [{segment, pace, cumulative}], fuelling: [{when, what, notes}]}
    weeks:
      - n: 1
        start: 2026-09-28              # a Monday; weeks must be contiguous
        phase: str
        banner: str                    # optional — opens a new phase section
        target_km: 20
        notes: str
        tue: {type: easy, km: 5}       # a session...
        mon: "Foot/calf loading"       # ...or a string for a non-run day
        sat: {type: long, km: 22, finish: {km: 3, pace: "6:45/km"}, note: "..."}

A missing day is rest. Session keys: type, km, pace, finish, note, name.
"""
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import yaml

RUN_TYPES = ("easy", "long", "race", "shakeout", "mp_tempo", "tempo", "intervals")
DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_SESSION_KEYS = {"type", "km", "pace", "finish", "note", "name"}
# Workout type -> the zone type that supplies its default pace.
_ZONE_FOR = {"easy": "easy", "long": "long", "mp_tempo": "mp", "tempo": "tempo",
             "intervals": "intervals"}
_LABEL = {"easy": "Easy", "long": "Long", "shakeout": "Shakeout", "mp_tempo": "MP tempo",
          "tempo": "Tempo", "intervals": "Intervals"}


class PlanError(ValueError):
    """The plan file is malformed. The message names the offending field."""


@dataclass
class PrescribedRun:
    workout_type: str       # "easy", "tempo", "intervals", "mp_tempo", "long", "rest", "race", "shakeout"
    distance_km: float
    target_pace: str        # e.g. "7:00–7:30/km" — a range where the plan gives one
    description: str        # Human-readable prescription, fed to prompts
    finish_km: float = 0.0  # Closing segment, e.g. "last 3 km @ MP (6:45)" -> 3.0
    finish_pace: str = ""   # ...and "6:45/km"

    def pace_brief(self) -> str:
        """Pace prescription in one phrase, including any closing segment.

        Key long runs are a Z2 body with a faster finish. A caller reading only
        target_pace would miss the MP segment, which is the point of them.
        """
        parts = []
        if self.target_pace:
            parts.append(self.target_pace)
        if self.finish_pace:
            parts.append(
                f"last {self.finish_km:g} km @ {self.finish_pace}"
                if self.finish_km else f"finish @ {self.finish_pace}"
            )
        return ", ".join(parts) if parts else "no pace given"


@dataclass
class PaceZone:
    run_type: str           # display label, e.g. "Easy 5–6 km"
    pace: str
    hr_zone: str
    feel: str
    type: str = ""          # machine key: easy / long / mp / strides / ...
    km: tuple[float, float] | None = None  # distance band this pace applies to


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
    checkpoint: str
    target: str
    why: str
    when: str


@dataclass
class GuidanceBlock:
    """A free-text rules block, e.g. "PLANTAR FASCIITIS RULES"."""
    title: str
    lines: list[str] = field(default_factory=list)


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

    def run_slots(self) -> list[tuple[int, PrescribedRun]]:
        """Every non-rest slot this week, as (weekday index, run)."""
        return [(i, self.day(i)) for i in range(7) if self.day(i).workout_type != "rest"]


@dataclass
class ResolvedRun:
    """A prescribed slot matched to the date a run actually happened on.

    `prescribed_date` is the plan's own day for the slot; `query_date` is the
    day being asked about. They differ when a run moved — the Saturday long run
    done on Sunday.
    """
    run: PrescribedRun
    prescribed_date: date
    query_date: date

    @property
    def shifted(self) -> bool:
        return self.prescribed_date != self.query_date

    @property
    def pulled_forward(self) -> bool:
        """True when a future slot was moved early onto `query_date`.

        As opposed to a carry-over, where `query_date` is catching up on a
        slot the runner already owes from an earlier day.
        """
        return self.prescribed_date > self.query_date

    def shift_note(self) -> str:
        """One phrase naming the move, or '' when the run is on its own day."""
        if not self.shifted:
            return ""
        direction = "pulled forward from" if self.pulled_forward else "carried over from"
        return f"{direction} {self.prescribed_date.strftime('%A %b %d')}"


def _require(mapping: dict, key: str, where: str, kind: type | tuple[type, ...]):
    if not isinstance(mapping, dict) or key not in mapping:
        raise PlanError(f"{where}: missing '{key}'")
    value = mapping[key]
    if not isinstance(value, kind):
        raise PlanError(f"{where}.{key}: expected {_kind_name(kind)}, got {value!r}")
    return value


def _optional(mapping: dict, key: str, where: str, kind, default=None):
    if key not in mapping or mapping[key] is None:
        return default
    value = mapping[key]
    if not isinstance(value, kind):
        raise PlanError(f"{where}.{key}: expected {_kind_name(kind)}, got {value!r}")
    return value


def _kind_name(kind) -> str:
    kinds = kind if isinstance(kind, tuple) else (kind,)
    return " or ".join(k.__name__ for k in kinds)


def _pace(mapping: dict, key: str, where: str) -> str:
    """A pace field. Rejects numbers — the tell of an unquoted 6:45."""
    value = mapping.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PlanError(
            f"{where}.{key}: got {value!r} — quote paces, e.g. \"6:45/km\" "
            f"(unquoted, YAML reads 6:45 as the number 405)"
        )
    return value


_NUM = (int, float)


class TrainingPlan:
    # A run can only stand in for a slot within this many days. Keeps a Sunday
    # run from claiming Thursday's easy slot once Saturday's is spoken for —
    # at that distance it's a different session, not a moved one.
    MAX_SHIFT_DAYS = 2

    def __init__(self, path: str):
        self._path = path
        self._mtime: float = 0.0
        self._load(path)

    # ------------------------------------------------------------------ load

    def _load(self, path: str):
        try:
            raw = yaml.safe_load(Path(path).read_text())
        except yaml.YAMLError as e:
            raise PlanError(f"{path}: not valid YAML — {e}") from e
        if not isinstance(raw, dict):
            raise PlanError(f"{path}: top level must be a mapping")

        # Build everything into locals first, so a bad edit on reload leaves
        # the previously loaded plan intact instead of half-overwritten.
        title = _require(raw, "title", "plan", str)
        revision = _optional(raw, "revision", "plan", str, "")

        race = _require(raw, "race", "plan", dict)
        race_name = _require(race, "name", "race", str)
        race_date = _require(race, "date", "race", date)
        goal = str(_optional(race, "goal", "race", (str, int, float), ""))
        goal_pace = _pace(race, "goal_pace", "race")

        zones = [self._load_zone(z, f"zones[{i}]")
                 for i, z in enumerate(_optional(raw, "zones", "plan", list, []))]

        benchmarks = []
        for i, b in enumerate(_optional(raw, "benchmarks", "plan", list, [])):
            where = f"benchmarks[{i}]"
            benchmarks.append(Benchmark(
                checkpoint=_require(b, "checkpoint", where, str),
                target=str(_optional(b, "target", where, (str, int, float), "")),
                why=_optional(b, "why", where, str, ""),
                when=_optional(b, "when", where, str, ""),
            ))

        guidance = []
        for title_, lines in (_optional(raw, "guidance", "plan", dict, {}) or {}).items():
            if not isinstance(lines, list) or not all(isinstance(ln, str) for ln in lines):
                raise PlanError(f"guidance.{title_}: expected a list of strings")
            guidance.append(GuidanceBlock(title=str(title_), lines=list(lines)))

        race_day = _optional(raw, "race_day", "plan", dict, {})
        splits = []
        for i, s in enumerate(_optional(race_day, "splits", "race_day", list, [])):
            where = f"race_day.splits[{i}]"
            splits.append(RaceSplit(
                segment=_require(s, "segment", where, str),
                target_pace=_pace(s, "pace", where),
                cumulative_time=_optional(s, "cumulative", where, str, ""),
            ))
        fueling = []
        for i, f in enumerate(_optional(race_day, "fuelling", "race_day", list, [])):
            where = f"race_day.fuelling[{i}]"
            fueling.append(FuelingItem(
                when=_require(f, "when", where, str),
                what=_require(f, "what", where, str),
                notes=_optional(f, "notes", where, str, ""),
            ))

        # Weeks need zones/splits/race in place to fill default paces; put the
        # old ones back if the weeks turn out to be broken.
        previous = (getattr(self, "pace_zones", []), getattr(self, "race_splits", []),
                    getattr(self, "race_date", None))
        self.pace_zones, self.race_splits, self.race_date = zones, splits, race_date
        try:
            weeks, markers = self._load_weeks(raw)
        except PlanError:
            self.pace_zones, self.race_splits, self.race_date = previous
            raise

        self.title, self.revision_note = title, revision
        self.race_name, self.goal, self.goal_pace = race_name, goal, goal_pace
        self.benchmarks, self.guidance, self.fueling = benchmarks, guidance, fueling
        self.weeks, self.section_markers = weeks, markers
        self._mtime = Path(path).stat().st_mtime

    def _load_weeks(self, raw: dict) -> tuple[list[TrainingWeek], list[tuple[int, str]]]:
        race_date = self.race_date
        weeks, markers = [], []
        raw_weeks = _require(raw, "weeks", "plan", list)
        if not raw_weeks:
            raise PlanError("weeks: the plan has no weeks")
        for i, w in enumerate(raw_weeks):
            week = self._load_week(w, f"weeks[{i}]")
            if weeks:
                prev = weeks[-1]
                if week.week_number != prev.week_number + 1:
                    raise PlanError(f"weeks[{i}]: week n={week.week_number} follows "
                                    f"n={prev.week_number}; numbering must be consecutive")
                if week.start_date != prev.start_date + timedelta(days=7):
                    raise PlanError(f"weeks[{i}]: start {week.start_date} leaves a gap or "
                                    f"overlap after {prev.start_date}")
            if w.get("banner"):
                markers.append((week.week_number, str(w["banner"])))
            weeks.append(week)

        race_week = next((wk for wk in weeks if wk.start_date <= race_date <= wk.end_date), None)
        if race_week and race_week.day(race_date.weekday()).workout_type != "race":
            raise PlanError(f"race.date {race_date} is not a race session in week {race_week.week_number}")
        return weeks, markers

    @staticmethod
    def _load_zone(z: dict, where: str) -> PaceZone:
        km = _optional(z, "km", where, list)
        if km is not None and (len(km) != 2 or not all(isinstance(k, _NUM) for k in km)):
            raise PlanError(f"{where}.km: expected [min, max]")
        return PaceZone(
            run_type=_require(z, "label", where, str),
            pace=_pace(z, "pace", where),
            hr_zone=_optional(z, "hr", where, str, ""),
            feel=_optional(z, "feel", where, str, ""),
            type=_require(z, "type", where, str),
            km=(float(km[0]), float(km[1])) if km else None,
        )

    def _load_week(self, w: dict, where: str) -> TrainingWeek:
        n = _require(w, "n", where, int)
        start = _require(w, "start", where, date)
        if start.weekday() != 0:
            raise PlanError(f"{where}.start: {start} is a {start.strftime('%A')}, not a Monday")
        end = start + timedelta(days=6)
        days = [self._load_session(w.get(key), f"{where}.{key}", start + timedelta(days=i))
                for i, key in enumerate(DAY_KEYS)]
        unknown = set(w) - {"n", "start", "phase", "banner", "target_km", "notes", *DAY_KEYS}
        if unknown:
            raise PlanError(f"{where}: unknown keys {sorted(unknown)}")
        return TrainingWeek(
            week_number=n,
            dates=f"{start.strftime('%b %d')} – {end.strftime('%b %d')}",
            start_date=start,
            end_date=end,
            phase=_optional(w, "phase", where, str, ""),
            monday=days[0], tuesday=days[1], wednesday=days[2], thursday=days[3],
            friday=days[4], saturday=days[5], sunday=days[6],
            weekly_km_target=float(_optional(w, "target_km", where, _NUM, 0)),
            notes=_optional(w, "notes", where, str, ""),
        )

    def _load_session(self, s, where: str, on: date) -> PrescribedRun:
        if s is None:
            return PrescribedRun("rest", 0, "", "Rest")
        if isinstance(s, str):
            return PrescribedRun("rest", 0, "", s)
        if not isinstance(s, dict):
            raise PlanError(f"{where}: expected a session mapping or a string")
        unknown = set(s) - _SESSION_KEYS
        if unknown:
            raise PlanError(f"{where}: unknown keys {sorted(unknown)}")
        wtype = _require(s, "type", where, str)
        if wtype not in RUN_TYPES:
            raise PlanError(f"{where}.type: '{wtype}' is not one of {', '.join(RUN_TYPES)}")
        km = float(_require(s, "km", where, _NUM))
        if km <= 0:
            raise PlanError(f"{where}.km: must be positive")
        note = _optional(s, "note", where, str, "")

        finish_km, finish_pace = 0.0, ""
        finish = _optional(s, "finish", where, dict)
        if finish is not None:
            finish_km = float(_require(finish, "km", f"{where}.finish", _NUM))
            finish_pace = _pace(finish, "pace", f"{where}.finish")
            if not finish_pace:
                raise PlanError(f"{where}.finish: missing 'pace'")

        pace = _pace(s, "pace", where)
        if wtype == "race":
            name = _optional(s, "name", where, str, "Race")
            # Only the goal race inherits the race-day opening pace. A tune-up
            # is a different event — don't stamp the marathon's pace on it.
            if not pace and on == self.race_date:
                pace = self._race_opening_pace()
            desc = f"🏁 {name} {km:g} km" + (f" @ {pace}" if pace else "")
        else:
            if not pace:
                pace = self._zone_pace(wtype, km)
            desc = f"{_LABEL[wtype]} {km:g} km" + (f" @ {pace}" if pace else "")
        if finish_pace:
            desc += f" — last {finish_km:g} km @ {finish_pace}"
        if note:
            desc += f" — {note}"
        return PrescribedRun(wtype, km, pace, desc, finish_km=finish_km, finish_pace=finish_pace)

    def _zone_pace(self, workout_type: str, km: float = 0.0) -> str:
        """Default pace for a workout type: the zone whose km band fits, else
        the first zone of that type."""
        ztype = _ZONE_FOR.get(workout_type)
        candidates = [z for z in self.pace_zones if z.type == ztype]
        for z in candidates:
            if z.km and z.km[0] <= km <= z.km[1]:
                return z.pace
        return candidates[0].pace if candidates else ""

    def _race_opening_pace(self) -> str:
        """Opening-split pace from the race-day plan, else marathon pace.

        The race strategy hangs on starting slow, so the opening split — not
        MP — is what the race slot should advertise.
        """
        for split in self.race_splits:
            m = re.search(r"(\d:\d{2})\s*/km", split.target_pace)
            if m:
                return f"{m.group(1)}/km"
        return self._zone_pace("mp_tempo")

    def reload_if_changed(self) -> bool:
        """Reload if the file changed. Returns True if reloaded.

        A broken edit raises PlanError and keeps the last good plan loaded.
        """
        try:
            current_mtime = Path(self._path).stat().st_mtime
        except OSError:
            return False
        if current_mtime > self._mtime:
            self._load(self._path)
            return True
        return False

    # ----------------------------------------------------------------- query

    @property
    def start_date(self) -> date:
        return self.weeks[0].start_date

    @property
    def target_finish(self) -> str:
        return self.goal

    @property
    def target_pace(self) -> str:
        return self.goal_pace

    def get_z2_bounds(self, max_hr: int, rhr: int | None = None) -> tuple[int, int] | None:
        """Return absolute (low, high) BPM bounds for Zone 2.

        Uses the Karvonen / %HRR formula: HR = ((max_hr - rhr) * pct) + rhr.
        If rhr is not provided, falls back to %MaxHR. Percentages come from the
        easy zone's `hr` text (e.g. '60-70% max HR' is read as 60-70% HRR).
        """
        for pz in self.pace_zones:
            if pz.type == "easy" or "zone 2" in pz.hr_zone.lower():
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

    def get_week_for_date(self, d: date) -> TrainingWeek | None:
        for week in self.weeks:
            if week.start_date <= d <= week.end_date:
                return week
        return None

    def get_prescribed_run(self, d: date) -> PrescribedRun | None:
        """The run prescribed for exactly this date, ignoring any day shift.

        Prospective callers ("what am I meant to run on Thursday?") want this.
        To match a run that already happened — or one being decided on this
        morning — use resolve_run_for_date, which tolerates a moved day.
        """
        week = self.get_week_for_date(d)
        if not week:
            return None
        slot = week.day(d.weekday())
        return slot if slot.workout_type != "rest" else None

    def resolve_run_for_date(
        self,
        d: date,
        completed_dates: Iterable[date] | None = None,
    ) -> ResolvedRun | None:
        """Match a date to the plan slot it fulfils, tolerating a shifted day.

        The plan pins sessions to weekdays, but runs move — a Saturday long run
        gets done on Sunday. Resolving strictly by weekday drops the
        prescription for that run and books Saturday as a miss, so:

          1. If `d` has a run of its own, that's the answer.
          2. Otherwise take the nearest non-rest slot within MAX_SHIFT_DAYS
             that no other run has already claimed — checked by calendar
             distance, not by plan week, so a Saturday long run done on the
             following Monday (a 2-day gap that crosses the week boundary)
             still matches. Ties — a rest day flanked by two slots — go to
             the earlier one; carrying a run over is far more common than
             pulling one forward.

        `completed_dates` is every date already holding a recorded activity;
        those slots are spoken for and can't be matched twice. Callers without
        DB access may omit it, at the cost of possibly matching a slot that was
        in fact already run.

        Returns None outside the plan window, or when no slot is free.
        """
        week = self.get_week_for_date(d)
        if not week:
            return None

        exact = week.day(d.weekday())
        if exact.workout_type != "rest":
            return ResolvedRun(run=exact, prescribed_date=d, query_date=d)

        taken = set(completed_dates or ())
        candidates: list[tuple[int, bool, date, PrescribedRun]] = []
        for gap in range(1, self.MAX_SHIFT_DAYS + 1):
            for slot_date in (d - timedelta(days=gap), d + timedelta(days=gap)):
                if slot_date in taken:
                    continue
                slot_week = self.get_week_for_date(slot_date)
                if not slot_week:
                    continue
                run = slot_week.day(slot_date.weekday())
                if run.workout_type == "rest":
                    continue
                # Sort key: nearest first, then past before future.
                candidates.append((gap, slot_date > d, slot_date, run))

        if not candidates:
            return None
        _, _, slot_date, run = min(candidates, key=lambda c: (c[0], c[1]))
        return ResolvedRun(run=run, prescribed_date=slot_date, query_date=d)

    def get_section_marker(self, week_number: int) -> str:
        """The phase banner covering a week, e.g. 'PHASE 1: RETURN — ...'."""
        current = ""
        for start, text in self.section_markers:
            if start <= week_number:
                current = text
        return current

    def get_goal_summary(self) -> str:
        """One line naming the race and goal, straight from the plan file.

        Read from the plan rather than hardcoded, so a revision that changes the
        target can't leave a stale goal embedded in the coaching prompts.
        """
        line = f"{self.race_name} on {self.race_date.strftime('%A %B %d, %Y')}"
        if self.goal and self.goal_pace:
            return f"{line} — target {self.goal} (~{self.goal_pace})"
        if self.goal:
            return f"{line} — goal: {self.goal}"
        return line

    def get_benchmarks_text(self) -> str:
        """Progress checkpoints, formatted for a prompt."""
        return "\n".join(
            f"  {b.checkpoint}: {b.target}"
            + (f" — {b.why}" if b.why else "")
            + (f" [{b.when}]" if b.when else "")
            for b in self.benchmarks
        )

    def get_guidance_text(self) -> str:
        """Every free-text rules block, formatted for a prompt."""
        out: list[str] = []
        for block in self.guidance:
            out.append(f"{block.title}")
            out.extend(f"  {line}" for line in block.lines)
            out.append("")
        return "\n".join(out).rstrip()

    _DAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

    def get_plan_summary(self) -> str:
        total = len(self.weeks)
        lines = [f"{self.title or 'Training plan'} — {total} weeks, {self.get_goal_summary()}", ""]
        lines.append("PACE ZONES:")
        for pz in self.pace_zones:
            lines.append(f"  {pz.run_type}: {pz.pace} | {pz.hr_zone} | {pz.feel}")
        lines.append("")
        lines.append("PROGRESS CHECKS:")
        lines.append(self.get_benchmarks_text())
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
        marker = self.get_section_marker(week.week_number)
        if marker:
            lines.append(marker)
        for i, label in enumerate(self._DAY_LABELS):
            lines.append(f"{label}: {week.day(i).description}")
        lines.append(f"Target: {week.weekly_km_target} km")
        if week.notes:
            lines.append(f"Notes: {week.notes}")
        return "\n".join(lines)
