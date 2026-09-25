"""
Extract environments from source Qase workspace.
"""
import logging
from typing import List, Dict, Any
from qase.api_client_v1.api.environments_api import EnvironmentsApi
from qase_service import QaseService
from migration.utils import retry_with_backoff, extract_entities_from_response, to_dict

logger = logging.getLogger(__name__)


def extract_environments(source_service: QaseService, project_code: str) -> List[Dict[str, Any]]:
    """
    Extract environments from source project.
    
    Args:
        source_service: Source Qase service
        project_code: Project code
    
    Returns:
        List of environment dictionaries
    """
    environments_api_source = EnvironmentsApi(source_service.client)
    
    environments = []
    offset = 0
    limit = 100
    
    while True:
        try:
            api_response = retry_with_backoff(
                environments_api_source.get_environments,
                code=project_code,
                limit=limit,
                offset=offset
            )
            
            entities = extract_entities_from_response(api_response)
            if not entities:
                break
            
            for env in entities:
                environments.append(to_dict(env))
            
            if len(entities) < limit:
                break
            
            offset += limit
        except Exception as e:
            logger.error(f"Error fetching environments: {e}")
            break
    
    return environments
