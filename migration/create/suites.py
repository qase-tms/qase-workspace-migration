"""
Create suites in target Qase workspace.
"""
import logging
from typing import Dict, Any, Optional
from qase.api_client_v1.api.suites_api import SuitesApi
from qase.api_client_v1.models import SuiteCreate
from qase_service import QaseService
from migration.utils import MigrationMappings, MigrationStats, retry_with_backoff

logger = logging.getLogger(__name__)


def migrate_suite_recursive(
    source_suite_id: int,
    all_suites: Dict[int, Dict[str, Any]],
    parent_child_map: Dict[int, list],
    project_code_target: str,
    suites_api_target: SuitesApi,
    suite_mapping: Dict[int, int],
    parent_target_id: Optional[int] = None,
    existing: Optional[Dict[int, int]] = None
):
    """Recursively migrate suite using the pre-built mapping.

    ``existing`` holds suites migrated by a previous run (from the mappings file).
    Those are reused instead of created again, so a resumed run does not duplicate them.
    """
    if source_suite_id not in all_suites:
        return

    existing = existing or {}
    if source_suite_id in existing:
        target_suite_id = existing[source_suite_id]
        suite_mapping[source_suite_id] = target_suite_id
        for child_id in parent_child_map.get(source_suite_id, []):
            migrate_suite_recursive(
                child_id, all_suites, parent_child_map,
                project_code_target, suites_api_target,
                suite_mapping, target_suite_id, existing
            )
        return

    suite_dict = all_suites[source_suite_id]
    
    suite_title = suite_dict.get('title')
    if not suite_title:
        suite_title = suite_dict.get('name') or f"Suite {source_suite_id}"
    
    suite_data = SuiteCreate(
        title=suite_title,
        description=suite_dict.get('description') or '',
        preconditions=suite_dict.get('preconditions') or '',
        parent_id=parent_target_id
    )
    
    try:
        create_response = retry_with_backoff(
            suites_api_target.create_suite,
            code=project_code_target,
            suite_create=suite_data
        )
        
        if create_response:
            target_suite_id = None
            if hasattr(create_response, 'status') and hasattr(create_response, 'result'):
                if create_response.status and create_response.result:
                    target_suite_id = getattr(create_response.result, 'id', None)
            elif hasattr(create_response, 'id'):
                target_suite_id = create_response.id
            elif hasattr(create_response, 'result'):
                result = create_response.result
                target_suite_id = getattr(result, 'id', None)
            
            if target_suite_id:
                suite_mapping[source_suite_id] = target_suite_id
                
                children_ids = parent_child_map.get(source_suite_id, [])
                for child_id in children_ids:
                    migrate_suite_recursive(
                        child_id, all_suites, parent_child_map,
                        project_code_target, suites_api_target,
                        suite_mapping, target_suite_id, existing
                    )
    except Exception as e:
        logger.error(f"Error creating suite {source_suite_id} (title: {suite_title}): {e}", exc_info=True)


def migrate_suites(
    source_service: QaseService,
    target_service: QaseService,
    project_code_source: str,
    project_code_target: str,
    mappings: MigrationMappings,
    stats: MigrationStats
) -> Dict[int, int]:
    """
    Migrate suites from source to target workspace.
    
    Args:
        source_service: Source Qase service
        target_service: Target Qase service
        project_code_source: Source project code
        project_code_target: Target project code
        mappings: Migration mappings object
        stats: Migration stats object
    
    Returns:
        Dictionary mapping source suite ID to target suite ID
    """
    from migration.extract.suites import extract_suites
    
    all_suites, parent_child_map = extract_suites(source_service, project_code_source)
    
    suites_api_target = SuitesApi(target_service.client)
    suite_mapping = {}
    existing = dict(mappings.suites.get(project_code_source, {}))
    
    root_suite_ids = parent_child_map.get(None, [])
    for root_suite_id in root_suite_ids:
        migrate_suite_recursive(
            root_suite_id, all_suites, parent_child_map,
            project_code_target, suites_api_target, suite_mapping, None, existing
        )
    
    if project_code_source not in mappings.suites:
        mappings.suites[project_code_source] = {}
    mappings.suites[project_code_source].update(suite_mapping)
    
    stats.add_entity('suites', len(all_suites), len(suite_mapping))
    stats.add_reused('suites', sum(1 for sid in suite_mapping if sid in existing))
    return suite_mapping
