#!/usr/bin/env python3
"""Static running dashboard generator.

Renders data/dashboard/index.html from the same DB and plan the poller
populates. Refreshed after every Garmin poll via systemd unit chaining
(ai-coach-poll.service's OnSuccess= fires ai-coach-dashboard.service, which
runs this module) — see systemd/ai-coach-dashboard.service. Serving the file
is a completely separate concern (dashboard/server.py, a bare stdlib static
file server on :8082): nothing in this module talks to Garmin, Telegram, or
Anthropic, or listens on a socket.
"""

import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

from src.coach import (
    compute_acr,
    compute_adherence,
    compute_cadence_context,
    compute_easy_run_trend,
    compute_hr_drift,
    compute_mileage_delta,
    compute_weekly_target,
    compute_zone_distribution,
    format_duration,
    format_pace,
    fulfilled_slots,
    resolve_z2_bounds,
)
from src.config import DB_PATH, PLAN_START_DATE, RACE_DATE, TRAINING_PLAN_PATH
from src.db import Database
from src.telegram_bot import send_error_alert
from src.training_plan import PrescribedRun, TrainingPlan, TrainingWeek

DASHBOARD_DIR = DB_PATH.parent / "dashboard"

# Not carried as a structured field in the plan xlsx (TrainingPlan.target_finish
# / .target_pace are goal-TIME strings, not the race distance) — documented here
# rather than re-derived from the plan's own race-day row each time.
MARATHON_KM = 42.195

WEEKDAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# =========================================================================
# Data helpers — logic not already in src/coach.py
# =========================================================================


def _longest_run_so_far(db: Database, start: date, today: date) -> float | None:
    """Farthest single run recorded from `start` through `today`, inclusive."""
    acts = db.get_activities_for_range(start.isoformat(), (today + timedelta(days=1)).isoformat())
    dists = [a.get("distance_km") for a in acts if a.get("distance_km")]
    return round(max(dists), 2) if dists else None


def _weekly_volume_series(plan: TrainingPlan, db: Database, start: date, end: date) -> list[dict]:
    """Per-week actual vs. prescribed km for every plan week overlapping [start, end].

    A shifted run must be credited to the week it was PRESCRIBED for, not the
    week its calendar date falls in — the same principle fulfilled_slots
    already encodes (coach.py), including the case where the shift crosses a
    week boundary (a Saturday long run done the following Monday). Naively
    summing distance by calendar week here would silently reintroduce the bug
    fixed in commit de88740 for adherence tracking, just for this chart instead.

    A run that resolves to no slot at all (a genuine bonus/unplanned run) is
    credited to its own calendar week and flagged in that week's `extra_km`.

    Returns one dict per week, oldest first:
      {week_number, start_date, phase, target_km, actual_km, extra_km}
    """
    weeks = [w for w in plan.weeks if w.end_date >= start and w.start_date <= end]
    if not weeks:
        return []

    pad = timedelta(days=TrainingPlan.MAX_SHIFT_DAYS)
    window_start = weeks[0].start_date - pad
    window_end = weeks[-1].end_date + pad
    acts = db.get_activities_for_range(
        window_start.isoformat(), (window_end + timedelta(days=1)).isoformat()
    )

    by_date: dict[date, float] = {}
    for a in acts:
        st = a.get("start_time") or ""
        try:
            d = date.fromisoformat(st[:10])
        except ValueError:
            continue
        by_date[d] = by_date.get(d, 0.0) + (a.get("distance_km") or 0.0)

    all_dates = set(by_date)
    by_week_number = {w.week_number: w for w in weeks}
    totals = {w.week_number: 0.0 for w in weeks}
    extras = {w.week_number: 0.0 for w in weeks}

    for d in sorted(all_dates):
        km = by_date[d]
        resolved = plan.resolve_run_for_date(d, all_dates - {d})
        target_week = plan.get_week_for_date(resolved.prescribed_date) if resolved else None
        if target_week is None:
            target_week = plan.get_week_for_date(d)
            bucket = extras
        else:
            bucket = totals
        if target_week and target_week.week_number in by_week_number:
            bucket[target_week.week_number] += km

    return [
        {
            "week_number": w.week_number,
            "start_date": w.start_date,
            "phase": w.phase,
            "target_km": round(w.weekly_km_target, 1),
            "actual_km": round(totals[w.week_number] + extras[w.week_number], 1),
            "extra_km": round(extras[w.week_number], 1),
        }
        for w in weeks
    ]


