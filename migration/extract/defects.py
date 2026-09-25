"""
Extract defects from source Qase workspace.
"""
import logging
import requests

from migration import qase_http
from typing import List, Dict, Any
from qase_service import QaseService

logger = logging.getLogger(__name__)


def extract_defects(source_service: QaseService, project_code: str) -> List[Dict[str, Any]]:
    """
    Extract defects from source project.
    Uses raw HTTP API to get full response.
    
    Args:
        source_service: Source Qase service
        project_code: Project code
    
    Returns:
        List of defect dictionaries with full details
    """
    defects = []
    
    # Get API configuration from service
    try:
        base_url = source_service.client.configuration.host
        api_key_dict = source_service.client.configuration.api_key
        if isinstance(api_key_dict, dict):
            api_token = api_key_dict.get('TokenAuth') or api_key_dict.get('Token') or api_key_dict.get('token')
        else:
            api_token = None
    except Exception:
        logger.error("Cannot get API token/URL from service")
        return defects
    
    if not api_token or not base_url:
        logger.error("API token or base URL not available")
        return defects
    
    # Use raw HTTP API to get full response
    api_base = base_url.rstrip('/')
    if not api_base.endswith('/v1'):
        api_base = f"{api_base}/v1"
    
    offset = 0
    limit = 100
    
    while True:
        try:
            url = f"{api_base}/defect/{project_code}"
            headers = {
                'Token': api_token,
                'accept': 'application/json'
            }
            params = {
                'limit': limit,
                'offset': offset
            }
            
            response = qase_http.get(url, headers=headers, params=params, timeout=30)
            if response.status_code == 200:
                response_data = response.json()
                if response_data.get('status') and response_data.get('result'):
                    result = response_data['result']
                    entities_list = result.get('entities', [])
                    if not entities_list and isinstance(result, list):
                        entities_list = result
                    
                    if entities_list:
                        defects.extend(entities_list)
                        
                        # Check if there are more pages
                        total = result.get('total', len(entities_list))
                        if len(entities_list) < limit or offset + len(entities_list) >= total:
                            break
                        offset += limit
                    else:
                        break
                else:
                    logger.warning(f"Unexpected response format: {response_data}")
                    break
            else:
                logger.error(f"Failed to fetch defects via raw API: {response.status_code} - {response.text[:200]}")
                break
        except Exception as e:
            logger.error(f"Failed to fetch defects via raw API: {e}")
            break
    
    return defects
