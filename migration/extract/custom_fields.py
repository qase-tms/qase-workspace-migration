"""
Extract custom fields from source Qase workspace.
"""
import logging
from typing import List, Dict, Any
from qase.api_client_v1.api.custom_fields_api import CustomFieldsApi
from qase_service import QaseService
from migration.utils import retry_with_backoff, extract_entities_from_response, to_dict

logger = logging.getLogger(__name__)


def extract_custom_fields(source_service: QaseService) -> List[Dict[str, Any]]:
    """
    Extract custom fields from source workspace.
    
    Returns:
        List of custom field dictionaries
    """
    logger.info("Extracting custom fields from source workspace...")
    custom_fields_api_source = CustomFieldsApi(source_service.client)
    
    fields = []
    offset = 0
    limit = 100
    
    while True:
        api_response = retry_with_backoff(
            custom_fields_api_source.get_custom_fields,
            limit=limit,
            offset=offset
        )
        
        entities = extract_entities_from_response(api_response)
        if not entities:
            break
        
        for field in entities:
            fields.append(to_dict(field))
        
        if len(entities) < limit:
            break
        offset += limit
    
    logger.info(f"Extracted {len(fields)} custom fields from source workspace")
    return fields
