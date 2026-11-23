"""
Attachment transformation utilities - extract and replace attachment hashes in text fields.

Qase has used several link forms for a stored file. All of them are recognised:

- signed, in workspaces with private attachments (Nov 2026 onwards):
  ``https://app-<host>/v1/attachments/api/{workspace}/{hash}/{file}?exp=...&sig=...``
  The link is valid for an hour. The API re-signs it on every read.
- public: ``https://files-<host>/public/team/{workspace}/attachment/{hash}/{file}``
- other API and reporter forms: ``/attachment/{hash}/`` or ``/attachments/{hash}/``,
  where the hash may be a UUID.
"""
import re
import logging
from typing import Dict, List, Optional, Set, Any

logger = logging.getLogger(__name__)

_HASH = r"[a-f0-9]{32,64}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"

# The signed form is tried first so its workspace segment is never taken for
# the file's hash.
ATTACHMENT_URL_RE = re.compile(
    rf"/attachments/api/(?P<ws>[A-Za-z0-9_-]+)/(?P<signed>{_HASH})/"
    rf"|/attachments?/(?P<plain>{_HASH})/",
    re.IGNORECASE,
)

# A URL as it appears in markdown or plain text.
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s)\]\"'<>]+", re.IGNORECASE)

# A markdown image or link: ![name](url) or [name](url)
_MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\((https?://[^\s)]+)\)", re.IGNORECASE)


def _normalise_hash(raw: str) -> str:
    return str(raw).replace("-", "").lower()


def attachment_hash_from_url(url: Any) -> Optional[str]:
    """The source attachment hash inside a Qase file link, or None."""
    if not url:
        return None
    m = ATTACHMENT_URL_RE.search(str(url))
    if not m:
        return None
    return _normalise_hash(m.group("signed") or m.group("plain"))


def as_attachment_list(items: Any) -> List[Any]:
    """
    An attachments field as a list, whatever shape the API used.

    Qase sometimes serialises a step's attachment list with a gap in it as an
    object keyed by position, ``{"1": {...}}``, instead of an array. Reading
    that as a list loses the file, and the SDK refuses the whole result.
    """
    if items is None or items == "":
        return []
    if isinstance(items, list):
        return items
    if isinstance(items, tuple):
        return list(items)
    if isinstance(items, dict):
        if items and all(str(k).isdigit() for k in items):
            return [items[k] for k in sorted(items, key=lambda k: int(k))]
        return [items]
    return [items]


def _lookup(mapping: Dict[str, str], source_hash: str) -> Optional[str]:
    return mapping.get(source_hash) or mapping.get(source_hash.lower()) or mapping.get(source_hash.upper())


def extract_attachment_hashes_from_text(text: str) -> Set[str]:
    """
    Extract attachment hashes from links to Qase files in a text field.

    Args:
        text: Text content that may contain markdown image links

    Returns:
        Set of attachment hash strings found in the text
    """
    if not text or not isinstance(text, str):
        return set()
    return {
        _normalise_hash(m.group("signed") or m.group("plain"))
        for m in ATTACHMENT_URL_RE.finditer(text)
    }


def extract_attachment_urls_from_text(text: str) -> Dict[str, str]:
    """
    Extract attachment URLs and their hashes from markdown images and links.

    Args:
        text: Text content that may contain markdown image links

    Returns:
        Dictionary mapping hash -> full URL
    """
    if not text or not isinstance(text, str):
        return {}
    url_map = {}
    for url in _MARKDOWN_LINK_RE.findall(text):
        h = attachment_hash_from_url(url)
        if h:
            url_map.setdefault(h, url)
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


def _rewrite_url_in_place(url: str, new_hash: str, target_workspace_hash: Optional[str]) -> str:
    """
    Point a source link at the target file when the target's own link is unknown.

    Used only for mappings written before target links were recorded.
    """
    m = ATTACHMENT_URL_RE.search(url)
    if not m:
        return url
    if m.group("signed"):
        ws = target_workspace_hash or m.group("ws")
        out = f"{url[:m.start()]}/attachments/api/{ws}/{new_hash}/{url[m.end():]}"
        # The signature belonged to the source file and is useless here.
        return out.split("?", 1)[0]
    out = f"{url[:m.start('plain')]}{new_hash}{url[m.end('plain'):]}"
    if target_workspace_hash:
        out = re.sub(
            r"(/public/team/)[a-f0-9]{32,64}(/)",
            rf"\g<1>{target_workspace_hash}\g<2>",
            out,
            count=1,
            flags=re.IGNORECASE,
        )
    return out


def replace_attachment_hashes_in_text(
    text: str,
    attachment_mapping: Dict[str, str],
    target_workspace_hash: Optional[str] = None,
    target_urls: Optional[Dict[str, str]] = None,
) -> str:
    """
    Point every link to a migrated source file at its copy in the target.

    When the target's own link for the file is known (recorded at upload), the
    whole source link is replaced with it. That is the only correct answer for
    a signed source link, whose signature, expiry and workspace all belong to
    the source. Otherwise the hash and workspace segments are swapped in place.

    Args:
        text: Text content containing markdown image links
        attachment_mapping: source_hash -> target_hash
        target_workspace_hash: Target workspace (team) hash, if known
        target_urls: target_hash -> the target's link for that file

    Returns:
        Text with attachment links pointing at the target
    """
    if not text or not isinstance(text, str) or not attachment_mapping:
        return text

    def replace_url(match: "re.Match[str]") -> str:
        url = match.group(0)
        source_hash = attachment_hash_from_url(url)
        if not source_hash:
            return url
        new_hash = _lookup(attachment_mapping, source_hash)
        if not new_hash:
            logger.debug("Attachment hash %s... not found in mapping", source_hash[:8])
            return url
        if target_urls and target_urls.get(new_hash):
            return target_urls[new_hash]
        return _rewrite_url_in_place(url, new_hash, target_workspace_hash)

    text = _URL_IN_TEXT_RE.sub(replace_url, text)

    # Relative paths, outside any full URL.
    def replace_hash(match: "re.Match[str]") -> str:
        new_hash = _lookup(attachment_mapping, match.group(2).lower())
        if new_hash:
            return f"{match.group(1)}{new_hash}{match.group(3)}"
        return match.group(0)

    return re.sub(r"(/attachment/)([a-f0-9]{32,64})(/)", replace_hash, text, flags=re.IGNORECASE)
