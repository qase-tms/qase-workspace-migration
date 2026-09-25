"""
Create cases in target Qase workspace.
"""
import logging
import re
from typing import Dict, Any, Optional, TYPE_CHECKING
from qase_service import QaseService

if TYPE_CHECKING:
    from migration.progress import ProjectMigrationProgress
from migration.utils import (
    MigrationMappings, MigrationStats, to_dict, preserve_or_hash_id,
    QaseRawApiClient
)
from migration.transform.attachments import replace_attachment_hashes_in_text

logger = logging.getLogger(__name__)


def transform_case_data(
    case_dict: Dict[str, Any],
    suite_mapping: Dict[int, int],
    custom_field_mapping: Dict[int, int],
    milestone_mapping: Dict[int, int],
    shared_step_mapping: Dict[str, str],
    shared_parameter_mapping: Dict[str, str],
    user_mapping: Dict[int, int],
    attachment_mapping: Dict[str, str],
    mappings: MigrationMappings,
    preserve_ids: bool = True
) -> Optional[Dict[str, Any]]:
    """
    Transform a case dictionary from source format to target format.
    
    Returns:
        Transformed case data dict, or None if transformation fails
    """
    source_suite_id = case_dict.get('suite_id')
    target_suite_id = None
    
    if source_suite_id:
        target_suite_id = suite_mapping.get(source_suite_id)
    
    case_id = case_dict.get('id')
    if case_id and preserve_ids:
        case_id = preserve_or_hash_id(case_id, preserve_ids)
    
    tags_list = case_dict.get('tags', [])
    processed_tags = []
    for tag in tags_list:
        if isinstance(tag, dict):
            tag_title = tag.get('title') or tag.get('name') or str(tag)
            processed_tags.append(tag_title)
        elif isinstance(tag, str):
            processed_tags.append(tag)
        else:
            processed_tags.append(str(tag))
    
    created_at = case_dict.get('created_at')
    if created_at and hasattr(created_at, 'isoformat'):
        created_at = created_at.isoformat()
    elif created_at and isinstance(created_at, str):
        pass  # Already a string
    else:
        created_at = None
    
    updated_at = case_dict.get('updated_at')
    if updated_at and hasattr(updated_at, 'isoformat'):
        updated_at = updated_at.isoformat()
    elif updated_at and isinstance(updated_at, str):
        pass  # Already a string
    else:
        updated_at = None
    
    # Map author_id from member_id (same as user_id for runs)
    source_user_id = case_dict.get('member_id') or case_dict.get('created_by') or case_dict.get('author_id')
    target_author_id = mappings.default_user_id
    if source_user_id:
        try:
            source_user_id_int = int(source_user_id)
            if source_user_id_int == 0:
                target_author_id = mappings.default_user_id
            else:
                target_author_id = mappings.get_user_id(source_user_id_int)
        except (ValueError, TypeError):
            target_author_id = mappings.default_user_id
    
    case_data = {
        'title': case_dict.get('title', ''),
        'description': case_dict.get('description', ''),
        'preconditions': case_dict.get('preconditions', ''),
        'postconditions': case_dict.get('postconditions', ''),
        'severity': case_dict.get('severity', 2),
        'priority': case_dict.get('priority', 2),
        'type': case_dict.get('type', 1),
        'behavior': case_dict.get('behavior', 1),
        'automation': case_dict.get('automation', 0),
        'status': case_dict.get('status', 1),
        'tags': processed_tags,
        'created_at': created_at,
        'updated_at': updated_at,
        'author_id': target_author_id,
        'milestone_id': milestone_mapping.get(case_dict.get('milestone_id')) if case_dict.get('milestone_id') else None,
        'attachments': [],
        'is_flaky': case_dict.get('is_flaky', 0),
        'custom_field': {}
    }
    
    # Extract and process params
    source_params = case_dict.get('params')
    source_parameters = case_dict.get('parameters')
    
    if source_params and isinstance(source_params, dict) and len(source_params) > 0:
        case_data['params'] = source_params
    else:
        case_data['params'] = {}
    
    # Preserve parameters structure for cases with parameters field
    if source_parameters and isinstance(source_parameters, list):
        parameters_list = []
        for param_item in source_parameters:
            param_dict = to_dict(param_item) if not isinstance(param_item, dict) else param_item
            if isinstance(param_dict, dict):
                param_type = param_dict.get('type')
                source_shared_id = param_dict.get('shared_id')
                
                # Map shared parameter ID if it exists
                target_shared_id = None
                if source_shared_id:
                    # Convert to string for consistent lookup
                    source_shared_id_str = str(source_shared_id)
                    if source_shared_id_str in shared_parameter_mapping:
                        target_shared_id = shared_parameter_mapping[source_shared_id_str]
                    # Also try original format
                    elif source_shared_id in shared_parameter_mapping:
                        target_shared_id = shared_parameter_mapping[source_shared_id]
                
                # Shared parameter reference
                if target_shared_id:
                    parameters_list.append({
                        'shared_id': str(target_shared_id)
                    })
                    continue
                
                # Handle single type parameter
                if param_type == 'single' and 'item' in param_dict:
                    item = param_dict['item']
                    item_dict = to_dict(item) if not isinstance(item, dict) else item
                    if isinstance(item_dict, dict):
                        param_name = item_dict.get('title') or item_dict.get('name')
                        param_values = item_dict.get('values') or []
                        if param_name and param_values:
                            parameters_list.append({
                                'title': param_name,
                                'values': param_values if isinstance(param_values, list) else [param_values]
                            })
                # Handle group type parameter
                elif param_type == 'group' and 'items' in param_dict:
                    items = param_dict['items']
                    if isinstance(items, list):
                        group_items = []
                        for item in items:
                            item_dict = to_dict(item) if not isinstance(item, dict) else item
                            if isinstance(item_dict, dict):
                                param_name = item_dict.get('title') or item_dict.get('name')
                                param_values = item_dict.get('values') or []
                                if param_name and param_values:
                                    group_items.append({
                                        'title': param_name,
                                        'values': param_values if isinstance(param_values, list) else [param_values]
                                    })
                        if group_items:
                            parameters_list.append({
                                'items': group_items
                            })
        
        if parameters_list:
            case_data['parameters'] = parameters_list
            case_data['_has_parameters_structure'] = True
    
    if case_id:
        case_data['id'] = case_id
    
    if target_suite_id:
        case_data['suite_id'] = target_suite_id
    
    case_attachments = case_dict.get('attachments', []) or []
    mapped_case_attachments = []
    if case_attachments and attachment_mapping:
        for att_item in case_attachments:
            source_hash = None
            if isinstance(att_item, str):
                source_hash = att_item
            elif isinstance(att_item, dict):
                if 'hash' in att_item:
                    source_hash = att_item['hash']
                elif 'url' in att_item:
                    url = att_item['url']
                    match = re.search(r'/attachment/([a-f0-9]{32,64})/', url, re.IGNORECASE)
                    if match:
                        source_hash = match.group(1)
            
            if source_hash:
                mapped_hash = attachment_mapping.get(source_hash)
                if mapped_hash:
                    mapped_case_attachments.append(mapped_hash)
    case_data['attachments'] = mapped_case_attachments
    
    target_workspace_hash = getattr(mappings, 'target_workspace_hash', None)
    if attachment_mapping:
        case_data['description'] = replace_attachment_hashes_in_text(
            case_data.get('description', ''), attachment_mapping, target_workspace_hash
        )
        case_data['preconditions'] = replace_attachment_hashes_in_text(
            case_data.get('preconditions', ''), attachment_mapping, target_workspace_hash
        )
        case_data['postconditions'] = replace_attachment_hashes_in_text(
            case_data.get('postconditions', ''), attachment_mapping, target_workspace_hash
        )
    
    if 'custom_fields' in case_dict and case_dict['custom_fields']:
        for custom_field_item in case_dict['custom_fields']:
            if isinstance(custom_field_item, dict):
                field_id_source = custom_field_item.get('id')
                value = custom_field_item.get('value')
                if field_id_source is not None:
                    field_id_target = custom_field_mapping.get(int(field_id_source))
                    if field_id_target:
                        mapped_value = value
                        if isinstance(value, str) and attachment_mapping:
                            mapped_value = replace_attachment_hashes_in_text(value, attachment_mapping, target_workspace_hash)
                        case_data['custom_field'][str(field_id_target)] = mapped_value
    elif 'custom_field' in case_dict and case_dict['custom_field']:
        for field_id_source, value in case_dict['custom_field'].items():
            field_id_target = custom_field_mapping.get(int(field_id_source))
            if field_id_target:
                mapped_value = value
                if isinstance(value, str) and attachment_mapping:
                    mapped_value = replace_attachment_hashes_in_text(value, attachment_mapping, target_workspace_hash)
                case_data['custom_field'][str(field_id_target)] = mapped_value
    
    if 'steps' in case_dict and case_dict['steps']:
        processed_steps = []
        for step in case_dict['steps']:
            step_dict = to_dict(step)
            
            # Check for shared step reference
            source_hash = None
            if isinstance(step_dict, dict):
                if 'shared' in step_dict:
                    source_hash = step_dict['shared']
                elif 'shared_step' in step_dict:
                    shared_step_obj = step_dict['shared_step']
                    if isinstance(shared_step_obj, dict):
                        source_hash = shared_step_obj.get('hash')
                    elif hasattr(shared_step_obj, 'hash'):
                        source_hash = getattr(shared_step_obj, 'hash', None)
                elif 'shared_step_hash' in step_dict:
                    source_hash = step_dict['shared_step_hash']
            
            if source_hash:
                target_hash = shared_step_mapping.get(source_hash)
                if target_hash:
                    processed_steps.append({'shared': target_hash})
                continue
            
            # Regular step processing
            if isinstance(step_dict, dict):
                step_attachments = step_dict.get('attachments', []) or []
                mapped_step_attachments = []
                if step_attachments and attachment_mapping:
                    for att_hash in step_attachments:
                        mapped_hash = attachment_mapping.get(att_hash)
                        if mapped_hash:
                            mapped_step_attachments.append(mapped_hash)
                
                step_action = step_dict.get('action', '')
                step_expected_result = step_dict.get('expected_result')
                step_data = step_dict.get('data')
                
                if attachment_mapping:
                    step_action = replace_attachment_hashes_in_text(step_action, attachment_mapping, target_workspace_hash)
                    if step_expected_result:
                        step_expected_result = replace_attachment_hashes_in_text(step_expected_result, attachment_mapping, target_workspace_hash)
                    if step_data:
                        step_data = replace_attachment_hashes_in_text(step_data, attachment_mapping, target_workspace_hash)
                
                processed_steps.append({
                    'action': step_action,
                    'expected_result': step_expected_result,
                    'data': step_data,
                    'position': step_dict.get('position', len(processed_steps) + 1),
                    'attachments': mapped_step_attachments
                })
        
        case_data['steps'] = processed_steps
    
    return case_data


