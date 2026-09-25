"""
Attachment transformation utilities - extract and replace attachment hashes in text fields.
"""
import re
import logging
from typing import Dict, List, Optional, Set, Any

logger = logging.getLogger(__name__)


def extract_attachment_hashes_from_text(text: str) -> Set[str]:
    """
    Extract attachment hashes from markdown image links in text fields.
    
    Pattern: ![filename](https://.../attachment/{HASH}/filename)
    The hash is typically a 40-character SHA-1 hash.
    
    Args:
        text: Text content that may contain markdown image links
        
    Returns:
        Set of attachment hash strings found in the text
    """
    if not text or not isinstance(text, str):
        return set()
    
    # /attachment/{hash}/ and /attachments/{hash}/; hash may be UUID (reporters, video/screenshot links)
    pattern = (
        r"/attachments?/"
        r"([a-f0-9]{32,64}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})"
        r"/"
    )

    hashes = set()
    matches = re.findall(pattern, text, re.IGNORECASE)
    for match in matches:
        hashes.add(str(match).replace("-", "").lower())

    return hashes


def extract_attachment_urls_from_text(text: str) -> Dict[str, str]:
    """
    Extract attachment URLs and their hashes from markdown image links.
    
    Pattern: ![filename](https://.../attachment/{HASH}/filename)
    
    Args:
        text: Text content that may contain markdown image links
        
    Returns:
        Dictionary mapping hash -> full URL
    """
    if not text or not isinstance(text, str):
        return {}
    
    # Pattern to match full markdown image links: ![filename](URL)
    # Extract both the URL and hash
    pattern = (
        r'!\[[^\]]*\]\('
        r'(https://[^\)]+/attachments?/'
        r'([a-f0-9]{32,64}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})'
        r'/[^\)]+)\)'
    )

    url_map = {}
    matches = re.findall(pattern, text, re.IGNORECASE)
    for full_url, hash_match in matches:
        url_map[str(hash_match).replace("-", "").lower()] = full_url

    return url_map


def extract_attachment_hashes_from_dict(data: Dict[str, Any], text_fields: List[str]) -> Set[str]:
    """
    Extract attachment hashes from multiple text fields in a dictionary.
    
    Args:
        data: Dictionary containing the data
        text_fields: List of field names to check for attachments
        
    Returns:
        Set of attachment hash strings found in all text fields
    """
    all_hashes = set()
    
    for field_name in text_fields:
        field_value = data.get(field_name)
        if field_value:
            if isinstance(field_value, str):
                hashes = extract_attachment_hashes_from_text(field_value)
                # Ensure we only add strings, not dicts
                valid_hashes = {h for h in hashes if isinstance(h, str)}
                all_hashes.update(valid_hashes)
            elif isinstance(field_value, list):
                # Handle list of strings (e.g., custom fields)
                for item in field_value:
                    if isinstance(item, dict) and 'value' in item:
                        # Custom field with value property
                        value_text = item.get('value', '')
                        if isinstance(value_text, str):
                            hashes = extract_attachment_hashes_from_text(value_text)
                            valid_hashes = {h for h in hashes if isinstance(h, str)}
                            all_hashes.update(valid_hashes)
                    elif isinstance(item, str):
                        hashes = extract_attachment_hashes_from_text(item)
                        valid_hashes = {h for h in hashes if isinstance(h, str)}
                        all_hashes.update(valid_hashes)
            elif isinstance(field_value, dict):
                # Handle nested dictionaries (e.g., custom fields)
                if 'value' in field_value:
                    value_text = field_value.get('value', '')
                    if isinstance(value_text, str):
                        hashes = extract_attachment_hashes_from_text(value_text)
                        valid_hashes = {h for h in hashes if isinstance(h, str)}
                        all_hashes.update(valid_hashes)
    
    return all_hashes


