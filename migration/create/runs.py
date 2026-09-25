"""
Create runs in target Qase workspace.
"""
import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from qase.api_client_v1.api.runs_api import RunsApi
from qase.api_client_v1.models import RunCreate
from qase_service import QaseService
from migration.utils import MigrationMappings, MigrationStats, retry_with_backoff, format_datetime, QaseRawApiClient
from migration.extract.runs import extract_runs, extract_run_cases, fetch_run_detail_json
from migration.trace_log import summarize_source_run

if TYPE_CHECKING:
    from migration.progress import ProjectMigrationProgress

logger = logging.getLogger(__name__)


def _source_run_should_complete_after_results(run_dict: Dict[str, Any]) -> bool:
    """
    Queue complete_run after results migration when the source run was already finished.
    List and detail payloads differ; check booleans, timestamps, and string/enum shapes.
    """
    if not run_dict:
        return False
    if run_dict.get("is_completed") or run_dict.get("is_complete"):
        return True
    for k in (
        "end_time",
        "completed_at",
        "time_end",
        "finished_at",
        "closed_at",
        "complete_time",
    ):
        if run_dict.get(k):
            return True
    for key in ("state", "status", "run_status", "runStatus", "execution_state", "executionState"):
        raw = run_dict.get(key)
        if isinstance(raw, str):
            s = raw.strip().lower()
            if s in (
                "completed",
                "complete",
                "finished",
                "done",
                "closed",
                "aborted",
                "ended",
            ):
                return True
        elif isinstance(raw, dict):
            for nk in ("title", "name", "status", "slug", "value"):
                v = raw.get(nk)
                if isinstance(v, str) and v.strip().lower() in (
                    "completed",
                    "complete",
                    "finished",
                    "done",
                    "closed",
                    "aborted",
                    "ended",
                ):
                    return True
    return False


def _plan_target_case_ids(
    source_service: QaseService,
    project_code: str,
    source_plan_id: Any,
    case_mapping: Dict[int, int],
    cache: Dict[Any, set],
) -> set:
    """Target ids of the cases in a source plan (cached per plan)."""
    if source_plan_id not in cache:
        ids: set = set()
        try:
            from qase.api_client_v1.api.plans_api import PlansApi

            plan = PlansApi(source_service.client).get_plan(code=project_code, id=int(source_plan_id))
            for item in (getattr(plan.result, "cases", None) or []):
                target = case_mapping.get(getattr(item, "case_id", None))
                if target:
                    ids.add(target)
        except Exception as e:  # noqa: BLE001 - only used for a warning
            logger.debug("Could not read plan %s in %s: %s", source_plan_id, project_code, e)
        cache[source_plan_id] = ids
    return cache[source_plan_id]


