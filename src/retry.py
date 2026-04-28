"""Tiny retry helper shared across modules that talk to flaky external APIs.

Originally lived in garmin_client.py; lifted here so telegram_bot can use it
without an awkward cross-module import. Pattern: log warnings, never raise,
return None on final failure (callers check for None).
"""

import logging
import time

log = logging.getLogger(__name__)


def with_retry(
    fn,
    *args,
    _label: str = "call",
    _attempts: int = 3,
    _backoff: float = 1.0,
    **kwargs,
):
    """Run fn(*args, **kwargs) with retries + exponential backoff.

    Returns the call's result, or None on final failure. Failures are logged
    as warnings — the caller decides whether None is recoverable.
    """
    for attempt in range(_attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if attempt < _attempts - 1:
                wait = _backoff * (2 ** attempt)
                log.warning(
                    "%s attempt %d/%d failed: %r (retry in %.1fs)",
                    _label, attempt + 1, _attempts, e, wait,
                )
                time.sleep(wait)
            else:
                log.warning("%s failed after %d attempts: %r", _label, _attempts, e)
    return None
