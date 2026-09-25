"""
Per-run logging setup and end-of-run diagnostic report.

Produces these files per run so an operator can hand over everything needed to
diagnose a migration without pasting terminal scrollback:

    logs/<prefix>_workspace_<run_id>.log   everything at the configured level
    logs/errors_<run_id>.log               warnings and errors only
    logs/report_<run_id>.txt               settings, counts, shortfalls, every issue
    stats/<prefix>_stats.json / .xlsx      counts plus the issue list

The report is populated from the logger: every warning and error becomes a
report line through ``ReportHandler``, so a warning added anywhere later is
reported without a matching hand-placed call.

Nothing here records an API token: only hosts, flags and project codes.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# Five additive levels. VERBOSE sits between INFO and DEBUG and carries request
# URIs and status codes, leaving DEBUG for parameters and bodies.
VERBOSE = 15
logging.addLevelName(VERBOSE, "VERBOSE")
LEVELS = {
    "error": logging.ERROR,
    "warn": logging.WARNING,
    "info": logging.INFO,
    "verbose": VERBOSE,
    "debug": logging.DEBUG,
}
_ALIASES = {"warning": "warn", "critical": "error"}


def normalise_level(level: Any) -> str:
    """Map a config value to one of the five level names. Unknown means info."""
    name = _ALIASES.get(str(level or "info").strip().lower(), str(level or "info").strip().lower())
    return name if name in LEVELS else "info"


def read_version() -> str:
    """This migration's version, from the VERSION file at the repo root."""
    try:
        path = Path(__file__).resolve().parent.parent / "VERSION"
        return path.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


VERSION = read_version()


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


class _RequestLineAsVerbose(logging.Filter):
    """urllib3 logs each request URI and status at DEBUG; show it at VERBOSE."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno == logging.DEBUG:
            record.levelno = VERBOSE
            record.levelname = "VERBOSE"
        return True


_PROJECT_IN_MESSAGE = re.compile(r"\bproject[=: ]+([A-Z][A-Z0-9]{1,9})\b")


class ReportHandler(logging.Handler):
    """Collects every warning and error as an end-of-run report entry."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.issues: List[Dict[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        # urllib3 announces its own retries; a retry that succeeds is not
        # something that failed to come across.
        if record.name.startswith("urllib3"):
            return
        try:
            message = record.getMessage().strip()
        except Exception:  # noqa: BLE001
            return
        if not message.strip("=- "):
            return
        from migration.step_logging import current_project

        project = current_project()
        if not project:
            match = _PROJECT_IN_MESSAGE.search(message)
            project = match.group(1) if match else "workspace"
        self.issues.append({
            "level": "error" if record.levelno >= logging.ERROR else "warn",
            "project": project,
            "step": record.name.rsplit(".", 1)[-1],
            "message": message.splitlines()[0][:500],
        })


REPORT = ReportHandler()


def setup_run_logging(
    run_id: str,
    level: str = "info",
    write_to_file: bool = True,
    log_dir: str = "logs",
    prefix: str = "",
) -> Dict[str, Optional[str]]:
    """
    Route logging to the console, and to a full log plus an errors-only log
    when ``write_to_file`` is true. Returns the paths for {full, errors, report}.
    Existing root handlers are replaced so this is safe to call once at startup.
    """
    level_no = LEVELS[normalise_level(level)]
    name = f"{prefix}_workspace_{run_id}.log" if prefix else f"workspace_{run_id}.log"
    paths: Dict[str, Optional[str]] = {"full": None, "errors": None, "report": None}

    formatter = logging.Formatter(LOG_FORMAT)
    root = logging.getLogger()
    root.setLevel(level_no)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level_no)
    console.setFormatter(formatter)
    root.addHandler(console)

    if write_to_file:
        os.makedirs(log_dir, exist_ok=True)
        paths = {
            "full": os.path.join(log_dir, name),
            "errors": os.path.join(log_dir, f"errors_{run_id}.log"),
            "report": os.path.join(log_dir, f"report_{run_id}.txt"),
        }
        # First line of every log is the version, so an attached log already
        # answers "which version are you on?".
        with open(paths["full"], "w", encoding="utf-8") as handle:
            handle.write(
                f"# qase-workspace-migration v{VERSION} | started "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            )
        full = logging.FileHandler(paths["full"], encoding="utf-8")
        full.setLevel(level_no)
        full.setFormatter(formatter)
        errors_only = logging.FileHandler(paths["errors"], encoding="utf-8")
        errors_only.setLevel(logging.WARNING)
        errors_only.setFormatter(formatter)
        root.addHandler(full)
        root.addHandler(errors_only)

    REPORT.issues.clear()
    root.addHandler(REPORT)

    request_log = logging.getLogger("urllib3.connectionpool")
    if level_no == VERBOSE:
        request_log.setLevel(logging.DEBUG)
        request_log.addFilter(_RequestLineAsVerbose())
    elif level_no > VERBOSE:
        request_log.setLevel(logging.WARNING)

    return paths