def extract_attachment_urls_from_dict(data: Dict[str, Any], text_fields: List[str]) -> Dict[str, str]:
    """
    Extract attachment URLs from multiple text fields in a dictionary.
    
    Args:
        data: Dictionary containing the data
        text_fields: List of field names to check for attachments
        
    Returns:
        Dictionary mapping hash -> URL
    """
    url_map = {}
    
    for field_name in text_fields:
        field_value = data.get(field_name)
        if field_value:
            if isinstance(field_value, str):
                urls = extract_attachment_urls_from_text(field_value)
                url_map.update(urls)
            elif isinstance(field_value, list):
                for item in field_value:
                    if isinstance(item, dict) and 'value' in item:
                        value_text = item.get('value', '')
                        if isinstance(value_text, str):
                            urls = extract_attachment_urls_from_text(value_text)
                            url_map.update(urls)
                    elif isinstance(item, str):
                        urls = extract_attachment_urls_from_text(item)
                        url_map.update(urls)
            elif isinstance(field_value, dict):
                if 'value' in field_value:
                    value_text = field_value.get('value', '')
                    if isinstance(value_text, str):
                        urls = extract_attachment_urls_from_text(value_text)
                        url_map.update(urls)
    
    return url_map


def replace_attachment_hashes_in_text(text: str, attachment_mapping: Dict[str, str], target_workspace_hash: Optional[str] = None) -> str:
    """
    Replace attachment hashes and workspace hash in markdown image links.
    
    URL pattern: https://.../public/team/{WORKSPACE_HASH}/attachment/{ATTACHMENT_HASH}/filename
    
    Args:
        text: Text content containing markdown image links
        attachment_mapping: Dictionary mapping source_hash -> target_hash
        target_workspace_hash: Target workspace/team hash to replace source workspace hash
        
    Returns:
        Text with attachment hashes and workspace hash replaced
    """
    if not text or not isinstance(text, str) or not attachment_mapping:
        return text
    
    # Pattern to match full attachment URLs with both workspace hash and attachment hash
    # Matches: https://.../public/team/{WORKSPACE_HASH}/attachment/{ATTACHMENT_HASH}/filename
    full_url_pattern = r'(https://[^/]+/public/team/)([a-f0-9]{32,64})(/attachment/)([a-f0-9]{32,64})(/[^\)]+)'
    
    def replace_full_url(match):
        url_prefix = match.group(1)  # https://.../public/team/
        old_workspace_hash = match.group(2)  # Workspace hash
        attachment_prefix = match.group(3)  # /attachment/
        old_attachment_hash = match.group(4).lower()  # Attachment hash (normalized)
        url_suffix = match.group(5)  # /filename
        
        # Replace attachment hash
        new_attachment_hash = attachment_mapping.get(old_attachment_hash)
        if not new_attachment_hash:
            # Try case-insensitive lookup
            for key, value in attachment_mapping.items():
                if key.lower() == old_attachment_hash:
                    new_attachment_hash = value
                    break
        
        # Replace workspace hash if target workspace hash is provided
        new_workspace_hash = target_workspace_hash if target_workspace_hash else old_workspace_hash
        
        if new_attachment_hash:
            return f"{url_prefix}{new_workspace_hash}{attachment_prefix}{new_attachment_hash}{url_suffix}"
        else:
            logger.debug(f"Attachment hash {old_attachment_hash[:8]}... not found in mapping")
            return match.group(0)  # Return original if no mapping found
    
    # Replace full URLs first (with workspace hash)
    text = re.sub(full_url_pattern, replace_full_url, text, flags=re.IGNORECASE)
    
    # Also handle URLs that only have /attachment/{HASH}/ pattern (fallback)
    pattern = r'(/attachment/)([a-f0-9]{32,64})(/)'
    
    def replace_hash(match):
        prefix = match.group(1)
        old_hash = match.group(2).lower()
        suffix = match.group(3)
        
        new_hash = attachment_mapping.get(old_hash)
        if not new_hash:
            for key, value in attachment_mapping.items():
                if key.lower() == old_hash:
                    new_hash = value
                    break
        
        if new_hash:
            return f"{prefix}{new_hash}{suffix}"
        return match.group(0)
    
    return re.sub(pattern, replace_hash, text, flags=re.IGNORECASE)
