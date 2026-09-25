"""
Extract users from source Qase workspace.
"""
import logging
from typing import List, Dict, Any
from qase.api_client_v1.api.authors_api import AuthorsApi
from qase_service import QaseService
from migration.utils import retry_with_backoff, extract_entities_from_response, to_dict

logger = logging.getLogger(__name__)


def extract_users(source_service: QaseService) -> List[Dict[str, Any]]:
    """
    Extract users from source workspace.
    
    Returns:
        List of user dictionaries with full user information
    """
    logger.info("Extracting users from source workspace...")
    authors_api_source = AuthorsApi(source_service.client)
    
    source_users = []
    offset = 0
    limit = 100
    
    while True:
        api_response = retry_with_backoff(
            authors_api_source.get_authors,
            limit=limit,
            offset=offset,
            type="user"
        )
        
        entities = extract_entities_from_response(api_response)
        if not entities:
            break
        
        for user in entities:
            user_dict = to_dict(user)
            source_users.append(user_dict)
        
        if len(entities) < limit:
            break
        offset += limit
    
    logger.info(f"Extracted {len(source_users)} users from source workspace")
    return source_users
