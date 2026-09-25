"""
Create defects in target Qase workspace.
"""
import logging
import re
from typing import Dict, Any, List
from qase_service import QaseService
from migration.step_logging import step_log_info
from migration.utils import MigrationMappings, MigrationStats, QaseRawApiClient
from migration.transform.attachments import replace_attachment_hashes_in_text

logger = logging.getLogger(__name__)


def migrate_defects(
    source_service: QaseService,
    target_service: QaseService,
    project_code_source: str,
    project_code_target: str,
    milestone_mapping: Dict[int, int],
    user_mapping: Dict[int, int],
    attachment_mapping: Dict[str, str],
    mappings: MigrationMappings,
    stats: MigrationStats
) -> Dict[int, int]:
    """
    Migrate defects from source to target workspace.
    
    Args:
        source_service: Source Qase service
        target_service: Target Qase service
        project_code_source: Source project code
        project_code_target: Target project code
        milestone_mapping: Mapping of source milestone ID -> target milestone ID
        user_mapping: Mapping of source user ID -> target user ID
        attachment_mapping: Mapping of source attachment hash -> target attachment hash
        mappings: Migration mappings object
        stats: Migration stats object
    
    Returns:
        Dictionary mapping source defect ID to target defect ID
    """
    from migration.extract.defects import extract_defects
    
    # Initialize raw API client
    try:
        base_url = target_service.client.configuration.host
        api_key_dict = target_service.client.configuration.api_key
        if isinstance(api_key_dict, dict):
            api_token = api_key_dict.get('TokenAuth') or api_key_dict.get('Token') or api_key_dict.get('token')
        else:
            api_token = None
        api_base = base_url.rstrip('/')
        if not api_base.endswith('/v1'):
            api_base = f"{api_base}/v1"
        raw_api_client = QaseRawApiClient(api_base, api_token) if api_token else None
    except Exception:
        logger.error("Cannot initialize raw API client for defects")
        return {}
    
    if not raw_api_client:
        logger.error("Raw API client not available")
        return {}
    
    # Normalize milestone_mapping: ensure keys are integers (JSON may store them as strings)
    if milestone_mapping:
        first_key = next(iter(milestone_mapping.keys()), None)
        if first_key is not None and isinstance(first_key, str):
            milestone_mapping = {int(k): v for k, v in milestone_mapping.items()}
    else:
        if project_code_source in getattr(mappings, 'milestones', {}):
            milestone_mapping = mappings.milestones[project_code_source]
            if milestone_mapping:
                first_key = next(iter(milestone_mapping.keys()), None)
                if first_key is not None and isinstance(first_key, str):
                    milestone_mapping = {int(k): v for k, v in milestone_mapping.items()}
        else:
            milestone_mapping = {}
    
    # Normalize user_mapping: ensure keys are integers
    if user_mapping:
        first_key = next(iter(user_mapping.keys()), None)
        if first_key is not None and isinstance(first_key, str):
            user_mapping = {int(k): v for k, v in user_mapping.items()}
    else:
        user_mapping = getattr(mappings, 'users', {})
        if user_mapping:
            first_key = next(iter(user_mapping.keys()), None)
            if first_key is not None and isinstance(first_key, str):
                user_mapping = {int(k): v for k, v in user_mapping.items()}
    
    defect_mapping = {}
    source_defects = extract_defects(source_service, project_code_source)
    
    if not source_defects:
        step_log_info(
            logger,
            "No defects found in source project %s",
            project_code_source,
        )
        return {}

    step_log_info(
        logger,
        "Found %s defects to migrate",
        len(source_defects),
    )
    
    # Map severity string to integer
    severity_map = {
        'undefined': 0,
        'blocker': 1,
        'critical': 2,
        'major': 3,
        'normal': 4,
        'minor': 5,
        'trivial': 6
    }
    
    # Defects migrated by a previous run are reused, not created a second time.
    existing = dict(mappings.defects.get(project_code_source, {}))

    for defect_dict in source_defects:
        source_defect_id = defect_dict.get('id')
        if not source_defect_id:
            continue
        if source_defect_id in existing:
            defect_mapping[source_defect_id] = existing[source_defect_id]
            continue
        
        # Map author_id
        source_author_id = defect_dict.get('author_id') or defect_dict.get('member_id')
        target_author_id = mappings.default_user_id
        if source_author_id:
            try:
                source_author_id_int = int(source_author_id)
                if source_author_id_int == 0:
                    target_author_id = mappings.default_user_id
                else:
                    target_author_id = mappings.get_user_id(source_author_id_int)
            except (ValueError, TypeError):
                target_author_id = mappings.default_user_id
        
        # Map severity
        severity_str = defect_dict.get('severity', 'undefined')
        severity_int = severity_map.get(severity_str.lower(), 0)
        
        # Map milestone_id
        target_milestone_id = None
        source_milestone_id = defect_dict.get('milestone_id')
        if source_milestone_id:
            try:
                source_milestone_id_int = int(source_milestone_id)
                target_milestone_id = milestone_mapping.get(source_milestone_id_int)
            except (ValueError, TypeError):
                pass
        
        target_attachments = []
        source_attachments = defect_dict.get('attachments', [])
        if source_attachments and attachment_mapping:
            for att_item in source_attachments:
                source_hash = None
                if isinstance(att_item, str):
                    source_hash = att_item
                elif isinstance(att_item, dict):
                    source_hash = att_item.get('hash') or att_item.get('attachment_hash') or att_item.get('id')
                    if not source_hash and att_item.get('url'):
                        match = re.search(r'/attachment/([a-f0-9]{32,64})/', att_item.get('url'), re.IGNORECASE)
                        if match:
                            source_hash = match.group(1)
                
                if source_hash:
                    source_hash_str = str(source_hash).strip()
                    mapped_hash = (
                        attachment_mapping.get(source_hash_str.lower()) or 
                        attachment_mapping.get(source_hash_str) or
                        attachment_mapping.get(source_hash_str.upper())
                    )
                    if mapped_hash:
                        target_attachments.append(str(mapped_hash).strip())
        
        actual_result = defect_dict.get('actual_result', '')
        if attachment_mapping:
            target_workspace_hash = getattr(mappings, 'target_workspace_hash', None)
            actual_result = replace_attachment_hashes_in_text(actual_result, attachment_mapping, target_workspace_hash)
        
        source_status = defect_dict.get('status')
        should_resolve = False
        
        if source_status is not None:
            if isinstance(source_status, str):
                if source_status.lower() in ['resolved', 'closed', 'invalid', 'duplicate']:
                    should_resolve = True
            elif isinstance(source_status, int) and source_status > 0:
                should_resolve = True
        
        defect_data = {
            'title': defect_dict.get('title', ''),
            'actual_result': actual_result,
            'severity': severity_int,
            'author_id': target_author_id
        }
        
        if target_milestone_id:
            defect_data['milestone_id'] = target_milestone_id
        
        if target_attachments:
            defect_data['attachments'] = target_attachments
        
        try:
            target_defect_id = raw_api_client.create_defect(project_code_target, defect_data)
        except Exception as e:
            logger.error(f"Error creating defect '{defect_dict.get('title', 'Unknown')}': {e}")
            continue
        
        if target_defect_id:
            defect_mapping[source_defect_id] = target_defect_id
            
            if should_resolve:
                raw_api_client.resolve_defect(project_code_target, target_defect_id)
    
    if project_code_source not in mappings.defects:
        mappings.defects[project_code_source] = {}
    mappings.defects[project_code_source].update(defect_mapping)
    
    stats.add_entity('defects', len(source_defects), len(defect_mapping))
    stats.add_reused('defects', sum(1 for d in defect_mapping if d in existing))
    return defect_mapping
