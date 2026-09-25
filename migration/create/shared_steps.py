"""
Create shared steps in target Qase workspace.
"""
import logging
from typing import Dict, Any, List
from qase.api_client_v1.api.shared_steps_api import SharedStepsApi
from qase.api_client_v1.models import SharedStepCreate, SharedStepContentCreate
from qase_service import QaseService
from migration.utils import MigrationMappings, MigrationStats, retry_with_backoff, to_dict

logger = logging.getLogger(__name__)


def migrate_shared_steps(
    source_service: QaseService,
    target_service: QaseService,
    project_code_source: str,
    project_code_target: str,
    mappings: MigrationMappings,
    stats: MigrationStats
) -> Dict[str, str]:
    """
    Migrate shared steps from source to target workspace.
    
    Args:
        source_service: Source Qase service
        target_service: Target Qase service
        project_code_source: Source project code
        project_code_target: Target project code
        mappings: Migration mappings object
        stats: Migration stats object
    
    Returns:
        Dictionary mapping source hash to target hash
    """
    from migration.extract.shared_steps import extract_shared_steps
    
    shared_steps = extract_shared_steps(source_service, project_code_source)
    
    shared_steps_api_target = SharedStepsApi(target_service.client)
    shared_step_mapping = {}
    existing = dict(mappings.shared_steps.get(project_code_source, {}))
    
    for step_dict in shared_steps:
        source_hash = step_dict.get('hash')
        if not source_hash:
            continue
        if source_hash in existing:
            shared_step_mapping[source_hash] = existing[source_hash]
            continue
        
        processed_steps = []
        for step_item in step_dict.get('steps', []):
            step_item_dict = to_dict(step_item)
            action = step_item_dict.get('action', '').strip()
            if not action:
                action = 'No action'
            
            processed_steps.append(
                SharedStepContentCreate(
                    action=action,
                    expected_result=step_item_dict.get('expected_result') or step_item_dict.get('expected')
                )
            )
        
        shared_step_data = SharedStepCreate(
            title=step_dict['title'],
            steps=processed_steps
        )
        
        create_response = retry_with_backoff(
            shared_steps_api_target.create_shared_step,
            code=project_code_target,
            shared_step_create=shared_step_data
        )
        
        if create_response and hasattr(create_response, 'status') and create_response.status:
            if hasattr(create_response, 'result') and create_response.result:
                if hasattr(create_response.result, 'hash'):
                    target_hash = create_response.result.hash
                    shared_step_mapping[source_hash] = target_hash
    
    if project_code_source not in mappings.shared_steps:
        mappings.shared_steps[project_code_source] = {}
    mappings.shared_steps[project_code_source].update(shared_step_mapping)
    
    stats.add_entity('shared_steps', len(shared_steps), len(shared_step_mapping))
    stats.add_reused('shared_steps', sum(1 for h in shared_step_mapping if h in existing))
    return shared_step_mapping