def _acr_status(ratio: float | None) -> tuple[str, str]:
    """(label, css status class) for an ACR ratio, per the plan's own framing:
    0.8-1.3 sweet spot, >1.5 injury risk."""
    if ratio is None:
        return "No data", "muted"
    if ratio < 0.8:
        return "Low", "info"
    if ratio <= 1.3:
        return "Sweet spot", "good"
    if ratio <= 1.5:
        return "Elevated", "warn"
    return "High", "bad"


def _ago_string(dt: datetime | None) -> tuple[str, bool]:
    """(human-readable delta, is_stale) for a UTC-aware datetime.

    Stale past ~90 min — 1.5x the poller's hourly cadence — doubles as a
    poller-health indicator on the page for free.
    """
    if dt is None:
        return "never", True
    mins = int((datetime.now(timezone.utc) - dt).total_seconds() // 60)
    stale = mins > 90
    if mins < 1:
        return "just now", stale
    if mins < 60:
        return f"{mins} min ago", stale
    h, m = divmod(mins, 60)
    return (f"{h}h {m}m ago" if m else f"{h}h ago"), stale


def _week_long_or_race_slot(week: TrainingWeek) -> PrescribedRun | None:
    """The week's headline session — its longest non-rest slot."""
    slots = week.run_slots()
    if not slots:
        return None
    return max((r for _, r in slots), key=lambda r: r.distance_km)


# =========================================================================
# Tiny inline-SVG chart helpers — no dependencies, matches this Pi's convention
# =========================================================================


def _esc(text) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _svg_volume_chart(series: list[dict]) -> str:
    """Grouped bar chart: prescribed (outline) vs actual (filled) km per week."""
    if not series:
        return '<p class="empty">Not enough data yet.</p>'
    w, h = 720, 220
    pad_l, pad_r, pad_t, pad_b = 40, 10, 14, 26
    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - pad_b
    max_km = max(max(s["target_km"], s["actual_km"]) for s in series) or 1
    n = len(series)
    group_w = plot_w / n
    bar_w = min(20.0, group_w * 0.32)

    def y(km: float) -> float:
        return pad_t + plot_h * (1 - km / max_km)

    bars = []
    labels = []
    for i, s in enumerate(series):
        cx = pad_l + group_w * (i + 0.5)
        t_x, a_x = cx - bar_w - 2, cx + 2
        t_y, a_y = y(s["target_km"]), y(s["actual_km"])
        bars.append(
            f'<rect x="{t_x:.1f}" y="{t_y:.1f}" width="{bar_w:.1f}" '
            f'height="{(pad_t + plot_h - t_y):.1f}" class="bar-target" rx="2">'
            f"<title>Week {s['week_number']} prescribed: {s['target_km']:g} km</title></rect>"
        )
        cls = "bar-extra" if s["extra_km"] >= s["actual_km"] else "bar-actual"
        extra_note = f" ({s['extra_km']:g} km unplanned)" if s["extra_km"] else ""
        bars.append(
            f'<rect x="{a_x:.1f}" y="{a_y:.1f}" width="{bar_w:.1f}" '
            f'height="{(pad_t + plot_h - a_y):.1f}" class="{cls}" rx="2">'
            f"<title>Week {s['week_number']} actual: {s['actual_km']:g} km{extra_note}</title></rect>"
        )
        if n <= 14 or i % 2 == 0:
            labels.append(
                f'<text x="{cx:.1f}" y="{h - 8}" class="ax" text-anchor="middle">W{s["week_number"]}</text>'
            )
    gridlines = "".join(
        f'<line x1="{pad_l}" y1="{y(g):.1f}" x2="{w - pad_r}" y2="{y(g):.1f}" class="grid"/>'
        f'<text x="{pad_l - 6}" y="{y(g) + 4:.1f}" class="ax" text-anchor="end">{g:g}</text>'
        for g in _nice_ticks(max_km)
    )
    return (
        f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Weekly volume, actual versus prescribed">'
        f"{gridlines}{''.join(bars)}{''.join(labels)}</svg>"
    )


def _nice_ticks(max_val: float, n: int = 4) -> list[float]:
    if max_val <= 0:
        return [0]
    step = max_val / n
    magnitude = 10 ** (len(str(int(step))) - 1) if step >= 1 else 1
    step = max(round(step / magnitude) * magnitude, magnitude)
    ticks = []
    v = 0.0
    while v <= max_val + step * 0.5:
        ticks.append(v)
        v += step
    return ticks


def _svg_zone_trend(points: list[dict]) -> str:
    """Time-in-Z1+Z2 trend, one point per run: {date, pct, workout_type}.

    A percentage has a natural, meaningful 0-100 domain and the plan states an
    explicit 80% target — autoscaling the y-axis to just the shown points (the
    naive sparkline approach) exaggerates noise and gives the reader no way to
    tell whether a dip is dramatic or trivial. Fixed domain + a labeled
    reference line fixes that. Long runs are expected to sit lower than easy
    runs by design (Z2 pace decays with distance) — shape-coding by workout
    type turns what otherwise reads as random noise into a legible pattern.
    """
    pts = [p for p in points if p["pct"] is not None]
    if len(pts) < 2:
        return '<p class="empty">Not enough data yet.</p>'
    w, h = 720, 170
    pad_l, pad_r, pad_t, pad_b = 30, 12, 14, 24
    plot_w, plot_h = w - pad_l - pad_r, h - pad_t - pad_b
    n = len(pts)

    def sx(i):
        return pad_l + (plot_w * i / (n - 1) if n > 1 else 0)

    def sy(pct):
        return pad_t + plot_h * (1 - pct / 100)

    grid = "".join(
        f'<line x1="{pad_l}" y1="{sy(g):.1f}" x2="{w - pad_r}" y2="{sy(g):.1f}" class="grid"/>'
        + (f'<text x="{pad_l - 6}" y="{sy(g) + 4:.1f}" class="ax" text-anchor="end">{g}</text>' if g in (0, 50, 100) else "")
        for g in (0, 25, 50, 75, 100)
    )
    target_y = sy(80)
    reference = (
        f'<line x1="{pad_l}" y1="{target_y:.1f}" x2="{w - pad_r}" y2="{target_y:.1f}" class="ref-line"/>'
        f'<text x="{pad_l + 4}" y="{target_y - 4:.1f}" class="ref-label">80% target</text>'
    )

    line_path = " ".join(f"{'M' if i == 0 else 'L'}{sx(i):.1f},{sy(p['pct']):.1f}" for i, p in enumerate(pts))
    markers = []
    for i, p in enumerate(pts):
        x, y = sx(i), sy(p["pct"])
        is_long = p["workout_type"] in ("long", "race")
        title = f"<title>{_esc(p['date'])} ({_esc(p['workout_type'])}): {p['pct']:g}%</title>"
        if is_long:
            markers.append(
                f'<rect x="{x - 4:.1f}" y="{y - 4:.1f}" width="8" height="8" rx="1.5" '
                f'class="mk-long" transform="rotate(45 {x:.1f} {y:.1f})">{title}</rect>'
            )
        else:
            markers.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" class="mk-easy">{title}</circle>')

    last = pts[-1]
    lx, ly = sx(n - 1), sy(last["pct"])
    label_y = ly + 14 if ly < h / 2 else ly - 8  # keep the label clear of the line's own trajectory
    end_label = f'<text x="{lx - 8:.1f}" y="{label_y:.1f}" text-anchor="end" class="spark-label">{last["pct"]:g}%</text>'

    first_date, last_date = pts[0]["date"][5:], pts[-1]["date"][5:]  # MM-DD, short enough for the axis
    axis_dates = (
        f'<text x="{pad_l}" y="{h - 6}" class="ax">{_esc(first_date)}</text>'
        f'<text x="{w - pad_r}" y="{h - 6}" text-anchor="end" class="ax">{_esc(last_date)}</text>'
    )

    return (
        f'<svg viewBox="0 0 {w} {h}" role="img" aria-label='
        f'"Time in Zone 1+2, {n} recent runs, latest {last["pct"]:g}%">'
        f"{grid}{reference}"
        f'<path d="{line_path}" class="spark-line" fill="none"/>'
        f"{''.join(markers)}{end_label}{axis_dates}"
        "</svg>"
    )


def _svg_ladder(entries: list[dict]) -> str:
    """Forward long-run ladder: one bar per week from today to race day, the
    race bar visually distinct at the end."""
    if not entries:
        return '<p class="empty">Nothing left on the ladder.</p>'
    w, h = 720, 200
    pad_l, pad_r, pad_t, pad_b = 34, 10, 14, 30
    plot_w, plot_h = w - pad_l - pad_r, h - pad_t - pad_b
    max_km = max(e["km"] for e in entries) or 1
    n = len(entries)
    slot_w = plot_w / n
    bar_w = min(34.0, slot_w * 0.6)
    bars, labels = [], []
    for i, e in enumerate(entries):
        cx = pad_l + slot_w * (i + 0.5)
        bar_h = plot_h * (e["km"] / max_km)
        y = pad_t + plot_h - bar_h
        cls = "bar-race" if e["is_race"] else ("bar-done" if e["done"] else "bar-target")
        bars.append(
            f'<rect x="{(cx - bar_w / 2):.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
            f'height="{bar_h:.1f}" class="{cls}" rx="2">'
            f"<title>{_esc(e['label'])}: {e['km']:g} km</title></rect>"
        )
        labels.append(f'<text x="{cx:.1f}" y="{h - 8}" class="ax" text-anchor="middle">{_esc(e["axis"])}</text>')
        labels.append(f'<text x="{cx:.1f}" y="{y - 4:.1f}" class="val" text-anchor="middle">{e["km"]:g}</text>')
    return (
        f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Long run ladder to race day">'
        f"{''.join(bars)}{''.join(labels)}</svg>"
    )


# =========================================================================
# Section renderers
# =========================================================================


def _render_header(plan: TrainingPlan, today: date, week: TrainingWeek | None, ago: str, stale: bool) -> str:
    days_to_race = (RACE_DATE - today).days
    if days_to_race < 0:
        race_line = f"Race day was {RACE_DATE.strftime('%b %d')} — hope it went well."
    elif days_to_race == 0:
        race_line = "Race day."
    else:
        race_line = f"T-{days_to_race} days to {RACE_DATE.strftime('%b %d')}"
    week_line = f"Week {week.week_number} &middot; {_esc(week.phase)}" if week else "Outside plan window"
    stale_cls = " stale" if stale else ""
    return f"""
<header class="top">
  <div class="eyebrow">{_esc(plan.title) or "Training Dashboard"}</div>
  <h1>{race_line}</h1>
  <div class="sub">{week_line}</div>
  <div class="freshness{stale_cls}">Data as of {_esc(ago)}</div>
</header>"""


def _render_stat_tiles(
    weekly_target: dict | None,
    acr: dict | None,
    delta: dict,
    adherence: dict,
    longest: float | None,
) -> str:
    tiles = []

    if weekly_target:
        pct = weekly_target["pct"]
        tiles.append(_tile("This week", f"{weekly_target['actual_km']:g} / {weekly_target['target_km']:g} km",
                            f"{pct:.0f}% &middot; {weekly_target['remaining_runs_count']} run(s) left",
                            "good" if pct >= 60 else "info"))
    else:
        tiles.append(_tile("This week", "&mdash;", "Outside plan window", "muted"))

    if acr:
        label, cls = _acr_status(acr["ratio"])
        ratio_str = f"{acr['ratio']:.2f}" if acr["ratio"] is not None else "&mdash;"
        tiles.append(_tile("ACR", ratio_str, label, cls))
    else:
        tiles.append(_tile("ACR", "&mdash;", "Not enough load history", "muted"))

    delta_pct = delta.get("pct_delta")
    delta_str = f"{'+' if (delta_pct or 0) >= 0 else ''}{delta_pct:.0f}%" if delta_pct is not None else "&mdash;"
    tiles.append(_tile("7d volume", f"{delta['last_7d_km']:g} km", f"{delta_str} vs 4wk avg",
                        "info"))

    adh_cls = "good" if adherence["total"] and adherence["completed"] == adherence["total"] else \
        ("warn" if adherence["total"] and adherence["completed"] / adherence["total"] < 0.8 else "info")
    adh_val = f"{adherence['completed']}/{adherence['total']}" if adherence["total"] else "&mdash;"
    tiles.append(_tile("Adherence", adh_val, "last prescribed runs", adh_cls))

    if longest:
        tiles.append(_tile("Longest run", f"{longest:g} km", f"of {MARATHON_KM:g} km race distance", "info"))
    else:
        tiles.append(_tile("Longest run", "&mdash;", "No runs yet", "muted"))

    return f'<section class="tiles">{"".join(tiles)}</section>'


def _tile(label: str, value: str, sub: str, cls: str) -> str:
    return (
        f'<div class="tile tile-{cls}"><div class="tile-label">{_esc(label)}</div>'
        f'<div class="tile-value">{value}</div><div class="tile-sub">{sub}</div></div>'
    )


def _render_recovery(wellness: dict | None) -> str:
    if not wellness:
        return '<section class="card"><h2>Recovery</h2><p class="empty">No wellness data yet.</p></section>'
    chips = []
    if wellness.get("sleep_score") is not None:
        chips.append(_chip("Sleep", str(wellness["sleep_score"])))
    if wellness.get("hrv_last_night") is not None:
        avg = wellness.get("hrv_7d_avg")
        sub = f"7d avg {avg:g}" if avg is not None else ""
        chips.append(_chip("HRV", f"{wellness['hrv_last_night']:g}ms", sub))
    if wellness.get("rhr") is not None:
        chips.append(_chip("RHR", f"{wellness['rhr']} bpm"))
    if not chips:
        return '<section class="card"><h2>Recovery</h2><p class="empty">No wellness data yet.</p></section>'
    return f'<section class="card"><h2>Recovery</h2><div class="chips">{"".join(chips)}</div></section>'


def _chip(label: str, value: str, sub: str = "") -> str:
    sub_html = f'<span class="chip-sub">{_esc(sub)}</span>' if sub else ""
    return f'<div class="chip"><span class="chip-label">{_esc(label)}</span><span class="chip-value">{_esc(value)}</span>{sub_html}</div>'


def _render_this_week(plan: TrainingPlan, db: Database, week: TrainingWeek | None, today: date) -> str:
    if not week:
        return '<section class="card"><h2>This week</h2><p class="empty">Outside plan window.</p></section>'
    done = fulfilled_slots(plan, db, week)
    cells = []
    for i, abbr in enumerate(WEEKDAY_ABBR):
        d = week.start_date + timedelta(days=i)
        run = week.day(i)
        is_today = d == today
        is_rest = run.workout_type == "rest"
        status = ""
        if not is_rest:
            if d in done:
                status = "done"
            elif d < today:
                status = "missed"
        cls = " ".join(c for c in ["day", is_today and "today", status] if c)
        if is_rest:
            body = '<div class="day-type rest">rest</div>'
            title = ""
        else:
            body = (
                f'<div class="day-type">{_esc(run.workout_type)}</div>'
                f'<div class="day-detail">{run.distance_km:g} km</div>'
                f'<div class="day-pace">{_esc(run.target_pace)}</div>'
            )
            # target_pace alone omits an MP finish segment ("last 3 km @
            # 6:45/km") — pace_brief() has the full prescription, shown on
            # hover/long-press since it doesn't fit the compact cell.
            title = f' title="{_esc(run.pace_brief())}"'
        cells.append(f'<div class="{cls}"{title}><div class="day-name">{abbr}</div>{body}</div>')
    return f'<section class="card"><h2>This week</h2><div class="week-grid">{"".join(cells)}</div></section>'


def _render_ladder(plan: TrainingPlan, db: Database, today: date) -> str:
    entries = []
    all_recent = {
        date.fromisoformat(a["start_time"][:10])
        for a in db.get_recent_activities(limit=60)
        if a.get("start_time")
    }
    for week in plan.weeks:
        if week.end_date < today:
            continue
        slot = _week_long_or_race_slot(week)
        if not slot or slot.distance_km <= 0:
            continue
        done = week.start_date <= today and bool(fulfilled_slots(plan, db, week) & {
            d for d in all_recent if week.start_date <= d <= week.end_date
        })
        entries.append({
            "label": f"Week {week.week_number} ({slot.workout_type})",
            "axis": week.start_date.strftime("%b %d"),
            "km": slot.distance_km,
            "is_race": slot.workout_type == "race" and slot.distance_km >= MARATHON_KM - 1,
            "done": done,
        })
    return f'<section class="card"><h2>Long run ladder to race day</h2>{_svg_ladder(entries)}</section>'


def _render_full_plan(plan: TrainingPlan) -> str:
    # A <table> forces its cells onto one line (see the CSS `white-space:
    # nowrap` rule shared by every other table on the page, which suits short
    # numeric cells) — the "Sessions" text here is too long for that, and on a
    # narrow phone that either overflows unreadably or gets crushed down to an
    # illegible font size. A wrapping block list avoids the problem entirely.
    rows = []
    for w in plan.weeks:
        sessions = " &middot; ".join(
            f"{WEEKDAY_ABBR[i]} {r.workout_type} {r.distance_km:g}km"
            for i, r in w.run_slots()
        ) or "rest week"
        rows.append(
            f'<div class="plan-week"><div class="plan-week-head">'
            f"<b>Wk {w.week_number}</b> {_esc(w.dates)} &middot; {_esc(w.phase)} "
            f'&middot; {w.weekly_km_target:g}km target</div>'
            f'<div class="plan-week-sessions">{sessions}</div></div>'
        )
    return f'<details class="card"><summary>Full plan ({len(plan.weeks)} weeks)</summary>' \
           f'<div class="plan-weeks">{"".join(rows)}</div></details>'


def _render_weekly_volume(plan: TrainingPlan, db: Database, today: date) -> str:
    series = _weekly_volume_series(plan, db, PLAN_START_DATE, today)
    return f'<section class="card"><h2>Weekly volume</h2>{_svg_volume_chart(series)}' \
           f'<div class="legend"><span class="sw sw-target"></span>Prescribed' \
           f'<span class="sw sw-actual"></span>Actual</div></section>'


def _render_zone_trend(plan: TrainingPlan, db: Database) -> str:
    z2 = resolve_z2_bounds(db, plan)
    if not z2:
        return '<section class="card"><h2>Time in Zone 1+2</h2><p class="empty">No HR zone data yet.</p></section>'
    lo, hi, _ = z2
    runs = list(reversed(db.get_recent_activities(limit=15)))
    dates = {date.fromisoformat(r["start_time"][:10]) for r in runs if r.get("start_time")}
    points = []
    for r in runs:
        st = r.get("start_time") or ""
        try:
            d = date.fromisoformat(st[:10])
        except ValueError:
            continue
        zd = compute_zone_distribution(r.get("hr_zones_json"), r.get("splits_json") or "", lo, hi)
        # Shift-aware, not get_prescribed_run's exact-date match — a long run
        # done two days late is still a long run for "why did this dip" to
        # make sense, and it keeps this chart consistent with the Recent Runs
        # table below, which already classifies shifted runs this way.
        resolved = plan.resolve_run_for_date(d, dates - {d})
        points.append({
            "date": st[:10],
            "pct": zd["easy_pct"] if zd else None,
            "workout_type": resolved.run.workout_type if resolved else "other",
        })
    return (
        f'<section class="card"><h2>Time in Zone 1+2 <span class="hint">last {len(runs)} runs</span></h2>'
        f'{_svg_zone_trend(points)}'
        f'<div class="legend"><span class="mk mk-legend-easy"></span>Easy/other'
        f'<span class="mk mk-legend-long"></span>Long/race</div></section>'
    )


def _render_easy_trend(plan: TrainingPlan, recent: list[dict]) -> str:
    trend = compute_easy_run_trend(plan, recent, n=8)
    if not trend:
        return '<section class="card"><h2>Easy-run pace vs HR</h2><p class="empty">Not enough easy runs yet.</p></section>'
    rows = "".join(
        f"<tr><td>{_esc(r['date'])}</td><td>{r['distance_km']:g} km</td><td>{_esc(r['pace'])}/km</td>"
        f"<td>{r['hr'] or '&mdash;'}</td><td>{r['hr_per_speed'] or '&mdash;'}</td>"
        f"<td>{r['drift_pct'] if r['drift_pct'] is not None else '&mdash;'}%</td></tr>"
        for r in trend["runs"]
    )
    table = (
        "<table><thead><tr><th>Date</th><th>Dist</th><th>Pace</th><th>HR</th>"
        f"<th>HR/speed</th><th>Drift</th></tr></thead><tbody>{rows}</tbody></table>"
    )
    return f'<section class="card"><h2>Easy-run pace vs HR</h2><div class="table-scroll">{table}</div></section>'


def _render_recent_runs(plan: TrainingPlan, recent: list[dict]) -> str:
    if not recent:
        return '<section class="card"><h2>Recent runs</h2><p class="empty">No runs recorded yet.</p></section>'
    dates = {
        date.fromisoformat(a["start_time"][:10]) for a in recent if a.get("start_time")
    }
    rows = []
    for i, a in enumerate(recent):
        st = a.get("start_time") or ""
        try:
            d = date.fromisoformat(st[:10])
        except ValueError:
            continue
        resolved = plan.resolve_run_for_date(d, dates - {d})
        prescribed = f"{resolved.run.workout_type}" + (f" ({resolved.shift_note()})" if resolved.shifted else "") \
            if resolved else "unplanned"
        drift = compute_hr_drift(a.get("splits_json") or "")
        drift_str = f"{drift['decoupling_pct']}%" if drift else "&mdash;"
        # recent is start_time DESC, so recent[i+1:] is exactly the runs strictly
        # before this one — compute_cadence_context takes its baseline from
        # list order, not by date, so passing the whole list-minus-self here
        # would let a LATER run's cadence leak into an EARLIER run's baseline.
        cad = compute_cadence_context(a, recent[i + 1:], n=5)
        cad_str = f"{cad['current']:g}" + (f" ({cad['delta']:+g})" if cad and cad.get("delta") is not None else "") \
            if cad else "&mdash;"
        rows.append(
            f"<tr><td>{_esc(st[:10])}</td><td>{_esc(prescribed)}</td>"
            f"<td>{(a.get('distance_km') or 0):.1f} km</td><td>{_esc(a.get('avg_pace_min_km') or '&mdash;')}/km</td>"
            f"<td>{a.get('avg_hr') or '&mdash;'}</td><td>{drift_str}</td><td>{cad_str}</td></tr>"
        )
    table = (
        "<table><thead><tr><th>Date</th><th>vs Plan</th><th>Dist</th><th>Pace</th>"
        f"<th>HR</th><th>Drift</th><th>Cadence</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )
    return f'<section class="card"><h2>Recent runs</h2><div class="table-scroll">{table}</div></section>'


# =========================================================================
# Page assembly
# =========================================================================

CSS = """
:root {
  --bg: #f6f7f6; --card: #ffffff; --ink: #1a1f1c; --ink-2: #55605a; --line: #dfe4e0;
  --good: #1a7a4c; --good-bg: #e4f5ea; --warn: #a6660a; --warn-bg: #fbeed7;
  --bad: #ab2b2b; --bad-bg: #fbe2e2; --info: #2b5aab; --info-bg: #e3ecfb;
  --muted: #8a938d; --muted-bg: #eef0ee; --accent: #1a7a4c; --accent-2: #a6660a;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #12140f; --card: #1b1f18; --ink: #eef0ec; --ink-2: #a9b3ac; --line: #2c332b;
    --good: #4fd394; --good-bg: #16321f; --warn: #f2b24a; --warn-bg: #3a2a0d;
    --bad: #f28a8a; --bad-bg: #3a1616; --info: #7fb1f2; --info-bg: #142338;
    --muted: #7d867f; --muted-bg: #232823; --accent: #4fd394; --accent-2: #f2b24a;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  padding: 16px 14px 40px; max-width: 760px; margin-inline: auto;
}
h1 { font-size: 22px; margin: 4px 0 2px; }
h2 { font-size: 15px; margin: 0 0 10px; display: flex; align-items: baseline; gap: 8px; }
.hint { font-size: 12px; color: var(--ink-2); font-weight: 400; }
.eyebrow { font-size: 11px; letter-spacing: .06em; text-transform: uppercase; color: var(--ink-2); }
.sub { color: var(--ink-2); font-size: 14px; margin-top: 2px; }
.freshness { font-size: 12px; color: var(--ink-2); margin-top: 8px; }
.freshness.stale { color: var(--warn); font-weight: 600; }
.top { margin-bottom: 18px; }
.tiles { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin-bottom: 18px; }
.tile { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; }
.tile-label { font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: var(--ink-2); }
.tile-value { font-size: 22px; font-weight: 700; margin-top: 2px; }
.tile-sub { font-size: 12px; color: var(--ink-2); margin-top: 2px; }
.tile-good .tile-value { color: var(--good); } .tile-warn .tile-value { color: var(--warn); }
.tile-bad .tile-value { color: var(--bad); } .tile-info .tile-value { color: var(--info); }
.card {
  background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 14px 16px; margin-bottom: 14px;
}
.card summary { cursor: pointer; font-size: 15px; font-weight: 600; }
.plan-weeks { margin-top: 10px; display: flex; flex-direction: column; gap: 8px; }
.plan-week { border-top: 1px solid var(--line); padding-top: 8px; font-size: 12.5px; }
.plan-week-head { color: var(--ink-2); }
.plan-week-head b { color: var(--ink); }
.plan-week-sessions { margin-top: 2px; }
.empty { color: var(--muted); font-size: 13px; }
.chips { display: flex; gap: 10px; flex-wrap: wrap; }
.chip {
  background: var(--muted-bg); border-radius: 8px; padding: 8px 12px;
  display: flex; flex-direction: column; min-width: 72px;
}
.chip-label { font-size: 10.5px; text-transform: uppercase; color: var(--ink-2); }
.chip-value { font-size: 17px; font-weight: 700; }
.chip-sub { font-size: 11px; color: var(--ink-2); }
.week-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 6px; }
.day { text-align: center; border-radius: 8px; padding: 8px 2px; background: var(--muted-bg); }
.day-name { font-size: 10.5px; color: var(--ink-2); text-transform: uppercase; }
.day-type { font-size: 12px; font-weight: 600; margin-top: 3px; text-transform: capitalize; }
.day-type.rest { color: var(--ink-2); font-weight: 400; }
.day-detail { font-size: 11px; color: var(--ink-2); }
.day-pace { font-size: 10px; color: var(--ink-2); margin-top: 1px; }
.day[title] { cursor: help; }
.day.today { outline: 2px solid var(--accent); }
.day.done { background: var(--good-bg); }
.day.missed { background: var(--bad-bg); }
svg { width: 100%; height: auto; display: block; }
.ax { font-size: 9px; fill: var(--ink-2); }
.val { font-size: 10px; fill: var(--ink-2); font-weight: 600; }
.grid { stroke: var(--line); stroke-width: 1; }
.bar-target { fill: none; stroke: var(--ink-2); stroke-width: 1.5; }
.bar-actual { fill: var(--accent); }
.bar-extra { fill: var(--accent-2); }
.bar-done { fill: var(--accent); }
.bar-race { fill: var(--bad); }
.legend { font-size: 11px; color: var(--ink-2); margin-top: 6px; display: flex; gap: 14px; align-items: center; }
.sw { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; vertical-align: -1px; }
.sw-target { border: 1.5px solid var(--ink-2); }
.sw-actual { background: var(--accent); }
.spark-line { stroke: var(--line-strong); stroke-width: 1.5; }
.spark-label { font-size: 12px; font-weight: 600; fill: var(--ink); }
.ref-line { stroke: var(--accent-2); stroke-width: 1; stroke-dasharray: 4 3; opacity: .7; }
.ref-label { font-size: 10px; fill: var(--accent-2); }
.mk-easy { fill: var(--accent); }
.mk-long { fill: var(--accent-2); }
.mk { display: inline-block; width: 9px; height: 9px; margin-right: 4px; vertical-align: -1px; }
.mk-legend-easy { border-radius: 50%; background: var(--accent); }
.mk-legend-long { background: var(--accent-2); transform: rotate(45deg); }
table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
th, td { text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--line); white-space: nowrap; }
th { color: var(--ink-2); font-weight: 600; font-size: 11px; text-transform: uppercase; }
.table-scroll { overflow-x: auto; }
footer { text-align: center; font-size: 11px; color: var(--muted); margin-top: 22px; }
"""


def _render_page(sections: list[str], generated_at: datetime) -> str:
    body = "\n".join(sections)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="900">
<title>Rundash</title>
<style>{CSS}</style>
</head>
<body>
{body}
<footer>Generated {generated_at.strftime('%Y-%m-%d %H:%M UTC')} &middot; regenerates after every Garmin poll</footer>
</body>
</html>
"""


# =========================================================================
# Orchestration
# =========================================================================


def run_dashboard(
    db_path: Path = DB_PATH,
    plan_path: Path = TRAINING_PLAN_PATH,
    output_dir: Path = DASHBOARD_DIR,
) -> bool:
    """Regenerate the dashboard HTML. Returns True on success.

    On any failure, logs + alerts and leaves whatever file was already on disk
    untouched (the write below is atomic — .tmp then os.replace — so a crash
    mid-render can never leave a torn/partial page for the server to serve).
    """
    db = Database(db_path)
    try:
        plan = TrainingPlan(str(plan_path))
        today = date.today()
        week = plan.get_week_for_date(today)

        recent = db.get_recent_activities(limit=15)
        ago_str, stale = _ago_string(db.get_last_poll_completed_at())

        sections = [
            _render_header(plan, today, week, ago_str, stale),
            _render_stat_tiles(
                compute_weekly_target(plan, db, today),
                compute_acr(db, today),
                compute_mileage_delta(db, today),
                compute_adherence(plan, db, today, lookback_runs=10),
                _longest_run_so_far(db, PLAN_START_DATE, today),
            ),
            _render_recovery(db.get_latest_wellness()),
            _render_this_week(plan, db, week, today),
            _render_ladder(plan, db, today),
            _render_full_plan(plan),
            _render_weekly_volume(plan, db, today),
            _render_zone_trend(plan, db),
            _render_easy_trend(plan, recent),
            _render_recent_runs(plan, recent),
        ]

        html = _render_page(sections, datetime.now(timezone.utc))

        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / "index.html"
        tmp = output_dir / "index.html.tmp"
        tmp.write_text(html, encoding="utf-8")
        os.replace(tmp, target)

        log.info("Dashboard regenerated: %s (%d bytes)", target, len(html))
        return True
    except Exception as e:
        log.exception("Dashboard generation failed")
        send_error_alert(f"Dashboard generation failed: {e!r}")
        return False
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(0 if run_dashboard() else 1)
