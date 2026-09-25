"""
Extract configurations from source Qase workspace.
"""
import logging
from typing import List, Dict, Any
from qase.api_client_v1.api.configurations_api import ConfigurationsApi
from qase_service import QaseService
from migration.step_logging import step_log_info
from migration.utils import retry_with_backoff, extract_entities_from_response, to_dict

logger = logging.getLogger(__name__)


def extract_configurations(source_service: QaseService, project_code: str) -> List[Dict[str, Any]]:
    """
    Extract configuration groups and their configurations from source project.
    
    Args:
        source_service: Source Qase service
        project_code: Project code
    
    Returns:
        List of configuration group dictionaries (with nested configs)
    """
    step_log_info(logger, "Extracting configurations from project %s...", project_code)
    configs_api_source = ConfigurationsApi(source_service.client)
    
    groups_list = []
    try:
        api_response = retry_with_backoff(
            configs_api_source.get_configurations,
            code=project_code
        )
        
        entities = extract_entities_from_response(api_response)
        if entities:
            for group in entities:
                groups_list.append(to_dict(group))
    except Exception as e:
        logger.warning(f"Error fetching configuration groups: {e}")
    
    step_log_info(
        logger,
        "Extracted %s configuration groups from project %s",
        len(groups_list),
        project_code,
    )
    return groups_list
