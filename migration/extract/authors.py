"""
Extract authors from source Qase workspace.
"""
import logging
import requests

from migration import qase_http
from typing import List, Dict, Any
from qase_service import QaseService
from migration.step_logging import step_log_info

logger = logging.getLogger(__name__)


def extract_authors(source_service: QaseService) -> Dict[str, int]:
    """
    Extract authors from source workspace using raw HTTP API.
    Builds a mapping: author_uuid -> author_id
    
    Args:
        source_service: Source Qase service
    
    Returns:
        Dictionary mapping author_uuid -> author_id
    """
    author_uuid_to_id = {}
    
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
        return author_uuid_to_id
    
    if not api_token or not base_url:
        logger.error("API token or base URL not available")
        return author_uuid_to_id
    
    # Use raw HTTP API to get authors
    api_base = base_url.rstrip('/')
    if not api_base.endswith('/v1'):
        api_base = f"{api_base}/v1"
    
    offset = 0
    limit = 100
    
    while True:
        try:
            url = f"{api_base}/author"
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
                        for author in entities_list:
                            author_uuid = author.get('uuid')
                            author_id = author.get('id')  # Use 'id' field, not 'author_id'
                            if author_uuid and author_id:
                                author_uuid_to_id[author_uuid] = int(author_id)
                        
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
                logger.error(f"Failed to fetch authors via raw API: {response.status_code} - {response.text[:200]}")
                break
        except Exception as e:
            logger.error(f"Failed to fetch authors via raw API: {e}")
            break
    
    step_log_info(
        logger, "Extracted %s author UUID mappings", len(author_uuid_to_id)
    )
    return author_uuid_to_id
