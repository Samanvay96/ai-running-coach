# Open follow-ups

Tracked here so each Claude session can pick up where the last left off without
spelunking through git log. Append new items at the end of a session; tick off
or delete completed ones at the start of the next.

## Coach quality

- [ ] **Rich context in `chat()`** — `Coach.chat()` (`src/coach.py:566-590`) only
  passes `format_recent_activities` (last 5 runs as date/distance/duration/pace/HR).
  Including the most recent saved `coaching_response`, current ACR, latest
  wellness row, and the next ~3 days of prescription would 10× the chat utility.
  Biggest UX gap right now — when you ask "where's my analysis?" the response
  is thin because the path can't see the rich data the poller already saved.
  ~30 lines of plumbing in `chat()`.

- [ ] **Plan adherence tracking** — surface "completed N of last 10 prescribed
  runs" in `/status`, and feed it into `analyze_run` so the coach knows whether
  you're in a fragile restart vs a steady block. Particularly relevant during
  the current restart phase. Helper would live in `src/db.py` or `src/coach.py`.

- [ ] **Race-aware milestones in the prompt** — countdown is shown but the
  coach doesn't reason against weekly targets ("by week 20 you should be hitting
  X km/week"). Worth a prompt iteration once the foundation work is done.

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