def print_issue_report(max_lines: int = 60, artifacts: Optional[List[str]] = None) -> None:
    """
    Print what the run skipped, degraded or failed, grouped by project. When
    there is nothing to report it says so, because silence is
    indistinguishable from a bug.
    """
    issues = REPORT.issues
    if not issues:
        print("\n------ Migration report: no skipped or degraded items ------")
    else:
        print(f"\n------ Migration report: {len(issues)} skipped/degraded/failed item(s) ------")
        by_project: Dict[str, List[Dict[str, str]]] = {}
        for issue in issues:
            by_project.setdefault(issue["project"], []).append(issue)
        shown = 0
        for project in sorted(by_project):
            items = by_project[project]
            print(f"\n  [{project}] {len(items)} item(s)")
            for issue in items:
                if shown >= max_lines:
                    break
                icon = "x" if issue["level"] == "error" else "!"
                print(f"    {icon} [{issue['step']}] {issue['message'][:240]}")
                shown += 1
        if len(issues) > shown:
            print(f"\n  ... +{len(issues) - shown} more, all listed in the run report")
    for line in artifacts or []:
        print(line)


def write_stats_files(stats: Any, prefix: str = "", stats_dir: str = "stats") -> List[str]:
    """Write stats/<prefix>_stats.json and .xlsx, both including the issue list."""
    base = f"{prefix}_stats" if prefix else "stats"
    os.makedirs(stats_dir, exist_ok=True)
    processed = dict(getattr(stats, "entities_processed", {}) or {})
    created = dict(getattr(stats, "entities_created", {}) or {})
    reused = dict(getattr(stats, "entities_reused", {}) or {})
    rows = [
        {
            "entity": entity,
            "source": processed.get(entity, 0),
            "migrated": created.get(entity, 0),
            "reused_from_previous_run": reused.get(entity, 0),
        }
        for entity in sorted(processed)
    ]
    written: List[str] = []
    json_path = os.path.join(stats_dir, f"{base}.json")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump({"version": VERSION, "entities": rows, "issues": REPORT.issues}, handle, indent=2)
    written.append(json_path)
    try:
        from openpyxl import Workbook
    except ImportError:
        logging.getLogger(__name__).warning(
            "openpyxl is not installed, so %s.xlsx was not written. "
            "Run: pip install -r requirements.txt", base,
        )
        return written
    book = Workbook()
    sheet = book.active
    sheet.title = "Entities"
    sheet.append(["entity", "source", "migrated", "reused_from_previous_run"])
    for row in rows:
        sheet.append(list(row.values()))
    issues_sheet = book.create_sheet("Issues")
    issues_sheet.append(["level", "project", "step", "message"])
    for issue in REPORT.issues:
        issues_sheet.append([issue["level"], issue["project"], issue["step"], issue["message"]])
    xlsx_path = os.path.join(stats_dir, f"{base}.xlsx")
    book.save(xlsx_path)
    written.append(xlsx_path)
    return written


