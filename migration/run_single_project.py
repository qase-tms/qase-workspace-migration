"""
Per-project migration steps (milestones through defects).

Used sequentially from start.py or in parallel workers with a forked
MigrationMappings (see migration.utils fork/merge helpers).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from qase_service import QaseService
from migration.progress import (
    ProjectMigrationProgress,
    prefetch_project_migration_profile,
    stderr_supports_progress,
)
from migration.step_logging import (
    set_current_project,
    set_migration_progress_console_quiet,
    step_log_info,
)
from migration.utils import MigrationMappings, MigrationStats
from migration.create import (
    migrate_milestones,
    migrate_configurations,
    migrate_environments,
    migrate_shared_steps,
    migrate_suites,
    migrate_cases,
    migrate_plans,
    migrate_runs,
    migrate_results,
    migrate_defects,
)

logger = logging.getLogger(__name__)


def run_single_project_migration(
    project: Dict[str, Any],
    source_service: QaseService,
    target_service: QaseService,
    mappings: MigrationMappings,
    stats: MigrationStats,
    user_mapping: Dict[Any, Any],
    custom_field_mapping: Any,
    shared_parameter_mapping: Any,
    preserve_ids: bool,
    mappings_file: Optional[str] = None,
    show_project_progress: bool = True,
    progress_position: int = 0,
    emit_summary_logs: bool = True,
    link_runs_to_plans: bool = False,
) -> Dict[str, str]:
    """
    Run milestones → defects for one project. Mutates mappings and stats.

    If mappings_file is set, saves after each sub-step (resume-friendly).
    If None, skips saves (parallel workers; parent merges and saves).

    Set ``emit_summary_logs=False`` when the orchestrator prints summaries once
    at the end (avoids INFO lines interrupting tqdm between projects).

    Returns:
        Per-entity ``created/processed`` strings for this project (same keys as summary).
    """
    project_code_source = project["source_code"]
    project_code_target = project["target_code"]
    set_current_project(project_code_source)

    tr = getattr(mappings, "trace", None)
    if tr:
        tr.event(
            "project_start",
            project_source=project_code_source,
            project_target=project_code_target,
        )

    progress: Optional[ProjectMigrationProgress] = None
    source_runs_precached: Optional[List[Dict[str, Any]]] = None
    if show_project_progress and stderr_supports_progress():
        try:
            profile = prefetch_project_migration_profile(
                source_service, project_code_source
            )
            source_runs_precached = profile.source_runs
            progress = ProjectMigrationProgress(
                project_code_source,
                profile.cases_total,
                profile.runs_total,
                profile.results_total,
                position=progress_position,
            )
        except Exception as e:
            logger.debug("Project progress prefetch failed: %s", e, exc_info=True)

    if progress is not None:
        set_migration_progress_console_quiet(True)

    project_stats: Dict[str, str] = {}

    def _save() -> None:
        if mappings_file:
            mappings.save_to_file(mappings_file)

    try:
        step_log_info(logger, "\n" + "=" * 60)
        step_log_info(
            logger,
            "Migrating project: %s -> %s",
            project_code_source,
            project_code_target,
        )
        step_log_info(logger, "=" * 60)

        step_log_info(logger, "\nMigrating milestones for %s...", project_code_source)
        try:
            milestone_mapping = migrate_milestones(
                source_service,
                target_service,
                project_code_source,
                project_code_target,
                mappings,
                stats,
            )
            _save()
        except Exception as e:
            logger.error("✗ Milestones migration failed: %s", e, exc_info=True)
            milestone_mapping = {}
            _save()
        
        step_log_info(logger, "\nMigrating configurations for %s...", project_code_source)
        try:
            config_group_mapping, config_mapping = migrate_configurations(
                source_service,
                target_service,
                project_code_source,
                project_code_target,
                mappings,
                stats,
            )
            _save()
        except Exception as e:
            logger.error("✗ Configurations migration failed: %s", e, exc_info=True)
            config_group_mapping = {}
            config_mapping = {}
            _save()
        
        step_log_info(logger, "\nMigrating environments for %s...", project_code_source)
        try:
            environment_mapping = migrate_environments(
                source_service,
                target_service,
                project_code_source,
                project_code_target,
                mappings,
                stats,
            )
            _save()
        except Exception as e:
            logger.error("✗ Environments migration failed: %s", e, exc_info=True)
            environment_mapping = {}
            _save()
        
        step_log_info(logger, "\nMigrating shared steps for %s...", project_code_source)
        try:
            shared_step_mapping = migrate_shared_steps(
                source_service,
                target_service,
                project_code_source,
                project_code_target,
                mappings,
                stats,
            )
            _save()
        except Exception as e:
            logger.error("✗ Shared steps migration failed: %s", e, exc_info=True)
            shared_step_mapping = {}
            _save()
        
        step_log_info(logger, "\nMigrating suites for %s...", project_code_source)
        try:
            suite_mapping = migrate_suites(
                source_service,
                target_service,
                project_code_source,
                project_code_target,
                mappings,
                stats,
            )
            _save()
        except Exception as e:
            logger.error("✗ Suites migration failed: %s", e, exc_info=True)
            suite_mapping = {}
            _save()
        
        step_log_info(logger, "\nMigrating test cases for %s...", project_code_source)
        try:
            case_mapping = migrate_cases(
                source_service,
                target_service,
                project_code_source,
                project_code_target,
                suite_mapping,
                custom_field_mapping,
                milestone_mapping,
                shared_step_mapping,
                shared_parameter_mapping,
                user_mapping,
                mappings,
                stats,
                preserve_ids=preserve_ids,
                progress=progress,
            )
            _save()
        except Exception as e:
            logger.error("✗ Test cases migration failed: %s", e, exc_info=True)
            case_mapping = {}
            _save()
        
        step_log_info(logger, "\nMigrating test plans for %s...", project_code_source)
        try:
            plan_mapping = migrate_plans(
                source_service,
                target_service,
                project_code_source,
                project_code_target,
                case_mapping,
                mappings,
                stats,
            )
            _save()
        except Exception as e:
            logger.error("✗ Test plans migration failed: %s", e, exc_info=True)
            plan_mapping = {}
            _save()
        
        step_log_info(logger, "\nMigrating test runs for %s...", project_code_source)
        try:
            run_mapping = migrate_runs(
                source_service,
                target_service,
                project_code_source,
                project_code_target,
                case_mapping,
                config_mapping,
                milestone_mapping,
                plan_mapping,
                user_mapping,
                mappings,
                stats,
                source_runs_precached=source_runs_precached,
                progress=progress,
                link_to_plan=link_runs_to_plans,
            )
            _save()
        except Exception as e:
            logger.error("✗ Test runs migration failed: %s", e, exc_info=True)
            run_mapping = {}
            _save()
        
        step_log_info(logger, "\nMigrating test results for %s...", project_code_source)
        try:
            migrate_results(
                source_service,
                target_service,
                project_code_source,
                project_code_target,
                run_mapping,
                case_mapping,
                mappings,
                stats,
                progress=progress,
            )
            _save()
        except Exception as e:
            logger.error("✗ Test results migration failed: %s", e, exc_info=True)
            _save()
        
        step_log_info(logger, "\nMigrating defects for %s...", project_code_source)
        try:
            attachment_mapping: Dict[str, Any] = {}
            if project_code_source in mappings.attachments:
                attachment_mapping = mappings.attachments[project_code_source]
                normalized_mapping = {}
                for key, value in attachment_mapping.items():
                    normalized_mapping[key.lower()] = value
                    normalized_mapping[key] = value
                attachment_mapping = normalized_mapping
        
            migrate_defects(
                source_service,
                target_service,
                project_code_source,
                project_code_target,
                milestone_mapping,
                user_mapping,
                attachment_mapping,
                mappings,
                stats,
            )
            _save()
        except Exception as e:
            logger.error("✗ Defects migration failed: %s", e, exc_info=True)
            _save()
        
        if progress is not None:
            progress.close()
            progress = None

        set_migration_progress_console_quiet(False)

        for entity_type in [
            "milestones",
            "configurations",
            "shared_steps",
            "suites",
            "cases",
            "plans",
            "runs",
            "results",
        ]:
            if entity_type in stats.entities_created:
                created = stats.entities_created.get(entity_type, 0)
                processed = stats.entities_processed.get(entity_type, 0)
                project_stats[entity_type] = f"{created}/{processed}"

        if emit_summary_logs:
            logger.info("\nCompleted migration for project %s", project_code_source)
            logger.info("Summary for %s:", project_code_source)
            for entity_type, count in project_stats.items():
                logger.info("  %s: %s", entity_type, count)
        else:
            logger.debug(
                "Finished project %s -> %s (summary deferred)",
                project_code_source,
                project_code_target,
            )

        if tr:
            tr.event(
                "project_end",
                project_source=project_code_source,
                project_target=project_code_target,
                stats=project_stats,
            )

        return project_stats

    finally:
        set_current_project(None)
        set_migration_progress_console_quiet(False)
        if progress is not None:
            progress.close()
