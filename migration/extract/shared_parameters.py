"""
Extract shared parameters from source Qase workspace.
"""
import logging
from typing import List, Dict, Any
from qase_service import QaseService
from migration.utils import retry_with_backoff, to_dict
import requests

from migration import qase_http

logger = logging.getLogger(__name__)


def extract_shared_parameters(source_service: QaseService, project_codes: List[str] = None) -> List[Dict[str, Any]]:
    """
    Extract shared parameters from source workspace.
    
    Args:
        source_service: Source Qase service
        project_codes: Optional list of project codes to filter by
    
    Returns:
        List of shared parameter dictionaries
    """
    base_url = source_service.client.configuration.host.rstrip('/')
    headers = {
        'Token': source_service.api_token,
        'Accept': 'application/json'
    }
    
    shared_parameters = []
    offset = 0
    limit = 100
    
    while True:
        url = f"{base_url}/shared_parameter"
        params = {
            'limit': limit,
            'offset': offset
        }
        
        # Add project code filters if provided
        if project_codes:
            for idx, code in enumerate(project_codes):
                params[f'filters[project_codes][{idx}]'] = code
        
        try:
            response = qase_http.get(url, headers=headers, params=params, timeout=60)
            if response.status_code == 200:
                response_data = response.json()
                if 'result' in response_data and 'entities' in response_data['result']:
                    entities = response_data['result']['entities']
                    if not entities:
                        break
                    
                    for entity in entities:
                        shared_parameters.append(to_dict(entity))
                    
                    if len(entities) < limit:
                        break
                    
                    offset += limit
                else:
                    break
            else:
                logger.error(f"Failed to extract shared parameters: {response.status_code} - {response.text}")
                break
        except Exception as e:
            logger.error(f"Exception extracting shared parameters: {e}")
            break
    
    return shared_parameters
