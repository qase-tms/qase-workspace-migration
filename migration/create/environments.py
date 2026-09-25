"""
Create environments in target Qase workspace.
"""
import logging
from typing import Dict, Any, List
from qase.api_client_v1.api.environments_api import EnvironmentsApi
from qase.api_client_v1.models import EnvironmentCreate
from qase_service import QaseService
from migration.utils import MigrationMappings, MigrationStats, retry_with_backoff, to_dict

logger = logging.getLogger(__name__)


def migrate_environments(
    source_service: QaseService,
    target_service: QaseService,
    project_code_source: str,
    project_code_target: str,
    mappings: MigrationMappings,
    stats: MigrationStats
) -> Dict[int, int]:
    """
    Migrate environments from source to target workspace.
    
    Args:
        source_service: Source Qase service
        target_service: Target Qase service
        project_code_source: Source project code
        project_code_target: Target project code
        mappings: Migration mappings object
        stats: Migration stats object
    
    Returns:
        Dictionary mapping source environment ID to target ID
    """
    from migration.extract.environments import extract_environments
    
    environments = extract_environments(source_service, project_code_source)
    
    environments_api_target = EnvironmentsApi(target_service.client)
    environment_mapping = {}
    reused = 0
    
    for env_dict in environments:
        source_id = env_dict.get('id')
        if not source_id:
            continue
        
        # Skip if already mapped
        if project_code_source in mappings.environments and source_id in mappings.environments[project_code_source]:
            environment_mapping[source_id] = mappings.environments[project_code_source][source_id]
            reused += 1
            continue
        
        title = env_dict.get('title')
        slug = env_dict.get('slug')
        host = env_dict.get('host')
        description = env_dict.get('description')
        
        if not title:
            continue
        
        env_data = EnvironmentCreate(
            title=title,
            slug=slug or '',
            host=host or ''
        )
        
        if description:
            env_data.description = description
        
        create_response = retry_with_backoff(
            environments_api_target.create_environment,
            code=project_code_target,
            environment_create=env_data
        )
        
        if create_response:
            target_id = None
            if hasattr(create_response, 'status') and hasattr(create_response, 'result'):
                if create_response.status and create_response.result:
                    target_id = getattr(create_response.result, 'id', None)
            elif hasattr(create_response, 'id'):
                target_id = create_response.id
            elif hasattr(create_response, 'result'):
                result = create_response.result
                target_id = getattr(result, 'id', None)
            
            if target_id:
                environment_mapping[source_id] = target_id
    
    if project_code_source not in mappings.environments:
        mappings.environments[project_code_source] = {}
    mappings.environments[project_code_source].update(environment_mapping)
    
    stats.add_entity('environments', len(environments), len(environment_mapping))
    stats.add_reused('environments', reused)
    return environment_mapping
