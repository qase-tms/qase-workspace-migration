"""
Create shared parameters in target Qase workspace.
"""
import logging
from typing import Dict, Any, List
from qase_service import QaseService
from migration.utils import MigrationMappings, MigrationStats, to_dict, convert_uuids_to_strings
import requests

from migration import qase_http

logger = logging.getLogger(__name__)


def get_existing_shared_parameters(target_service: QaseService) -> Dict[str, str]:
    """
    Get all existing shared parameters from target workspace, indexed by normalized title.
    
    Returns:
        Dictionary mapping normalized_title -> target_id
    """
    base_url = target_service.client.configuration.host.rstrip('/')
    headers = {
        'Token': target_service.api_token,
        'Accept': 'application/json'
    }
    
    existing_params_by_title = {}
    offset = 0
    limit = 100
    
    while True:
        url = f"{base_url}/shared_parameter"
        params = {
            'limit': limit,
            'offset': offset
        }
        
        try:
            response = qase_http.get(url, headers=headers, params=params, timeout=60)
            if response.status_code == 200:
                response_data = response.json()
                if 'result' in response_data and 'entities' in response_data['result']:
                    entities = response_data['result']['entities']
                    if not entities:
                        break
                    
                    for entity in entities:
                        entity_dict = to_dict(entity)
                        entity_title = entity_dict.get('title')
                        entity_id = entity_dict.get('id')
                        
                        if entity_title and entity_id:
                            normalized_title = entity_title.strip().lower()
                            existing_params_by_title[normalized_title] = entity_id
                    
                    if len(entities) < limit:
                        break
                    
                    offset += limit
                else:
                    break
            else:
                logger.error(f"Failed to fetch existing shared parameters: {response.status_code} - {response.text}")
                break
        except Exception as e:
            logger.error(f"Exception fetching existing shared parameters: {e}")
            break
    
    return existing_params_by_title


def migrate_shared_parameters(
    source_service: QaseService,
    target_service: QaseService,
    project_codes: List[str],
    mappings: MigrationMappings,
    stats: MigrationStats
) -> Dict[str, str]:
    """
    Migrate shared parameters from source to target workspace.
    
    Args:
        source_service: Source Qase service
        target_service: Target Qase service
        project_codes: List of project codes to filter shared parameters
        mappings: Migration mappings object
        stats: Migration stats object
    
    Returns:
        Dictionary mapping source shared parameter ID to target ID
    """
    from migration.extract.shared_parameters import extract_shared_parameters
    
    shared_parameters = extract_shared_parameters(source_service, project_codes)
    
    # Get existing shared parameters from target for deduplication
    existing_params_by_title = get_existing_shared_parameters(target_service)
    
    base_url = target_service.client.configuration.host.rstrip('/')
    headers = {
        'Token': target_service.api_token,
        'Accept': 'application/json',
        'Content-Type': 'application/json'
    }
    
    shared_parameter_mapping = {}
    
    for param_dict in shared_parameters:
        source_id = param_dict.get('id')
        if not source_id:
            continue
        
        # Check if already migrated
        if hasattr(mappings, 'shared_parameters') and source_id in mappings.shared_parameters:
            shared_parameter_mapping[source_id] = mappings.shared_parameters[source_id]
            continue
        
        param_type = param_dict.get('type')
        title = param_dict.get('title')
        if not title:
            continue
        
        # Check for existing shared parameter by name (deduplication)
        normalized_title = title.strip().lower()
        if normalized_title in existing_params_by_title:
            existing_id = existing_params_by_title[normalized_title]
            shared_parameter_mapping[source_id] = existing_id
            if not hasattr(mappings, 'shared_parameters'):
                mappings.shared_parameters = {}
            mappings.shared_parameters[source_id] = existing_id
            continue
        
        is_enabled_for_all = param_dict.get('is_enabled_for_all_projects', False)
        project_codes_list = param_dict.get('project_codes', [])
        
        # Extract parameters structure
        parameters = param_dict.get('parameters', [])
        if not parameters:
            continue
        
        # Build parameters list
        parameters_list = []
        for param_item in parameters:
            param_item_dict = to_dict(param_item) if not isinstance(param_item, dict) else param_item
            if isinstance(param_item_dict, dict):
                param_title = param_item_dict.get('title')
                param_values = param_item_dict.get('values', [])
                if param_title and param_values:
                    parameters_list.append({
                        'title': param_title,
                        'values': param_values if isinstance(param_values, list) else [param_values]
                    })
        
        if not parameters_list:
            continue
        
        # Build payload
        payload = {
            'type': param_type,
            'title': title,
            'is_enabled_for_all_projects': is_enabled_for_all,
            'parameters': parameters_list
        }
        
        # Add project codes if not enabled for all
        if not is_enabled_for_all and project_codes_list:
            payload['project_codes'] = project_codes_list
        
        # Convert UUIDs to strings
        payload = convert_uuids_to_strings(payload)
        
        url = f"{base_url}/shared_parameter"
        
        try:
            response = qase_http.post(url, headers=headers, json=payload, timeout=60)
            if response.status_code == 200:
                response_data = response.json()
                if 'result' in response_data and 'id' in response_data['result']:
                    target_id = response_data['result']['id']
                    shared_parameter_mapping[source_id] = target_id
                else:
                    logger.warning(f"Shared parameter '{title}' created but no ID in response")
            else:
                logger.error(f"Failed to create shared parameter '{title}': {response.status_code} - {response.text}")
        except Exception as e:
            logger.error(f"Exception creating shared parameter '{title}': {e}")
    
    # Store in mappings
    if not hasattr(mappings, 'shared_parameters'):
        mappings.shared_parameters = {}
    mappings.shared_parameters.update(shared_parameter_mapping)
    
    stats.add_entity('shared_parameters', len(shared_parameters), len(shared_parameter_mapping))
    return shared_parameter_mapping
