"""
Extract shared steps from source Qase workspace.
"""
import logging
from typing import List, Dict, Any
from qase.api_client_v1.api.shared_steps_api import SharedStepsApi
from qase_service import QaseService
from migration.step_logging import step_log_info
from migration.utils import retry_with_backoff, extract_entities_from_response, to_dict

logger = logging.getLogger(__name__)


def extract_shared_steps(source_service: QaseService, project_code: str) -> List[Dict[str, Any]]:
    """
    Extract shared steps from source project.
    
    Args:
        source_service: Source Qase service
        project_code: Project code
    
    Returns:
        List of shared step dictionaries
    """
    step_log_info(logger, "Extracting shared steps from project %s...", project_code)
    shared_steps_api_source = SharedStepsApi(source_service.client)
    
    shared_steps = []
    offset = 0
    limit = 100
    
    while True:
        api_response = retry_with_backoff(
            shared_steps_api_source.get_shared_steps,
            code=project_code,
            limit=limit,
            offset=offset
        )
        
        entities = extract_entities_from_response(api_response)
        if not entities:
            break
        
        for step in entities:
            step_dict = to_dict(step)
            source_hash = step_dict.get('hash') or getattr(step, 'hash', None)
            if source_hash:
                shared_steps.append(step_dict)
        
        if len(entities) < limit:
            break
        offset += limit
    
    step_log_info(
        logger,
        "Extracted %s shared steps from project %s",
        len(shared_steps),
        project_code,
    )
    return shared_steps
