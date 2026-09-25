"""
Migrate groups from source to target Qase workspace.
Creates groups and adds users via SCIM API.
"""
import logging
from typing import Dict, List, Any, Optional
from qase_service import QaseService
from migration.utils import MigrationMappings, MigrationStats

logger = logging.getLogger(__name__)


def migrate_groups(
    source_service: QaseService,
    target_service: QaseService,
    user_mapping: Dict[int, int],
    mappings: MigrationMappings,
    stats: MigrationStats,
    config: Optional[Dict[str, Any]] = None
) -> Dict[str, str]:
    """
    Migrate groups from source to target workspace.
    
    Args:
        source_service: Source Qase service
        target_service: Target Qase service
        user_mapping: Mapping of source user ID -> target user ID
        mappings: Migration mappings object
        stats: Migration stats object
        config: Configuration dictionary with group migration options:
            - groups.create: Whether to create groups from source (default: False)
    
    Returns:
        Dictionary mapping source group ID to target group ID
    """
    from migration.extract.groups import extract_groups
    
    config = config or {}
    create_groups = config.get('groups', {}).get('create', False)
    
    if not target_service.scim_client:
        logger.warning("SCIM client not available, skipping group migration")
        return {}
    
    if not create_groups:
        logger.info("Group creation disabled in config, skipping group migration")
        return {}
    
    group_mapping = {}
    
    # Extract source groups
    source_groups = extract_groups(source_service)
    
    # Get existing groups in target
    existing_groups_by_name = {}
    try:
        target_groups = target_service.scim_client.get_all_groups()
        for group in target_groups:
            name = group.get('displayName', '').lower()
            if name:
                existing_groups_by_name[name] = group.get('id')
    except Exception as e:
        logger.warning(f"Failed to get existing groups: {e}")
    
    # Migrate groups
    created_count = 0
    mapped_count = 0
    
    for source_group in source_groups:
        source_group_id = source_group.get('id')
        source_group_name = source_group.get('displayName')
        
        if not source_group_id or not source_group_name:
            continue
        
        # Check if group already exists
        normalized_name = source_group_name.lower()
        if normalized_name in existing_groups_by_name:
            target_group_id = existing_groups_by_name[normalized_name]
            group_mapping[source_group_id] = target_group_id
            mapped_count += 1
            logger.info(f"Group '{source_group_name}' already exists, using existing group")
        else:
            # Create group
            try:
                target_group_id = target_service.scim_client.create_group(source_group_name)
                if target_group_id:
                    group_mapping[source_group_id] = target_group_id
                    created_count += 1
                    existing_groups_by_name[normalized_name] = target_group_id
                    logger.info(f"Created group '{source_group_name}' (ID: {target_group_id})")
                else:
                    logger.warning(f"Failed to create group '{source_group_name}'")
                    continue
            except Exception as e:
                logger.error(f"Error creating group '{source_group_name}': {e}")
                continue
        
        # Add users to group
        source_member_ids = source_group.get('members', [])
        if source_member_ids:
            # Map source user IDs to target user IDs
            target_user_ids = []
            for source_member_id in source_member_ids:
                # Handle both string and int IDs
                if isinstance(source_member_id, str):
                    # Try to find in user_mapping by converting to int if possible
                    try:
                        source_user_id_int = int(source_member_id)
                        if source_user_id_int in user_mapping:
                            target_user_ids.append(str(user_mapping[source_user_id_int]))
                    except ValueError:
                        # If it's already a UUID string, check if it's in mappings
                        # For now, skip non-integer member IDs
                        pass
                elif isinstance(source_member_id, int):
                    if source_member_id in user_mapping:
                        target_user_id = user_mapping[source_member_id]
                        # SCIM uses string IDs
                        target_user_ids.append(str(target_user_id))
            
            if target_user_ids:
                try:
                    success = target_service.scim_client.add_users_to_group(
                        target_group_id,
                        target_user_ids
                    )
                    if success:
                        logger.info(f"Added {len(target_user_ids)} users to group '{source_group_name}'")
                    else:
                        logger.warning(f"Failed to add users to group '{source_group_name}'")
                except Exception as e:
                    logger.error(f"Error adding users to group '{source_group_name}': {e}")
    
    # Store group mappings
    if not hasattr(mappings, 'groups'):
        mappings.groups = {}
    mappings.groups.update(group_mapping)
    
    stats.add_entity('groups', len(source_groups), len(group_mapping))
    
    logger.info(f"Group migration complete: {mapped_count} mapped, {created_count} created")
    
    return group_mapping
