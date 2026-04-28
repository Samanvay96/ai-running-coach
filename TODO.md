# Open follow-ups

Tracked here so each Claude session can pick up where the last left off without
spelunking through git log. Append new items at the end of a session; tick off
or delete completed ones at the start of the next.

## Coach quality

- [x] ~~Rich context in `chat()`~~ — done 2026-04-28: latest analysis,
  ACR, weekly target, adherence, wellness, and upcoming 3 days now in chat.
- [x] ~~Plan adherence tracking~~ — done 2026-04-28: `compute_adherence` in
  `coach.py`, surfaced in `/status` and fed into `analyze_run`.
- [x] ~~Race-aware milestones~~ — done 2026-04-28: `compute_weekly_target`
  surfaces actual-vs-prescribed km with days remaining; in `/status`,
  `analyze_run`, and chat.
- [x] ~~Cross-run trend awareness~~ — done 2026-04-28: `compute_easy_run_trend`
  shows last 4 easy runs with HR-per-speed and drift% in `analyze_run`.

- [ ] **Pull running power / power zones** *(if watch supports)* — power is a
  more stable effort signal than HR for short or hot runs. Currently we don't
  read it. One Garmin field at extraction time + one prompt line.

- [ ] **Track perceived effort** — bot prompts "how hard did that feel? 1-10"
  after each run; stored on the activity. Subjective vs objective HR/pace
  divergence is a strong injury/illness predictor.

- [ ] **Weekly mood/energy check-in** — Sunday Telegram nudge: "1-10 on legs,
  1-10 on motivation." Catches mental burnout that wellness data misses.

## Robustness

- [ ] **Telegram send retry wrapper** — lift `_with_retry` from
  `src/garmin_client.py:13` and wrap `_send_message` / `_send_document` in
  `src/telegram_bot.py`. 3 attempts, exponential backoff. Cheap insurance
  against transient Telegram 502s; failure alerts will surface it if it fires.

- [ ] **Heartbeat in daily alert** — if the poll timer ever stops firing
  (systemd quirk, reboot loop), today you'd notice only via missing analyses.
  In `src/alerts.py`: "if last activity poll >12 h ago, send alert."

- [ ] **Smoke test for `analyze_run`** — no test suite in the repo yet. A
  single test that mocks an Anthropic response with only a thinking block
  would have caught the Apr 28 silent-failure bug before it shipped. Worth
  one test per LLM call site (`analyze_run`, `weekly_summary`, `chat`).

## Cost / efficiency

- [ ] **Proper system-prompt split for caching** — Apr 28 added
  `cache_control` on the existing system string (`src/coach.py:446, 577, 602`),
  but `_build_system_prompt()` is below the 1024-token Sonnet cache minimum
  *and* contains dynamic content (today's date, latest wellness). Splitting
  into static (race date, plan structure, pace zones, benchmarks) + dynamic
  (today's date, latest training status, RHR) would make caching actually fire.
  Only worth doing if API cost ever matters — currently small.

## Cleanup

- [ ] **Reconsider `replay_analyze.py`** — useful diagnostic, but if it stays
  unused for 30+ days it can probably go. `git log -- scripts/replay_analyze.py`
  to check last touch.

---

*Don't track in here:* anything already in code (file paths, function names,
patterns). This file is for *intentions* — what we'd like to do next. Memory
in `~/.claude/projects/-home-skarambhe-Projects-ai-running-coach/memory/` is
for facts that persist (user role, project context, ops references).*
