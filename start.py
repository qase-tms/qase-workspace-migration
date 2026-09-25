#!/usr/bin/env python3
"""
Qase Workspace Migration

Migrates content from one Qase workspace to another, preserving structure,
links, attachments, and relationships.

    python start.py [config.json] [--dry-run]
"""
import sys

if sys.version_info < (3, 11):
    sys.exit(
        f"This migration requires Python 3.11 or newer "
        f"(found {sys.version_info.major}.{sys.version_info.minor})."
    )

import argparse
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from queue import Queue
from typing import Any, Dict, List, Tuple

from qase_service import QaseService
from migration.config import ConfigError, load_config, missing_projects, resolve_default_user, select_projects
from migration.progress import init_tqdm_lock, stderr_supports_progress
from migration.run_single_project import run_single_project_migration
from migration.utils import (
    MigrationMappings,
    MigrationStats,
    fork_mappings_for_parallel_project,
    merge_migration_stats,
    merge_parallel_project_into_main,
)
from migration.create import (
    migrate_projects,
    migrate_users,
    migrate_custom_fields,
    migrate_shared_parameters,
    migrate_attachments_workspace,
    migrate_groups,
)

from migration.run_report import (
    REPORT,
    VERSION,
    new_run_id,
    print_issue_report,
    setup_run_logging,
    write_run_report,
    write_stats_files,
)

RUN_ID = new_run_id()
LOG_PATHS: Dict[str, Any] = {"full": None, "errors": None, "report": None}
logger = logging.getLogger(__name__)


def _emit_deferred_project_summaries(
    entries: List[Tuple[str, str, Dict[str, str]]],
) -> None:
    """Log per-project created/processed lines once (after tqdm bars are finished)."""
    if not entries:
        return
    logger.info("")
    logger.info("=" * 60)
    logger.info("Per-project migration summaries")
    logger.info("=" * 60)
    for src, tgt, pst in sorted(entries, key=lambda x: x[0].lower()):
        logger.info("%s -> %s", src, tgt)
        for entity_type, count in pst.items():
            logger.info("  %s: %s", entity_type, count)
        logger.info("")


