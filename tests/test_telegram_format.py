"""Tests for the markdown → Telegram-HTML translator in src/telegram_bot.py.

Background: prior to this, run analyses arrived with literal asterisks and
"---" lines because Telegram defaulted to plain-text rendering. Screenshot
from May 14 showed every "**bold**" in the LLM output as **bold** rather
than rendered bold. The format_for_telegram + parse_mode=HTML wiring fixes
this; these tests pin its behaviour so we don't regress.
"""

import sys
import types


def _import_format(monkeypatch=None):
    """Import format_for_telegram in isolation, stubbing telegram libs since the
    full bot module won't load without python-telegram-bot installed."""
    # Stub the telegram package so the import chain doesn't blow up
    pkg = types.ModuleType("telegram")
    pkg.Bot = type("Bot", (), {})
    pkg.Update = type("Update", (), {})
    constants = types.ModuleType("telegram.constants")
    constants.ParseMode = types.SimpleNamespace(HTML="HTML")
    ext = types.ModuleType("telegram.ext")
    for name in ["Application", "CommandHandler", "MessageHandler", "filters"]:
        setattr(ext, name, type(name, (), {}))
    # ContextTypes.DEFAULT_TYPE is referenced as a type annotation in CoachBot
    ext.ContextTypes = type("ContextTypes", (), {"DEFAULT_TYPE": object})
    sys.modules.setdefault("telegram", pkg)
    sys.modules["telegram.constants"] = constants
    sys.modules["telegram.ext"] = ext

    # Env stubs for config
    import os
    for var, val in [
        ("GARMIN_EMAIL", "x"), ("GARMIN_PASSWORD", "x"),
        ("TELEGRAM_BOT_TOKEN", "x"), ("TELEGRAM_CHAT_ID", "x"),
        ("ANTHROPIC_API_KEY", "x"), ("RUNNER_AGE", "30"),
    ]:
        os.environ.setdefault(var, val)
    # Stub anthropic / openpyxl / dotenv ONLY if they're not already installed.
    # Previously this unconditionally overwrote `openpyxl.load_workbook` etc. on
    # the real module, which silently broke any later test (e.g. test_coach)
    # that actually tried to load the xlsx.
    stubs = [
        ("anthropic", "Anthropic", type("A", (), {"__init__": lambda self, **kw: None})),
        ("openpyxl", "load_workbook", lambda *a, **kw: None),
        ("dotenv", "load_dotenv", lambda *a, **kw: None),
    ]
    for name, attr, val in stubs:
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
            setattr(sys.modules[name], attr, val)

    from src.telegram_bot import format_for_telegram
    return format_for_telegram


format_for_telegram = _import_format()


def test_bold_converted_to_html():
    assert format_for_telegram("**Verdict:** ok") == "<b>Verdict:</b> ok"


def test_multiple_bold_segments():
    out = format_for_telegram("**Verdict:** ok | **Watch:** legs")
    assert out == "<b>Verdict:</b> ok | <b>Watch:</b> legs"


def test_code_converted_to_html():
    assert format_for_telegram("run `git status`") == "run <code>git status</code>"


def test_horizontal_rule_removed():
    """The screenshot showed literal --- lines; these should drop out cleanly."""
    src = "Section A\n---\nSection B"
    out = format_for_telegram(src)
    assert "---" not in out
    assert "Section A" in out and "Section B" in out


def test_horizontal_rule_with_padding():
    """`---` with surrounding whitespace on the same line still counts."""
    assert "---" not in format_for_telegram("Foo\n   ---   \nBar")


def test_html_escape_keeps_greater_than():
    """LLM emits things like 'drift >5%' — > must render as > not break the parser."""
    out = format_for_telegram("drift >5% & humidity <70%")
    assert "&gt;5%" in out
    assert "&lt;70%" in out
    assert "&amp;" in out


def test_html_escape_inside_bold():
    """A literal `<` inside bold text shouldn't break the tag."""
    out = format_for_telegram("**ratio >1.5**")
    assert out == "<b>ratio &gt;1.5</b>"


def test_no_false_positive_for_multiplication():
    """Single asterisks (e.g. multiplication '3*2') must NOT become italic — the
    formatter only handles ** for bold, leaving stray asterisks untouched."""
    out = format_for_telegram("formula 3*2 = 6")
    assert "<i>" not in out
    assert "3*2" in out


def test_collapses_excess_blank_lines():
    """After removing ---, we shouldn't be left with three+ consecutive newlines."""
    out = format_for_telegram("A\n\n---\n\nB")
    assert "\n\n\n" not in out


def test_plain_text_passes_through():
    assert format_for_telegram("just plain text") == "just plain text"


def test_idempotent_for_text_without_markdown():
    """Calling twice on text without markdown should be a no-op after first pass."""
    once = format_for_telegram("hello world")
    twice = format_for_telegram(once)
    assert once == twice == "hello world"


def test_real_run_analysis_snippet():
    """Anchor on the actual output style from the May 14 screenshot."""
    src = (
        "**Week 8 | Thursday Easy | May 14**\n\n"
        "**Verdict:** Cautious recovery run — HR well-managed, but pace drifted slow.\n\n"
        "---\n\n"
        "**Prescribed vs Actual**\n"
        "Target: 4 km @ 6:15/km | Actual: 4 km @ 6:55/km (+40 sec/km slower)."
    )
    out = format_for_telegram(src)
    # Bold tags rendered
    assert "<b>Verdict:</b>" in out
    assert "<b>Prescribed vs Actual</b>" in out
    # Separator stripped
    assert "---" not in out
    # No literal asterisks left
    assert "**" not in out
