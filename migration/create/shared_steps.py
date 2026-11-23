"""
Create shared steps in target Qase workspace.
"""
import logging
from typing import Dict, Any, List
from qase.api_client_v1.api.shared_steps_api import SharedStepsApi
from qase.api_client_v1.models import SharedStepCreate, SharedStepContentCreate
from qase_service import QaseService
from migration.utils import MigrationMappings, MigrationStats, retry_with_backoff, to_dict
from migration.create.cases import source_attachment_hash
from migration.transform.attachments import as_attachment_list, replace_attachment_hashes_in_text

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

    # Attachments are migrated before any project, so their mapping is ready.
    # Without it a shared step kept links to the source workspace, which stop
    # working when that workspace's links are signed and expire after an hour.
    attachment_mapping: Dict[str, str] = {}
    for key, value in (mappings.attachments.get(project_code_source) or {}).items():
        attachment_mapping[key] = value
        attachment_mapping[str(key).lower()] = value
    target_workspace_hash = getattr(mappings, 'target_workspace_hash', None)
    target_urls = getattr(mappings, 'attachment_urls', None)

    def rewrite(text: Any) -> Any:
        if not attachment_mapping or not isinstance(text, str):
            return text
        return replace_attachment_hashes_in_text(text, attachment_mapping, target_workspace_hash, target_urls)

    def map_attachments(items: Any) -> List[str]:
        out: List[str] = []
        for item in as_attachment_list(items):
            source_hash = source_attachment_hash(item)
            target_hash = attachment_mapping.get(source_hash) or attachment_mapping.get(str(source_hash).lower()) if source_hash else None
            if target_hash and target_hash not in out:
                out.append(target_hash)
        return out
    
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
            
            content = SharedStepContentCreate(
                action=rewrite(action),
                expected_result=rewrite(step_item_dict.get('expected_result') or step_item_dict.get('expected')),
            )
            if step_item_dict.get('data'):
                content.data = rewrite(step_item_dict['data'])
            step_attachments = map_attachments(step_item_dict.get('attachments'))
            if step_attachments:
                content.attachments = step_attachments
            processed_steps.append(content)
        
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