def _finish_report(
    started_at: "datetime",
    outcome: str,
    settings: Dict[str, Any],
    stats: MigrationStats,
    notes: List[str],
    prefix: str = "",
) -> None:
    """Print the migration report, write the stats and run report, list the files."""
    stats_files: List[str] = []
    try:
        stats_files = write_stats_files(stats, prefix)
    except Exception as e:  # noqa: BLE001 - reporting must never mask the outcome
        logger.error("Could not write the statistics files: %s", e)
    report_path = None
    if LOG_PATHS.get("report"):
        report_path = write_run_report(
            LOG_PATHS["report"],
            RUN_ID,
            started_at,
            outcome,
            settings,
            stats,
            LOG_PATHS,
            notes,
        )
    artifacts = ["", "=" * 60, "FILES FOR THIS RUN", "=" * 60]
    if report_path:
        artifacts.append(f"  report      : {report_path}   <-- send this one first")
    if LOG_PATHS.get("full"):
        artifacts.append(f"  full log    : {LOG_PATHS['full']}")
        artifacts.append(f"  errors only : {LOG_PATHS['errors']}")
    for path in stats_files:
        artifacts.append(f"  statistics  : {path}")
    artifacts.append("=" * 60)
    print_issue_report(artifacts=artifacts)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Migrate a Qase workspace to another Qase workspace."
    )
    parser.add_argument(
        "config", nargs="?", default="config.json",
        help="Path to the config file (default: config.json)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Read the source and report what would be created, writing nothing. "
             "Also enabled by the QASE_DRY_RUN environment variable.",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Continue a previous run from its mappings file (same as options.resume)",
    )
    parser.add_argument(
        "--skip-attachments", action="store_true",
        help="Skip the attachment step entirely (same as options.skip_attachments). "
             "Much faster for a trial run, but the result is not a complete migration.",
    )
    return parser.parse_args(argv)


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def main(argv=None) -> int:
    """Main migration function. Returns the process exit code."""
    args = parse_args(argv)
    
    global LOG_PATHS
    try:
        config, legacy_warnings = load_config(args.config)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    logging_cfg = config.get("logging") or {}
    prefix = str(config.get("prefix") or "").strip()
    LOG_PATHS = setup_run_logging(
        RUN_ID,
        level=logging_cfg.get("level", "info"),
        write_to_file=logging_cfg.get("write_to_file", True) is not False,
        log_dir=str(logging_cfg.get("dir") or "logs"),
        prefix=prefix,
    )
    for warning in legacy_warnings:
        logger.warning("Config: %s", warning)

    source_config = config.get("source") or {}
    target_config = config.get("target") or {}
    opts = config.get("options") or {}
    dry_run = args.dry_run or _truthy_env("QASE_DRY_RUN")
    resume = bool(args.resume or opts.get("resume", False))
    skip_attachments = bool(args.skip_attachments or opts.get("skip_attachments", False))
    preserve_ids = bool((config.get("cases") or {}).get("preserve_ids", False))
    allow_existing_target = bool((config.get("migration") or {}).get("allow_existing_target", False))
    mappings_file = str(opts.get("mappings_file") or "mappings.json")
    link_runs_to_plans = bool((config.get("runs") or {}).get("link_to_plan", False))

    missing_tokens = [
        name for name, block in (("source.api_token", source_config), ("target.api_token", target_config))
        if not str(block.get("api_token") or "").strip()
    ]
    if missing_tokens:
        logger.error(
            "Missing %s. Set it in %s or through the environment "
            "(QASE_SOURCE_API_TOKEN / QASE_TARGET_API_TOKEN).",
            " and ".join(missing_tokens), args.config,
        )
        return 1

    logger.info("="*60)
    logger.info("QASE WORKSPACE MIGRATION v%s%s", VERSION, " (DRY RUN)" if dry_run else "")
    logger.info("="*60)
    logger.info("Source: %s", source_config.get("host", "qase.io"))
    logger.info("Target: %s", target_config.get("host", "qase.io"))
    logger.info("="*60)

    source_scim_token = source_config.get('scim_token')
    source_scim_host = source_config.get('scim_host')
    target_scim_token = target_config.get('scim_token')
    target_scim_host = target_config.get('scim_host')

    source_kw: Dict[str, Any] = {
        "api_token": source_config.get("api_token"),
        "host": source_config.get("host", "qase.io"),
        "ssl": source_config.get("ssl", True) is not False,
        "scim_token": source_scim_token,
        "scim_host": source_scim_host,
    }
    target_kw: Dict[str, Any] = {
        "api_token": target_config.get("api_token"),
        "host": target_config.get("host", "qase.io"),
        "ssl": target_config.get("ssl", True) is not False,
        "scim_token": target_scim_token,
        "scim_host": target_scim_host,
    }
    source_service = QaseService(**source_kw)
    target_service = QaseService(**target_kw)

    # Project selection is resolved up front, so a mistyped code stops the run
    # before anything is written rather than being silently ignored.
    from migration.extract.projects import extract_projects

    try:
        source_codes = [p.get("code") for p in extract_projects(source_service) if p.get("code")]
    except Exception as e:
        logger.error("Could not list source projects: %s", e)
        return 1
    unknown = missing_projects(config, source_codes)
    if unknown:
        logger.error(
            "projects.import lists %s, which do not exist in the source workspace. "
            "Available: %s", unknown, sorted(source_codes),
        )
        return 1
    selected = select_projects(config, source_codes)
    if not selected:
        logger.error(
            "No projects selected. List project codes in projects.import, "
            "or set projects.import_all: true."
        )
        return 1

    users_config = config.setdefault("users", {})
    try:
        default_user_id, detail = resolve_default_user(target_service, users_config.get("default"))
    except Exception as e:
        logger.error("Could not resolve users.default against the target workspace: %s", e)
        return 1
    if default_user_id is None:
        logger.error("users.default: %s", detail)
        return 1
    logger.info("users.default: %s (used for authors with no match in the target)", detail)
    users_config["default"] = default_user_id

    if dry_run:
        from migration.dry_run import run_dry_run

        mappings = MigrationMappings()
        if resume:
            mappings.load_from_file(mappings_file)
        stats = run_dry_run(
            source_service, target_service, selected, config, mappings,
            allow_existing_target=allow_existing_target, resume=resume,
            skip_attachments=skip_attachments,
            link_runs_to_plans=link_runs_to_plans,
        )
        stats.print_summary(dry_run=True)
        print_issue_report()
        failed = any(i["level"] == "error" for i in REPORT.issues)
        print("\nDry run complete. Nothing was written to the target workspace.")
        return 1 if failed else 0

    mappings = MigrationMappings()
    mappings.default_user_id = default_user_id
    stats = MigrationStats()

    started_at = datetime.now()
    outcome = "incomplete (run did not reach the end)"
    report_notes: List[str] = []
    report_settings = {
        "version": VERSION,
        "source host": source_kw["host"],
        "target host": target_kw["host"],
        "projects": ", ".join(selected),
        "preserve_ids": preserve_ids,
        "resume": resume,
        "skip_attachments": skip_attachments,
        "allow_existing_target": allow_existing_target,
        "runs.link_to_plan": link_runs_to_plans,
        "users.default": default_user_id,
        "mappings file": mappings_file,
    }
    if LOG_PATHS.get("full"):
        logger.info("Logs for this run: %s", LOG_PATHS["full"])

    trace_file_cfg = opts.get("migration_trace_file", "migration_trace.jsonl")
    if "migration_trace_file" in opts and opts["migration_trace_file"] in (False, None, ""):
        trace_file_cfg = None
    trace_full = bool(opts.get("migration_trace_full_payloads", False))
    if trace_file_cfg:
        from migration.trace_log import MigrationTrace

        mappings.trace = MigrationTrace(str(trace_file_cfg), full_payloads=trace_full)
        logger.info("Migration trace (JSONL): %s (full_payloads=%s)", trace_file_cfg, trace_full)
        mappings.trace.event(
            "migration_start",
            source_host=source_kw["host"],
            target_host=target_kw["host"],
            mappings_file=mappings_file,
            resume=resume,
        )
    
    if resume:
        mappings.load_from_file(mappings_file)
        mappings.default_user_id = default_user_id
    
    try:
        logger.info("\n" + "="*60)
        logger.info("STEP 1: Migrating Projects")
        logger.info("="*60)
        resumed_projects = set(mappings.projects) if resume else set()
        projects = migrate_projects(
            source_service, 
            target_service, 
            mappings, 
            stats,
            only_projects=selected,
        )

        # A target project that already holds data is only written to when this
        # migration created it (resume) or the operator explicitly allowed it.
        kept = []
        for project in projects:
            code = project["source_code"]
            if project.get("has_data") and code not in resumed_projects and not allow_existing_target:
                logger.error(
                    "Target project %s already contains data, so it was skipped to avoid "
                    "duplicating into it. Use --resume if a previous run of this migration "
                    "created it, or set migration.allow_existing_target: true to add to it anyway.",
                    project["target_code"],
                )
                continue
            kept.append(project)
        projects = kept
        
        if not projects:
            logger.error("No projects to migrate!")
            outcome = "failed: no projects to migrate"
            _finish_report(started_at, outcome, report_settings, stats, report_notes, prefix)
            return 1
        
        logger.info(f"Found {len(projects)} project(s) to migrate")
        
        mappings.save_to_file(mappings_file)
        
        # Check if user migration is enabled
        users_config = config.get('users', {})
        migrate_users_flag = users_config.get('migrate', False)
        
        if migrate_users_flag:
            logger.info("\n" + "="*60)
            logger.info("STEP 2: Migrating Users (Workspace Level)")
            logger.info("="*60)
            try:
                user_mapping = migrate_users(source_service, target_service, mappings, stats, config)
                # Ensure user_mapping keys are integers (in case it was loaded from JSON with string keys)
                if user_mapping:
                    first_key = next(iter(user_mapping.keys()), None)
                    if first_key is not None and isinstance(first_key, str):
                        user_mapping = {int(k): v for k, v in user_mapping.items()}
                mappings.save_to_file(mappings_file)
                
                logger.info("\n" + "="*60)
                logger.info("STEP 2.5: Migrating Groups (Workspace Level)")
                logger.info("="*60)
                try:
                    group_mapping = migrate_groups(source_service, target_service, user_mapping, mappings, stats, config)
                    mappings.save_to_file(mappings_file)
                except Exception as e:
                    logger.error(f"✗ Groups migration failed: {e}", exc_info=True)
                    mappings.save_to_file(mappings_file)
            except Exception as e:
                logger.error(f"✗ User migration failed: {e}", exc_info=True)
                # Create fallback mapping using default user ID
                logger.warning("Creating fallback user mapping using default user ID")
                from migration.extract.users import extract_users
                try:
                    source_users = extract_users(source_service)
                    default_user_id = users_config.get('default', 1)
                    user_mapping = {user.get('id'): default_user_id for user in source_users if user.get('id')}
                    mappings.users = user_mapping
                    stats.add_entity('users', len(source_users), len(user_mapping))
                except Exception as fallback_error:
                    logger.error(f"Failed to create fallback user mapping: {fallback_error}")
                    user_mapping = {}
                mappings.save_to_file(mappings_file)
        else:
            logger.info("\n" + "="*60)
            logger.info("STEP 2: User Migration (SKIPPED - users.migrate: false)")
            logger.info("="*60)
            logger.info("User migration is disabled. Skipping user and group migration entirely.")
            # Create empty user mapping - will use default user ID (1) for all references
            user_mapping = {}
            mappings.users = user_mapping
            mappings.save_to_file(mappings_file)
        
        logger.info("\n" + "="*60)
        logger.info("STEP 3: Migrating Custom Fields (Workspace Level)")
        logger.info("="*60)
        custom_field_mapping = migrate_custom_fields(
            source_service, target_service,
            mappings, stats
        )
        mappings.save_to_file(mappings_file)
        
        logger.info("\n" + "="*60)
        logger.info("STEP 4: Migrating Shared Parameters (Workspace Level)")
        logger.info("="*60)
        project_codes_list = [p['source_code'] for p in projects]
        shared_parameter_mapping = migrate_shared_parameters(
            source_service, target_service,
            project_codes_list, mappings, stats
        )
        mappings.save_to_file(mappings_file)
        
        if skip_attachments:
            logger.warning("\n" + "="*60)
            logger.warning("STEP 5: Attachments SKIPPED (--skip-attachments)")
            logger.warning("="*60)
            logger.warning(
                "No attachments will be uploaded. Cases, runs and results still "
                "migrate, but any attachment they reference will be missing, and "
                "inline images in descriptions and steps will not resolve. Re-run "
                "without this flag for a complete migration."
            )
            report_notes.append(
                "Attachments were SKIPPED via --skip-attachments. This target is not a "
                "complete migration: attachment references on cases, steps and results "
                "are absent. Re-run without the flag to migrate them."
            )
            attachment_mapping = {}
        else:
            logger.info("\n" + "="*60)
            logger.info("STEP 5: Migrating Attachments (Workspace Level)")
            logger.info("="*60)
            attachment_mapping = migrate_attachments_workspace(
                source_service, target_service,
                projects, mappings, stats
            )
        mappings.save_to_file(mappings_file)

        parallel_projects = bool(opts.get("parallel_project_migration", True))
        try:
            max_parallel_projects = max(1, int(opts.get("max_parallel_projects", 4)))
        except (TypeError, ValueError):
            max_parallel_projects = 4
        max_parallel_projects = min(max_parallel_projects, max(1, len(projects)))

        show_project_progress = bool(opts.get("show_project_progress", True))
        use_project_progress_bars = show_project_progress and stderr_supports_progress()
        if use_project_progress_bars:
            init_tqdm_lock()

        deferred_project_summaries: List[Tuple[str, str, Dict[str, str]]] = []

        progress_position_queue: Queue[int] = Queue()
        for _pos in range(max_parallel_projects):
            progress_position_queue.put(_pos)

        def _run_parallel_project_worker(project: Dict[str, Any]):
            bar_pos = progress_position_queue.get()
            try:
                src = QaseService(**source_kw)
                tgt = QaseService(**target_kw)
                wm = fork_mappings_for_parallel_project(mappings)
                wstats = MigrationStats()
                pst = run_single_project_migration(
                    project,
                    src,
                    tgt,
                    wm,
                    wstats,
                    user_mapping,
                    custom_field_mapping,
                    shared_parameter_mapping,
                    preserve_ids,
                    mappings_file=None,
                    show_project_progress=use_project_progress_bars,
                    progress_position=bar_pos,
                    emit_summary_logs=False,
                    link_runs_to_plans=link_runs_to_plans,
                )
                return project["source_code"], project["target_code"], wm, wstats, pst
            finally:
                progress_position_queue.put(bar_pos)

        if parallel_projects and len(projects) > 1:
            logger.info(
                "Running per-project migration in parallel (%s workers). "
                "Intermediate mappings saves only after each project completes; "
                "set options.parallel_project_migration false for step-by-step saves.",
                max_parallel_projects,
            )
            futures = {}
            with ThreadPoolExecutor(max_workers=max_parallel_projects) as pool:
                for project in projects:
                    futures[pool.submit(_run_parallel_project_worker, project)] = project
                for fut in as_completed(futures):
                    proj = futures[fut]
                    try:
                        psrc, _ptgt, wm, wst, pst = fut.result()
                        deferred_project_summaries.append((psrc, _ptgt, pst))
                        merge_parallel_project_into_main(mappings, wm, psrc)
                        merge_migration_stats(stats, wst)
                        mappings.save_to_file(mappings_file)
                    except Exception as e:
                        logger.error(
                            "✗ Project %s migration failed: %s",
                            proj.get("source_code"),
                            e,
                            exc_info=True,
                        )
                        mappings.save_to_file(mappings_file)
        else:
            if len(projects) > 1 and not parallel_projects:
                logger.info("Parallel project migration disabled (options.parallel_project_migration).")
            for project in projects:
                pst = run_single_project_migration(
                    project,
                    source_service,
                    target_service,
                    mappings,
                    stats,
                    user_mapping,
                    custom_field_mapping,
                    shared_parameter_mapping,
                    preserve_ids,
                    mappings_file=mappings_file,
                    show_project_progress=use_project_progress_bars,
                    progress_position=0,
                    emit_summary_logs=False,
                    link_runs_to_plans=link_runs_to_plans,
                )
                deferred_project_summaries.append(
                    (project["source_code"], project["target_code"], pst)
                )

        _emit_deferred_project_summaries(deferred_project_summaries)

        stats.print_summary()

        mappings.save_to_file(mappings_file)
        logger.info(f"\nMigration complete! Mappings saved to {mappings_file}")
        outcome = "completed"
        if getattr(mappings, "trace", None):
            mappings.trace.event("migration_complete", mappings_file=mappings_file)

    except KeyboardInterrupt:
        logger.warning("\nMigration interrupted by user")
        outcome = "interrupted by user"
        mappings.save_to_file(mappings_file)
        logger.info(f"Mappings saved to {mappings_file}. Use --resume to continue.")
        if getattr(mappings, "trace", None):
            mappings.trace.event("migration_interrupted")
        _finish_report(started_at, outcome, report_settings, stats, report_notes, prefix)
        return 1
    except Exception as e:
        logger.error(f"\nMigration failed with error: {e}", exc_info=True)
        outcome = f"failed: {e}"
        mappings.save_to_file(mappings_file)
        logger.info(f"Mappings saved to {mappings_file}. Use --resume to continue.")
        if getattr(mappings, "trace", None):
            mappings.trace.event("migration_failed", error=str(e))
        _finish_report(started_at, outcome, report_settings, stats, report_notes, prefix)
        return 1
    finally:
        if getattr(mappings, "trace", None):
            try:
                mappings.trace.close()
            except Exception:
                pass
            mappings.trace = None

    has_errors = bool(stats.errors) or any(i["level"] == "error" for i in REPORT.issues)
    if has_errors:
        outcome = "completed with errors (see the migration report)"
    _finish_report(started_at, outcome, report_settings, stats, report_notes, prefix)
    return 1 if has_errors else 0


if __name__ == '__main__':
    sys.exit(main())
