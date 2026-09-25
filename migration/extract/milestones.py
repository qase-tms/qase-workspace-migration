"""
Extract milestones from source Qase workspace.
"""
import logging
from typing import List, Dict, Any
from qase.api_client_v1.api.milestones_api import MilestonesApi
from qase_service import QaseService
from migration.step_logging import step_log_info
from migration.utils import retry_with_backoff, extract_entities_from_response, to_dict

logger = logging.getLogger(__name__)


def extract_milestones(source_service: QaseService, project_code: str) -> List[Dict[str, Any]]:
    """
    Extract milestones from source project.
    
    Args:
        source_service: Source Qase service
        project_code: Project code
    
    Returns:
        List of milestone dictionaries
    """
    step_log_info(logger, "Extracting milestones from project %s...", project_code)
    milestones_api_source = MilestonesApi(source_service.client)
    
    milestones = []
    offset = 0
    limit = 100
    
    while True:
        api_response = retry_with_backoff(
            milestones_api_source.get_milestones,
            code=project_code,
            limit=limit,
            offset=offset
        )
        
        entities = extract_entities_from_response(api_response)
        if not entities:
            break
        
        for milestone in entities:
            milestones.append(to_dict(milestone))
        
        if len(entities) < limit:
            break
        offset += limit
    
    step_log_info(
        logger,
        "Extracted %s milestones from project %s",
        len(milestones),
        project_code,
    )
    return milestones
