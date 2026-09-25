"""
Dry run: read the source workspace and report what a real run would create.

Nothing here writes. The only calls made against the target are reads, used to
say which projects and custom fields already exist there. With ``--resume``, the
mappings file is consulted so entities a previous run migrated are reported as
already migrated rather than as new.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from qase.api_client_v1.api.projects_api import ProjectsApi
from qase.api_client_v1.exceptions import ApiException

from migration.create.custom_fields import get_existing_custom_fields
from migration.create.projects import project_has_data
from migration.extract.configurations import extract_configurations
from migration.extract.custom_fields import extract_custom_fields
from migration.extract.defects import extract_defects
from migration.extract.environments import extract_environments
from migration.extract.milestones import extract_milestones
from migration.extract.plans import extract_plans
from migration.extract.shared_steps import extract_shared_steps
from migration.extract.suites import extract_suites
from migration.progress import (
    fetch_result_total_for_run,
    infer_results_count_from_run_dict,
    prefetch_project_migration_profile,
)
from migration.step_logging import set_current_project
from migration.utils import MigrationMappings, MigrationStats, to_dict

logger = logging.getLogger(__name__)


def _target_project(target_service: Any, code: str) -> Dict[str, Any] | None:
    try:
        response = ProjectsApi(target_service.client).get_project(code=code)
    except ApiException as e:
        if e.status == 404:
            return None
        raise
    if response and getattr(response, "status", False) and getattr(response, "result", None):
        return to_dict(response.result)
    return None


def _project_results_total(source_service: Any, code: str) -> int:
    """Exact number of results in a project, from the API's own total."""
    from qase.api_client_v1.api.results_api import ResultsApi

    response = ResultsApi(source_service.client).get_results(code=code, limit=1)
    return int(getattr(response.result, "total", 0) or 0)


def _count(stats: MigrationStats, entity: str, source_ids: List[Any], already: Dict[Any, Any]) -> None:
    """Record ``entity`` as ``would create / source``, crediting what is already mapped."""
    reused = sum(1 for sid in source_ids if sid in already)
    stats.add_entity(entity, len(source_ids), len(source_ids) - reused)
    stats.add_reused(entity, reused)


def _report_plan_links(source_service: Any, code: str, runs: List[Dict[str, Any]], link: bool) -> None:
    """Say what runs.link_to_plan would mean for this project's plan-linked runs."""
    linked = [r for r in runs if r.get("plan_id")]
    if not linked:
        return
    if not link:
        logger.info(
            "%s: %s run(s) belong to a test plan. runs.link_to_plan is false, so they keep "
            "their exact cases and are not linked to the plan in the target.", code, len(linked),
        )
        return
    from qase.api_client_v1.api.plans_api import PlansApi
    from qase.api_client_v1.api.runs_api import RunsApi

    plans: Dict[Any, set] = {}
    grown = 0
    for run in linked:
        pid = run["plan_id"]
        if pid not in plans:
            plan = PlansApi(source_service.client).get_plan(code=code, id=int(pid))
            plans[pid] = {c.case_id for c in (plan.result.cases or [])}
        detail = RunsApi(source_service.client).get_run(code=code, id=int(run["id"]), include="cases")
        run_cases = set(getattr(detail.result, "cases", None) or [])
        extra = plans[pid] - run_cases
        if extra:
            grown += 1
            logger.warning(
                "%s: run %s would be linked to plan %s and also get %s plan case(s) it did not "
                "include, shown as untested (total %s instead of %s)",
                code, run["id"], pid, len(extra), len(extra | run_cases), len(run_cases),
            )
    logger.info(
        "%s: %s run(s) would be linked to their test plan; %s of them would gain cases from the plan",
        code, len(linked), grown,
    )


