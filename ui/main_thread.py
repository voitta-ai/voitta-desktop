"""The single crossing point into AppKit's main thread.

macOS requires that every AppKit object be touched only from the main
thread. Violating that produces ``EXC_BAD_ACCESS`` — a hard crash with no
Python traceback, which is the worst possible failure mode to debug.

The rule used to be enforced by remembering to write ``AppHelper.callAfter``
at each of eight scattered call sites. A rule with no enforcement is one
that eventually gets missed, and the misses looked like random instability.
Decorating the boundary instead means the dispatch happens whether or not
the author remembered it.
"""

from __future__ import annotations

import functools
import logging
import threading
from typing import Callable, TypeVar

logger = logging.getLogger("voitta-desktop.main_thread")

F = TypeVar("F", bound=Callable)


def is_main_thread() -> bool:
    """True when the caller is already on the AppKit main thread."""
    return threading.current_thread() is threading.main_thread()


def on_main_thread(fn: F) -> F:
    """Ensure ``fn`` runs on the AppKit main thread.

    Called from the main thread, ``fn`` runs inline and its return value is
    passed through. Called from anywhere else, it is queued via
    ``AppHelper.callAfter`` and the wrapper returns ``None`` immediately —
    so do not use this for anything whose result the caller needs. Failures
    inside a queued call are logged rather than lost, since ``callAfter``
    has nowhere to propagate them.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if is_main_thread():
            return fn(*args, **kwargs)

        from PyObjCTools import AppHelper

        def _invoke():
            try:
                fn(*args, **kwargs)
            except Exception:
                logger.exception(
                    "main-thread call %s failed", getattr(fn, "__qualname__", fn)
                )

        AppHelper.callAfter(_invoke)
        return None

    return wrapper  # type: ignore[return-value]


def run_on_main(fn: Callable, *args, **kwargs) -> None:
    """Dispatch a one-off callable to the main thread.

    For call sites that aren't methods worth decorating.
    """
    on_main_thread(fn)(*args, **kwargs)