def write_run_report(
    path: str,
    run_id: str,
    started_at: datetime,
    outcome: str,
    settings: Dict[str, Any],
    stats: Any,
    log_paths: Dict[str, str],
    notes: Optional[List[str]] = None,
) -> Optional[str]:
    """
    Write the end-of-run report. Never raises: a failed report must not mask the
    migration's own outcome.
    """
    try:
        finished_at = datetime.now()
        processed = dict(getattr(stats, "entities_processed", {}) or {})
        created = dict(getattr(stats, "entities_created", {}) or {})
        reused = dict(getattr(stats, "entities_reused", {}) or {})
        errors = list(getattr(stats, "errors", []) or [])

        lines: List[str] = []
        lines.append("=" * 72)
        lines.append("QASE WORKSPACE MIGRATION REPORT")
        lines.append("=" * 72)
        lines.append(f"run id      : {run_id}")
        lines.append(f"started     : {started_at.isoformat(timespec='seconds')}")
        lines.append(f"finished    : {finished_at.isoformat(timespec='seconds')}")
        lines.append(f"duration    : {str(finished_at - started_at).split('.')[0]}")
        lines.append(f"outcome     : {outcome}")
        lines.append(f"version     : {VERSION}")
        lines.append(f"python      : {platform.python_version()} on {platform.system()}")
        lines.append("")

        lines.append("SETTINGS (no credentials are recorded)")
        for key in sorted(settings):
            lines.append(f"  {key:<22}: {settings[key]}")
        lines.append("")

        lines.append("ENTITIES (migrated / source)")
        if processed:
            shortfalls = []
            for entity in sorted(processed):
                n_processed = processed.get(entity, 0)
                n_created = created.get(entity, 0)
                flag = ""
                if n_created < n_processed:
                    missing = n_processed - n_created
                    flag = f"   <-- {missing} MISSING"
                    shortfalls.append((entity, missing, n_processed))
                if reused.get(entity):
                    flag += f"   ({reused[entity]} already migrated by a previous run)"
                lines.append(f"  {entity:<22}: {n_created}/{n_processed}{flag}")
            lines.append("")
            if shortfalls:
                lines.append("SHORTFALLS: these did not fully migrate")
                for entity, missing, n_processed in shortfalls:
                    lines.append(f"  {entity}: {missing} of {n_processed} missing")
                lines.append(
                    "  The warnings and errors listed below say why."
                )
            else:
                lines.append("SHORTFALLS: none, every entity migrated in full")
        else:
            lines.append("  (nothing recorded — the run stopped before migrating)")
        lines.append("")

        lines.append(f"ERRORS ({len(errors)} recorded, all listed)")
        if errors:
            for i, err in enumerate(errors, 1):
                lines.append(f"  {i}. [{err.get('entity_type')}] {err.get('error')}")
                lines.append(f"     at {err.get('timestamp')}")
        else:
            lines.append("  none")
        lines.append("")

        lines.append(f"WARNINGS AND ERRORS FROM THE LOG ({len(REPORT.issues)}, all listed)")
        if REPORT.issues:
            for i, issue in enumerate(REPORT.issues, 1):
                lines.append(
                    f"  {i}. {issue['level']:<5} [{issue['project']}] [{issue['step']}] {issue['message']}"
                )
        else:
            lines.append("  none: nothing was skipped, degraded or failed")
        lines.append("")

        if notes:
            lines.append("NOTES")
            for note in notes:
                lines.append(f"  - {note}")
            lines.append("")

        lines.append("FILES TO SEND WHEN ASKING FOR HELP")
        lines.append(f"  this report : {path}")
        lines.append(f"  full log    : {log_paths.get('full')}")
        lines.append(f"  errors only : {log_paths.get('errors')}")
        lines.append("  trace       : migration_trace.jsonl (if enabled in config options)")
        lines.append("")
        lines.append("None of these contain API tokens. The full log and trace do contain")
        lines.append("project, case and user data from the migrated workspaces.")
        lines.append("=" * 72)

        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        return path
    except Exception as e:  # noqa: BLE001 - reporting must never break the run
        logging.getLogger(__name__).error("Could not write run report: %s", e)
        return None