def run_dry_run(
    source_service: Any,
    target_service: Any,
    projects: List[str],
    config: Dict[str, Any],
    mappings: MigrationMappings,
    allow_existing_target: bool = False,
    resume: bool = False,
    skip_attachments: bool = False,
    link_runs_to_plans: bool = False,
) -> MigrationStats:
    stats = MigrationStats()
    logger.info("DRY RUN: reading the source, nothing will be written to the target")

    source_fields = extract_custom_fields(source_service)
    existing_fields = get_existing_custom_fields(target_service)
    new_fields = [
        f for f in source_fields
        if str(f.get("title") or "").strip().lower() not in existing_fields
    ]
    stats.add_entity("custom_fields", len(source_fields), len(new_fields))
    stats.add_reused("custom_fields", len(source_fields) - len(new_fields))

    users_cfg = config.get("users") or {}
    if users_cfg.get("migrate"):
        logger.info(
            "Users: users.migrate is true, so users are matched by email%s.",
            " and missing users created through SCIM (this consumes seats)"
            if users_cfg.get("create") else "",
        )
    if skip_attachments:
        logger.warning("Attachments would be SKIPPED (--skip-attachments): the target would not be complete.")

    stats.add_entity("projects", len(projects), 0)
    for code in projects:
        set_current_project(code)
        try:
            target = _target_project(target_service, code)
            if target is None:
                logger.info("%s: project would be created in the target", code)
                stats.add_entity("projects", 0, 1)
            elif project_has_data(target) and not (resume and code in mappings.projects) and not allow_existing_target:
                logger.error(
                    "%s: target project already contains data and would be SKIPPED. Use --resume "
                    "if a previous run of this migration created it, or set "
                    "migration.allow_existing_target: true.", code,
                )
                continue
            else:
                logger.info("%s: target project exists and would be reused", code)
                stats.add_reused("projects", 1)
                stats.add_entity("projects", 0, 1)

            all_suites, _ = extract_suites(source_service, code)
            _count(stats, "suites", list(all_suites), mappings.suites.get(code, {}))
            milestones = extract_milestones(source_service, code)
            _count(stats, "milestones", [m.get("id") for m in milestones], mappings.milestones.get(code, {}))
            groups = extract_configurations(source_service, code)
            _count(stats, "configuration_groups", [g.get("id") for g in groups],
                   mappings.configuration_groups.get(code, {}))
            environments = extract_environments(source_service, code)
            _count(stats, "environments", [e.get("id") for e in environments], mappings.environments.get(code, {}))
            shared_steps = extract_shared_steps(source_service, code)
            _count(stats, "shared_steps", [s.get("hash") for s in shared_steps], mappings.shared_steps.get(code, {}))
            plans = extract_plans(source_service, code)
            _count(stats, "plans", [p.get("id") for p in plans], mappings.plans.get(code, {}))
            defects = extract_defects(source_service, code)
            _count(stats, "defects", [d.get("id") for d in defects], mappings.defects.get(code, {}))

            profile = prefetch_project_migration_profile(source_service, code)
            mapped_cases = mappings.cases.get(code, {})
            stats.add_entity("cases", profile.cases_total, max(0, profile.cases_total - len(mapped_cases)))
            stats.add_reused("cases", min(len(mapped_cases), profile.cases_total))
            _count(stats, "runs", [r.get("id") for r in profile.source_runs], mappings.runs.get(code, {}))
            _report_plan_links(source_service, code, profile.source_runs, link_runs_to_plans)
            done_runs = {int(r) for r in mappings.results_done.get(code, [])}
            already_sent = 0
            for run in profile.source_runs:
                if int(run.get("id") or 0) in done_runs:
                    n = infer_results_count_from_run_dict(run)
                    already_sent += n if n is not None else fetch_result_total_for_run(
                        source_service, code, int(run["id"])
                    )
            results_total = _project_results_total(source_service, code)
            stats.add_entity("results", results_total, max(0, results_total - already_sent))
            stats.add_reused("results", min(already_sent, results_total))
        except Exception as e:  # noqa: BLE001 - one project failing to read should not hide the others
            logger.error("%s: could not read the source project: %s", code, e)
        finally:
            set_current_project(None)

    return stats
