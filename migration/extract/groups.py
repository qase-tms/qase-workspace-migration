"""
Extract groups from source Qase workspace.
Note: Qase API doesn't have a direct groups endpoint, so we extract from SCIM if available.
"""
import logging
from typing import List, Dict, Any
from qase_service import QaseService

logger = logging.getLogger(__name__)


def extract_groups(source_service: QaseService) -> List[Dict[str, Any]]:
    """
    Extract groups from source workspace.
    
    Currently, Qase API doesn't provide a direct groups endpoint.
    This function attempts to extract groups via SCIM if available.
    
    Args:
        source_service: Source Qase service
    
    Returns:
        List of group dictionaries with user_ids
    """
    groups = []
    
    # Try to get groups via SCIM if available
    if source_service.scim_client:
        try:
            scim_groups = source_service.scim_client.get_all_groups()
            for group in scim_groups:
                group_dict = {
                    'id': group.get('id'),
                    'displayName': group.get('displayName'),
                    'members': []
                }
                
                # Extract member IDs
                members = group.get('members', [])
                for member in members:
                    member_id = member.get('value') if isinstance(member, dict) else member
                    if member_id:
                        group_dict['members'].append(member_id)
                
                groups.append(group_dict)
            
            logger.info(f"Extracted {len(groups)} groups from source via SCIM")
        except Exception as e:
            logger.warning(f"Failed to extract groups via SCIM: {e}")
    
    # If no groups found, return empty list
    # Note: In the future, groups might be extracted from project memberships
    if not groups:
        logger.info("No groups found in source workspace (SCIM not available or no groups)")
    
    return groups
