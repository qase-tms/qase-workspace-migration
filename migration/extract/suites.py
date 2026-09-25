"""
Extract suites from source Qase workspace.
"""
import logging
from typing import List, Dict, Any, Tuple
from qase.api_client_v1.api.suites_api import SuitesApi
from qase_service import QaseService
from migration.step_logging import step_log_info
from migration.utils import retry_with_backoff, extract_entities_from_response, to_dict

logger = logging.getLogger(__name__)


def extract_suites(source_service: QaseService, project_code: str) -> Tuple[Dict[int, Dict[str, Any]], Dict[int, List[int]]]:
    """
    Extract suites from source project and build parent-child mapping.
    
    Args:
        source_service: Source Qase service
        project_code: Project code
    
    Returns:
        Tuple of (all_suites_dict, parent_child_map)
        - all_suites_dict: suite_id -> suite_dict
        - parent_child_map: parent_id -> list of child_ids
    """
    step_log_info(logger, "Extracting suites from project %s...", project_code)
    suites_api_source = SuitesApi(source_service.client)
    
    all_suites = {}  # suite_id -> suite_dict
    parent_child_map = {}  # parent_id -> list of child_ids
    
    offset = 0
    limit = 100
    
    while True:
        api_response = retry_with_backoff(
            suites_api_source.get_suites,
            code=project_code,
            limit=limit,
            offset=offset
        )
        
        entities = extract_entities_from_response(api_response)
        if not entities:
            break
        
        for suite in entities:
            suite_dict = to_dict(suite)
            suite_id = suite_dict.get('id')
            if suite_id:
                all_suites[suite_id] = suite_dict
                parent_id = suite_dict.get('parent_id')
                if parent_id not in parent_child_map:
                    parent_child_map[parent_id] = []
                parent_child_map[parent_id].append(suite_id)
        
        if len(entities) < limit:
            break
        offset += limit
    
    step_log_info(
        logger, "Extracted %s suites from project %s", len(all_suites), project_code
    )
    return all_suites, parent_child_map