def migrate_runs(
    source_service: QaseService,
    target_service: QaseService,
    project_code_source: str,
    project_code_target: str,
    case_mapping: Dict[int, int],
    config_mapping: Dict[int, int],
    milestone_mapping: Dict[int, int],
    plan_mapping: Dict[int, int],
    user_mapping: Dict[int, int],
    mappings: MigrationMappings,
    stats: MigrationStats,
    source_runs_precached: Optional[List[Dict[str, Any]]] = None,
    progress: Optional["ProjectMigrationProgress"] = None,
    link_to_plan: bool = False,
) -> Dict[int, int]:
    """
    Migrate test runs from source to target workspace.

    ``link_to_plan`` (config ``runs.link_to_plan``) chooses which side of an API
    limit to keep for runs that belong to a test plan. Qase builds a run created
    with a plan from the plan's current cases and ignores any case list, and a
    plan cannot be attached afterwards. So the run keeps either its plan link
    (True) or its exact source case list (False, the default).
    
    Returns:
        Dictionary mapping source run ID to target run ID
    """
    runs_api_target = RunsApi(target_service.client)
    
    # Initialize raw API client for creating runs with milestone_id support
    try:
        base_url = target_service.client.configuration.host
        api_key_dict = target_service.client.configuration.api_key
        if isinstance(api_key_dict, dict):
            api_token = api_key_dict.get('TokenAuth') or api_key_dict.get('Token') or api_key_dict.get('token')
        else:
            api_token = None
        api_base = base_url.rstrip('/')
        if not api_base.endswith('/v1'):
            api_base = f"{api_base}/v1"
        raw_api_client = QaseRawApiClient(api_base, api_token) if api_token else None
    except Exception:
        raw_api_client = None
    
    # Normalize user_mapping: ensure keys are integers (JSON may store them as strings)
    if user_mapping:
        first_key = next(iter(user_mapping.keys()), None)
        if first_key is not None and isinstance(first_key, str):
            user_mapping = {int(k): v for k, v in user_mapping.items()}
    else:
        # Fallback to mappings.users if user_mapping is empty
        user_mapping = getattr(mappings, 'users', {})
        if user_mapping:
            first_key = next(iter(user_mapping.keys()), None)
            if first_key is not None and isinstance(first_key, str):
                user_mapping = {int(k): v for k, v in user_mapping.items()}
    
    # Normalize milestone_mapping: ensure keys are integers (JSON may store them as strings)
    if milestone_mapping:
        first_key = next(iter(milestone_mapping.keys()), None)
        if first_key is not None and isinstance(first_key, str):
            milestone_mapping = {int(k): v for k, v in milestone_mapping.items()}
    else:
        # Fallback to mappings.milestones if milestone_mapping is empty
        if project_code_source in getattr(mappings, 'milestones', {}):
            milestone_mapping = mappings.milestones[project_code_source]
            if milestone_mapping:
                first_key = next(iter(milestone_mapping.keys()), None)
                if first_key is not None and isinstance(first_key, str):
                    milestone_mapping = {int(k): v for k, v in milestone_mapping.items()}
        else:
            milestone_mapping = {}
    
    # Build a mapping from milestone title to source milestone ID
    # This is needed because runs return milestone as {title, description} not milestone_id
    milestone_title_to_id = {}
    try:
        from migration.extract.milestones import extract_milestones
        source_milestones = extract_milestones(source_service, project_code_source)
        for milestone in source_milestones:
            milestone_id = milestone.get('id')
            milestone_title = milestone.get('title')
            if milestone_id and milestone_title:
                milestone_title_to_id[milestone_title] = milestone_id
    except Exception:
        pass
    
    run_mapping = {}
    if source_runs_precached is not None:
        source_runs = source_runs_precached
    else:
        source_runs = extract_runs(source_service, project_code_source)
    trace = getattr(mappings, "trace", None)
    if trace:
        trace.event(
            "runs_phase_start",
            project_source=project_code_source,
            project_target=project_code_target,
            n_source_runs=len(source_runs),
        )

    # Runs migrated by a previous run are reused, not created a second time.
    existing = dict(mappings.runs.get(project_code_source, {}))
    reused = 0
    linked_to_plan = 0
    plan_cases_cache: Dict[Any, set] = {}

    for run_dict in source_runs:
        source_run_id = run_dict.get('id')
        if source_run_id in existing:
            run_mapping[source_run_id] = existing[source_run_id]
            reused += 1
            if progress:
                progress.add_runs(1)
            continue
        
        target_cases = extract_run_cases(
            source_service, project_code_source, source_run_id, case_mapping
        )
        
        if not target_cases:
            source_cases = run_dict.get('cases', [])
            if source_cases:
                for case_item in source_cases:
                    case_id = None
                    if isinstance(case_item, int):
                        case_id = case_item
                    elif isinstance(case_item, dict):
                        case_id = case_item.get('id') or case_item.get('case_id')
                    elif hasattr(case_item, 'id'):
                        case_id = case_item.id
                    
                    if case_id:
                        target_case_id = case_mapping.get(int(case_id))
                        if target_case_id and target_case_id not in target_cases:
                            target_cases.append(target_case_id)
        
        target_configs = []
        source_configs = run_dict.get('configurations', [])
        if source_configs:
            for config_item in source_configs:
                config_id = None
                if isinstance(config_item, int):
                    config_id = config_item
                elif isinstance(config_item, dict):
                    config_id = config_item.get('id') or config_item.get('configuration_id')
                elif hasattr(config_item, 'id'):
                    config_id = config_item.id
                
                if config_id:
                    target_config_id = config_mapping.get(int(config_id))
                    if target_config_id:
                        target_configs.append(target_config_id)
        
        # Map author_id: Qase API returns 'user_id' field in run data
        source_user_id = run_dict.get('user_id') or run_dict.get('created_by') or run_dict.get('author_id') or run_dict.get('member_id')
        if source_user_id:
            try:
                source_user_id_int = int(source_user_id)
                # Skip if user_id is 0 (system/automated runs)
                if source_user_id_int == 0:
                    target_author_id = mappings.default_user_id
                else:
                    target_author_id = mappings.get_user_id(source_user_id_int)
            except (ValueError, TypeError):
                target_author_id = mappings.default_user_id
        else:
            target_author_id = mappings.default_user_id
        
        run_data_dict = {
            'title': run_dict.get('title', ''),
            'description': run_dict.get('description', ''),
            'author_id': target_author_id,
        }
        
        # Format and include start_time only if valid
        start_time_formatted = format_datetime(run_dict.get('start_time'))
        if start_time_formatted:
            run_data_dict['start_time'] = start_time_formatted
        
        # Format and include end_time only if valid
        end_time_formatted = format_datetime(run_dict.get('end_time'))
        if end_time_formatted:
            run_data_dict['end_time'] = end_time_formatted
        
        if target_cases:
            run_data_dict['cases'] = target_cases
        if target_configs:
            run_data_dict['configurations'] = target_configs
        
        # Handle milestone: runs return milestone as {title, description} object, not milestone_id
        source_milestone_id = None
        milestone_obj = run_dict.get('milestone')
        if milestone_obj:
            # Extract milestone title from milestone object
            if isinstance(milestone_obj, dict):
                milestone_title = milestone_obj.get('title')
                if milestone_title and milestone_title_to_id:
                    source_milestone_id = milestone_title_to_id.get(milestone_title)
        elif run_dict.get('milestone_id'):
            # Fallback: if milestone_id is directly available (shouldn't happen based on API response)
            source_milestone_id = run_dict.get('milestone_id')
        
        if source_milestone_id is not None:
            try:
                # Ensure milestone_id is an integer for lookup
                source_milestone_id_int = int(source_milestone_id)
                mapped_milestone = milestone_mapping.get(source_milestone_id_int)
                if mapped_milestone is not None:
                    run_data_dict['milestone_id'] = mapped_milestone
            except (ValueError, TypeError):
                pass
        # Plan link or exact case list, not both: POST /run with plan_id builds
        # the run from the plan's current cases plus any `cases` sent, and
        # PATCH ignores plan_id, so the link cannot be added after creation.
        # By default the plan is left off and the run keeps exactly its
        # source cases.
        source_plan_id = run_dict.get('plan_id')
        if not source_plan_id and isinstance(run_dict.get('plan'), dict):
            source_plan_id = run_dict['plan'].get('id')
        mapped_plan_id = plan_mapping.get(source_plan_id) if source_plan_id else None
        if link_to_plan and source_plan_id:
            if mapped_plan_id:
                # The run's own cases are still sent: the server adds the plan's
                # cases to them, so every source result keeps a case to land on.
                run_data_dict['plan_id'] = mapped_plan_id
                linked_to_plan += 1
                plan_cases = _plan_target_case_ids(
                    source_service, project_code_source, source_plan_id,
                    case_mapping, plan_cases_cache,
                )
                extra = plan_cases - set(target_cases)
                if extra:
                    logger.warning(
                        "Run %s is linked to plan %s (runs.link_to_plan), so it also gets %s "
                        "plan case(s) the source run did not include. They show as untested, "
                        "so the run's total is %s instead of %s.",
                        source_run_id, source_plan_id, len(extra),
                        len(extra) + len(target_cases), len(target_cases),
                    )
            else:
                logger.warning(
                    "Run %s belongs to plan %s, which was not migrated, so it was created "
                    "with its own case list and no plan link",
                    source_run_id, source_plan_id,
                )

        if trace:
            trace.event(
                "run_extracted",
                project_source=project_code_source,
                project_target=project_code_target,
                source_run_id=source_run_id,
                extracted=summarize_source_run(run_dict),
                target_cases_count=len(target_cases),
                target_configs_count=len(target_configs),
                source_plan_id=source_plan_id,
                mapped_plan_id=mapped_plan_id,
                linked_to_plan=bool(link_to_plan and mapped_plan_id),
            )

        # Use raw API client if milestone_id is present (SDK may not support it properly)
        # Otherwise fall back to SDK
        if raw_api_client and 'milestone_id' in run_data_dict:
            target_run_id = raw_api_client.create_run(project_code_target, run_data_dict)
            create_response = target_run_id is not None
        else:
            run_data = RunCreate(**run_data_dict)
            create_response = retry_with_backoff(
                runs_api_target.create_run,
                max_retries=7,
                base_delay=1.5,
                code=project_code_target,
                run_create=run_data,
            )
            
            if create_response:
                target_run_id = None
                if hasattr(create_response, 'status') and hasattr(create_response, 'result'):
                    if create_response.status and create_response.result:
                        target_run_id = getattr(create_response.result, 'id', None)
                elif hasattr(create_response, 'id'):
                    target_run_id = create_response.id
                elif hasattr(create_response, 'result'):
                    result = create_response.result
                    target_run_id = getattr(result, 'id', None)
            else:
                target_run_id = None
        
        if create_response and target_run_id:
            run_mapping[source_run_id] = target_run_id
            flag_src: Dict[str, Any] = run_dict
            should_complete = _source_run_should_complete_after_results(flag_src)
            if not should_complete and source_run_id is not None:
                try:
                    detail = fetch_run_detail_json(
                        source_service, project_code_source, int(source_run_id)
                    )
                    if detail and _source_run_should_complete_after_results(detail):
                        should_complete = True
                        flag_src = detail
                except Exception:
                    pass
            if should_complete:
                if not hasattr(migrate_runs, '_runs_to_complete'):
                    migrate_runs._runs_to_complete = {}
                migrate_runs._runs_to_complete[target_run_id] = {
                    'project_code': project_code_target,
                    'is_completed': True,
                    'source_is_completed': bool(
                        flag_src.get("is_completed") or flag_src.get("is_complete")
                    ),
                    'has_end_time': bool(
                        flag_src.get("end_time")
                        or flag_src.get("time_end")
                        or flag_src.get("completed_at")
                    ),
                }
            if trace:
                trace.event(
                    "run_created_target",
                    project_source=project_code_source,
                    project_target=project_code_target,
                    source_run_id=source_run_id,
                    target_run_id=target_run_id,
                    payload_sent=run_data_dict,
                    queued_complete_after_results=should_complete,
                    complete_reason={
                        "is_completed": bool(
                            run_dict.get("is_completed") or run_dict.get("is_complete")
                        ),
                        "has_end_time": bool(run_dict.get("end_time")),
                        "state": run_dict.get("state"),
                        "status": run_dict.get("status"),
                    },
                )
        elif trace:
            trace.event(
                "run_create_failed",
                project_source=project_code_source,
                project_target=project_code_target,
                source_run_id=source_run_id,
                payload_attempted=run_data_dict,
                create_response_bool=bool(create_response),
                target_run_id_resolved=target_run_id,
            )
        if progress:
            progress.add_runs(1)
    
    if project_code_source not in mappings.runs:
        mappings.runs[project_code_source] = {}
    mappings.runs[project_code_source].update(run_mapping)
    
    if hasattr(migrate_runs, '_runs_to_complete'):
        if not hasattr(mappings, '_runs_to_complete'):
            mappings._runs_to_complete = {}
        mappings._runs_to_complete[project_code_source] = migrate_runs._runs_to_complete
        delattr(migrate_runs, '_runs_to_complete')
    
    stats.add_entity('runs', len(source_runs), len(run_mapping))
    stats.add_reused('runs', reused)
    if linked_to_plan:
        logger.info(
            "%s: %s run(s) linked to their test plan (runs.link_to_plan)",
            project_code_source, linked_to_plan,
        )
    if reused:
        logger.info(
            "%s: %s run(s) already migrated by a previous run, reusing them",
            project_code_source, reused,
        )
    if trace:
        n_qc = 0
        if hasattr(mappings, "_runs_to_complete") and project_code_source in mappings._runs_to_complete:
            n_qc = len(mappings._runs_to_complete[project_code_source])
        trace.event(
            "runs_phase_end",
            project_source=project_code_source,
            n_source_runs=len(source_runs),
            n_target_runs_mapped=len(run_mapping),
            n_queued_complete=n_qc,
        )
    return run_mapping
