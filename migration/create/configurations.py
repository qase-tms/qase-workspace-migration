"""
Create configurations in target Qase workspace.
"""
import logging
from typing import Dict, Any, List, Tuple
from qase.api_client_v1.api.configurations_api import ConfigurationsApi
from qase.api_client_v1.models import ConfigurationGroupCreate, ConfigurationCreate
from qase_service import QaseService
from migration.utils import MigrationMappings, MigrationStats, retry_with_backoff, to_dict

logger = logging.getLogger(__name__)


def migrate_configurations(
    source_service: QaseService,
    target_service: QaseService,
    project_code_source: str,
    project_code_target: str,
    mappings: MigrationMappings,
    stats: MigrationStats
) -> Tuple[Dict[int, int], Dict[int, int]]:
    """
    Migrate configurations from source to target workspace.
    
    Args:
        source_service: Source Qase service
        target_service: Target Qase service
        project_code_source: Source project code
        project_code_target: Target project code
        mappings: Migration mappings object
        stats: Migration stats object
    
    Returns:
        Tuple of (configuration_group_mapping, configuration_mapping)
    """
    from migration.extract.configurations import extract_configurations
    
    groups_list = extract_configurations(source_service, project_code_source)
    
    configs_api_target = ConfigurationsApi(target_service.client)
    group_mapping = {}
    config_mapping = {}
    
    existing_groups = dict(mappings.configuration_groups.get(project_code_source, {}))

    for group_dict in groups_list:
        # A group migrated by a previous run is reused rather than created again.
        target_group_id = existing_groups.get(group_dict.get('id'))
        if target_group_id:
            create_response = True
        else:
            group_data = ConfigurationGroupCreate(title=group_dict['title'])
            create_response = retry_with_backoff(
                configs_api_target.create_configuration_group,
                code=project_code_target,
                configuration_group_create=group_data
            )
        
        if create_response and not target_group_id:
            if hasattr(create_response, 'status') and hasattr(create_response, 'result'):
                if create_response.status and create_response.result:
                    target_group_id = getattr(create_response.result, 'id', None)
            elif hasattr(create_response, 'id'):
                target_group_id = create_response.id
            elif hasattr(create_response, 'result'):
                result = create_response.result
                target_group_id = getattr(result, 'id', None)

        if create_response:
            if target_group_id:
                source_group_id = group_dict.get('id')
                group_mapping[source_group_id] = target_group_id
                
                # Check for configurations in various possible field names
                configs_list = None
                if 'configs' in group_dict:
                    configs_list = group_dict['configs']
                elif 'configurations' in group_dict:
                    configs_list = group_dict['configurations']
                elif 'entities' in group_dict:
                    configs_list = group_dict['entities']
                
                if configs_list:
                    for config in configs_list:
                        config_dict = to_dict(config) if not isinstance(config, dict) else config
                        config_title = config_dict.get('title')
                        source_config_id = config_dict.get('id')
                        
                        if not config_title:
                            continue
                        
                        # Skip if already mapped
                        if project_code_source in mappings.configurations and source_config_id in mappings.configurations[project_code_source]:
                            config_mapping[source_config_id] = mappings.configurations[project_code_source][source_config_id]
                            continue
                        
                        config_data = ConfigurationCreate(
                            title=config_title,
                            group_id=target_group_id
                        )
                        
                        config_create_response = retry_with_backoff(
                            configs_api_target.create_configuration,
                            code=project_code_target,
                            configuration_create=config_data
                        )
                        
                        if config_create_response:
                            target_config_id = None
                            if hasattr(config_create_response, 'status') and hasattr(config_create_response, 'result'):
                                if config_create_response.status and config_create_response.result:
                                    target_config_id = getattr(config_create_response.result, 'id', None)
                            elif hasattr(config_create_response, 'id'):
                                target_config_id = config_create_response.id
                            elif hasattr(config_create_response, 'result'):
                                result = config_create_response.result
                                target_config_id = getattr(result, 'id', None)
                            
                            if target_config_id:
                                config_mapping[source_config_id] = target_config_id
    
    if project_code_source not in mappings.configuration_groups:
        mappings.configuration_groups[project_code_source] = {}
    mappings.configuration_groups[project_code_source].update(group_mapping)
    
    if project_code_source not in mappings.configurations:
        mappings.configurations[project_code_source] = {}
    mappings.configurations[project_code_source].update(config_mapping)
    
    # Count total configurations for stats
    total_configs = 0
    for group_dict in groups_list:
        if 'configs' in group_dict:
            total_configs += len(group_dict['configs'])
        elif 'configurations' in group_dict:
            total_configs += len(group_dict['configurations'])
        elif 'entities' in group_dict:
            total_configs += len(group_dict['entities'])
    
    stats.add_entity('configuration_groups', len(groups_list), len(group_mapping))
    stats.add_reused('configuration_groups', sum(1 for g in group_mapping if g in existing_groups))
    stats.add_entity('configurations', total_configs, len(config_mapping))
    return group_mapping, config_mapping
