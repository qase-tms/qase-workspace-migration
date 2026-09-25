"""
Console-friendly step logging during per-project tqdm progress.

Lives in its own module to avoid circular imports (``migration.progress`` loads
``migration.extract``, which must not import ``progress`` during package init).
"""
from __future__ import annotations

import logging
import threading
from typing import Any

_migration_console_quiet = threading.local()


def set_migration_progress_console_quiet(active: bool) -> None:
    """
    When True, repetitive migration step logs use DEBUG so tqdm can own the console
    (thread-local for parallel per-project workers).
    """
    _migration_console_quiet.active = bool(active)


def migration_progress_console_quiet() -> bool:
    return bool(getattr(_migration_console_quiet, "active", False))


def step_log_info(
    log: logging.Logger, msg: str, *args: Any, **kwargs: Any
) -> None:
    """INFO, or DEBUG while a project progress bar is active."""
    if migration_progress_console_quiet():
        log.debug(msg, *args, **kwargs)
    else:
        log.info(msg, *args, **kwargs)


_active_projects: dict = {}
_active_lock = threading.Lock()


def set_current_project(code: str | None) -> None:
    """Project this thread is migrating, used to group the end-of-run report."""
    previous = getattr(_migration_console_quiet, "project", None)
    _migration_console_quiet.project = code
    with _active_lock:
        if previous and previous != code:
            _active_projects[previous] = _active_projects.get(previous, 1) - 1
            if _active_projects[previous] <= 0:
                _active_projects.pop(previous, None)
        if code and code != previous:
            _active_projects[code] = _active_projects.get(code, 0) + 1


def current_project() -> str | None:
    """
    This thread's project. Helper threads inside a step have none of their own,
    so when exactly one project is being migrated, that one is theirs.
    """
    code = getattr(_migration_console_quiet, "project", None)
    if code:
        return code
    with _active_lock:
        if len(_active_projects) == 1:
            return next(iter(_active_projects))
    return None