def migrate_cases(
    source_service: QaseService,
    target_service: QaseService,
    project_code_source: str,
    project_code_target: str,
    suite_mapping: Dict[int, int],
    custom_field_mapping: Dict[int, int],
    milestone_mapping: Dict[int, int],
    shared_step_mapping: Dict[str, str],
    shared_parameter_mapping: Dict[str, str],
    user_mapping: Dict[int, int],
    mappings: MigrationMappings,
    stats: MigrationStats,
    preserve_ids: bool = True,
    progress: Optional["ProjectMigrationProgress"] = None,
) -> Dict[int, int]:
    """
    Migrate test cases from source to target workspace.
    
    Returns:
        Dictionary mapping source case ID to target case ID
    """
    from migration.extract.cases import extract_cases
    
    case_mapping = {}
    limit = 20
    
    raw_api_client = QaseRawApiClient(
        base_url=target_service.client.configuration.host,
        api_token=target_service.api_token
    )
    
    attachment_mapping = {}
    if project_code_source in mappings.attachments:
        attachment_mapping = mappings.attachments[project_code_source]
        normalized_mapping = {}
        for key, value in attachment_mapping.items():
            normalized_mapping[key.lower()] = value
            normalized_mapping[key] = value
        attachment_mapping = normalized_mapping
    
    # Use workspace-level shared parameter mapping from mappings
    if hasattr(mappings, 'shared_parameters') and mappings.shared_parameters:
        # Merge with any passed mapping (workspace-level takes precedence)
        if shared_parameter_mapping:
            mappings.shared_parameters.update(shared_parameter_mapping)
        shared_parameter_mapping = mappings.shared_parameters
    elif not shared_parameter_mapping:
        shared_parameter_mapping = {}
    
    all_source_cases = extract_cases(source_service, project_code_source, limit)
    if progress:
        progress.reconcile_case_cap(len(all_source_cases))
    
    if not all_source_cases:
        if project_code_source not in mappings.cases:
            mappings.cases[project_code_source] = {}
        stats.add_entity('cases', 0, 0)
        return {}
    
    batch_size = limit
    total_cases_processed = len(all_source_cases)

    # Cases migrated by a previous run are reused, not created a second time.
    existing = dict(mappings.cases.get(project_code_source, {}))
    for case_dict in all_source_cases:
        source_id = case_dict.get('id')
        if source_id in existing:
            case_mapping[source_id] = existing[source_id]
    reused = len(case_mapping)
    if reused:
        logger.info(
            "%s: %s case(s) already migrated by a previous run, reusing them",
            project_code_source, reused,
        )
        if progress:
            progress.add_cases(reused)
    all_source_cases = [c for c in all_source_cases if c.get('id') not in case_mapping]
    
    for batch_start in range(0, len(all_source_cases), batch_size):
        batch_cases = all_source_cases[batch_start:batch_start + batch_size]
        
        cases_to_create = []
        
        for case_dict in batch_cases:
            case_data = transform_case_data(
                case_dict, suite_mapping, custom_field_mapping,
                milestone_mapping, shared_step_mapping, shared_parameter_mapping,
                user_mapping, attachment_mapping, mappings, preserve_ids
            )
            
            if not case_data:
                continue
            
            source_id = case_dict.get('id')
            case_data.pop('_has_parameters_structure', False)
            case_data['_source_id'] = source_id
            cases_to_create.append(case_data)
        
        # Create cases using raw API
        if cases_to_create:
            case_data_list = []
            source_ids_batch = []
            
            for case_data in cases_to_create:
                source_id = case_data.pop('_source_id')
                case_data_list.append(case_data)
                source_ids_batch.append(source_id)
            
            created_ids = raw_api_client.create_cases_bulk(project_code_target, case_data_list)
            if created_ids:
                for idx, source_id in enumerate(source_ids_batch):
                    if idx < len(created_ids):
                        case_mapping[source_id] = created_ids[idx]
            # A rejected or short response means source cases silently never
            # arrived, and every suite/plan/run referencing them loses them too.
            # Report it loudly instead of leaving it to a log-file warning.
            n_missing = len(source_ids_batch) - len(created_ids or [])
            if n_missing > 0:
                lost_ids = source_ids_batch[len(created_ids or []):]
                message = (
                    f"{n_missing} of {len(source_ids_batch)} cases were not created in "
                    f"{project_code_target} (source ids {lost_ids}). Runs, plans and suites "
                    f"referencing them will be missing those cases. See the log for the API error."
                )
                logger.error(message)
                print(f"ERROR: {message}")
                stats.add_error('cases', message)
        if progress:
            progress.add_cases(len(batch_cases))
    
    if project_code_source not in mappings.cases:
        mappings.cases[project_code_source] = {}
    mappings.cases[project_code_source].update(case_mapping)
    
    stats.add_entity('cases', total_cases_processed, len(case_mapping))
    stats.add_reused('cases', reused)
    return case_mapping
