"""
Create attachments in target Qase workspace.
"""
import logging
import mimetypes
import os
import re
import sys
import time
import base64
import threading
import requests

from migration import qase_http
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from typing import Dict, Set, List, Any, Optional, Tuple
from tqdm import tqdm
from qase.api_client_v1.api.attachments_api import AttachmentsApi
from qase.api_client_v1.api.cases_api import CasesApi
from qase_service import QaseService
from migration.utils import MigrationMappings, MigrationStats, retry_with_backoff, extract_entities_from_response, to_dict
from migration.extract.attachments import extract_all_attachment_hashes
from migration.transform.attachments import attachment_hash_from_url
from migration.qase_rate_limit import QaseApiRateLimiter, exponential_backoff_delay

logger = logging.getLogger(__name__)

# Qase bulk attachment upload limits (POST /v1/attachment/{projectCode}, multipart file[]):
# up to 20 files, 128 MB per request, 32 MB per file.
#
# Those limits are DECIMAL megabytes. Budgeting in MiB (128 * 1024 * 1024 =
# 134.2 MB) puts every full batch ~5% over the cap, which the server rejects with
# 413 — and since batch composition depends on download timing, a rerun loses a
# different set of files each time. Budget in decimal bytes, with headroom for
# the multipart envelope, and keep the per-file cap under 32 MB too.
MAX_FILES_PER_UPLOAD = 20
MAX_BYTES_PER_UPLOAD = 100_000_000
MAX_BYTES_PER_FILE = 30_000_000
# Bytes a single multipart part costs on top of its content (boundary,
# Content-Disposition, Content-Type, CRLFs). Filename length is added per file.
MULTIPART_PART_OVERHEAD = 256
# Qase cloud: ~1000 API req/min per account. Source and target each get their own rolling window.
# Qase cloud advertises 600 requests/minute plus a 100/10s burst on non
# Enterprise plans, and 1000/minute on Enterprise. Pacing above the real ceiling
# just converts throughput into 429s and backoff, so default to the lower figure
# and let options.api_calls_per_minute raise it.
QASE_API_MAX_CALLS_PER_MINUTE = 600


def set_api_calls_per_minute(value: Optional[int]) -> None:
    """Override the attachment pacing from options.api_calls_per_minute."""
    global QASE_API_MAX_CALLS_PER_MINUTE
    try:
        if value and int(value) > 0:
            QASE_API_MAX_CALLS_PER_MINUTE = int(value)
    except (TypeError, ValueError):
        pass
DOWNLOAD_PARALLEL_WORKERS = min(24, max(8, (os.cpu_count() or 4) * 2))
MAX_PARALLEL_TARGET_QUEUES = 4
_BULK_UPLOAD_RETRIES = 12
_ATTACHMENT_GET_MAX_RETRIES = 12
_ATTACHMENT_GET_BASE_DELAY = 1.5


_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
# A signed link this close to its expiry is treated as expired, so the download
# does not start on a link that lapses mid-transfer.
_SIGNED_URL_MARGIN_SECONDS = 120


def _source_qase_hosts(source_service: QaseService) -> Set[str]:
    """Hosts that belong to the source workspace's Qase cluster."""
    host = (getattr(source_service, "host", "") or "").strip().lower()
    if not host:
        return set()
    delimiter = "." if host == "qase.io" else "-"
    return {host} | {f"{prefix}{delimiter}{host}" for prefix in ("api", "app", "files")}


def _is_qase_host(hostname: Optional[str], source_service: QaseService) -> bool:
    hn = (hostname or "").lower()
    return hn in _source_qase_hosts(source_service) or hn == "qase.io" or hn.endswith(".qase.io")


def signed_url_expired(url: Optional[str], now: Optional[float] = None) -> bool:
    """True when a signed Qase link's exp has passed (or is about to)."""
    if not url:
        return False
    try:
        exp = parse_qs(urlparse(url).query).get("exp")
        if not exp:
            return False
        value = int(exp[0])
    except (TypeError, ValueError):
        return False
    if value > 10**12:  # milliseconds
        value //= 1000
    return value <= (now if now is not None else time.time()) + _SIGNED_URL_MARGIN_SECONDS


def _get_following_redirects(
    url: str, headers: Optional[Dict[str, str]], timeout: int
) -> Tuple[Optional[requests.Response], bool]:
    """
    GET, following redirects by hand so headers are sent to the first host only.

    requests strips Authorization on a cross-host redirect but not a custom
    Token header, so letting it follow a signed link's redirect to storage would
    hand the API token to the storage provider.
    """
    redirected = False
    for _ in range(_MAX_REDIRECTS + 1):
        r = qase_http.get(url, headers=headers, timeout=timeout, allow_redirects=False)
        location = r.headers.get("Location")
        if r.status_code in _REDIRECT_STATUSES and location:
            url = urljoin(url, location)
            headers = None
            redirected = True
            continue
        return r, redirected
    return None, redirected


def fetch_file_url(
    url: str, source_service: QaseService, timeout: int = 60
) -> Tuple[Optional[bytes], Optional[str]]:
    """
    Download a stored file from a link. Returns (content, None) or (None, reason).

    The API token is sent only to the source's own Qase hosts, only after an
    anonymous request was refused, and never along a redirect.
    """
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None, f"the link is not a web address: {url!r}"
    try:
        r, redirected = _get_following_redirects(url, None, timeout)
        if (
            r is not None
            and r.status_code in (401, 403)
            and parsed.hostname.lower() in _source_qase_hosts(source_service)
        ):
            r, redirected = _get_following_redirects(
                url, {"Token": source_service.api_token}, timeout
            )
    except Exception as e:
        return None, f"downloading the file failed: {e}"
    if r is None:
        return None, "the link redirected too many times"
    if r.status_code != 200:
        return None, f"downloading the file returned HTTP {r.status_code}"
    content_type = (r.headers.get("Content-Type") or "").lower()
    if redirected and "text/html" in content_type and _is_qase_host(urlparse(r.url).hostname, source_service):
        # An old public link in a workspace with private attachments ends on
        # Qase's sign-in page. Uploading that page would be worse than nothing.
        return None, "the link led to a Qase sign-in page instead of the file"
    return r.content, None


def _filename_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    # The path is percent-encoded, so without decoding a file called
    # "my screenshot.png" arrives as "my%20screenshot.png".
    name = unquote(os.path.basename(urlparse(url).path))
    return name if name and name != "/" else None


def _upload_result_items(data: Any) -> Optional[List[Any]]:
    if not isinstance(data, dict) or not data.get("status"):
        return None
    result = data.get("result")
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        if "hash" in result:
            return [result]
        entities = result.get("entities")
        if isinstance(entities, list):
            return entities
    return None


def _parse_hashes_from_upload_json(data: Any, expected_n: int) -> List[Optional[str]]:
    """Normalize attachment upload JSON into a list of target hashes."""
    items = _upload_result_items(data)
    if items is None:
        return [None] * expected_n
    out = []
    for item in items:
        if isinstance(item, dict):
            out.append(item.get("hash"))
        else:
            out.append(getattr(item, "hash", None))
    return out


def _record_target_urls(mappings: MigrationMappings, data: Any) -> None:
    """
    Keep the target's own link for each uploaded file.

    Text that linked a source file is rewritten to this link. For a source with
    private attachments the source link is signed for the source workspace, so
    swapping the hash inside it cannot produce a working target link.
    """
    urls = getattr(mappings, "attachment_urls", None)
    if urls is None:
        return
    for item in _upload_result_items(data) or []:
        if isinstance(item, dict) and item.get("hash") and item.get("url"):
            urls[item["hash"]] = item["url"]


def _maybe_capture_workspace_hash_from_upload_result(mappings: MigrationMappings, data: Any) -> None:
    if mappings.target_workspace_hash:
        return

    def scan(obj: Any) -> None:
        if mappings.target_workspace_hash:
            return
        if isinstance(obj, dict):
            for v in obj.values():
                scan(v)
            u = obj.get("url") or obj.get("full_path")
            if isinstance(u, str):
                m = re.search(
                    r"/public/team/([a-f0-9]{32,64})/|/attachments/api/([a-f0-9]{32,64})/",
                    u,
                    re.IGNORECASE,
                )
                if m:
                    mappings.target_workspace_hash = m.group(1) or m.group(2)
        elif isinstance(obj, list):
            for x in obj:
                scan(x)

    scan(data)


def _media_type_for(filename: Optional[str]) -> str:
    """Media type from the file's name, falling back to a generic binary type."""
    guessed, _ = mimetypes.guess_type(filename or "")
    return guessed or "application/octet-stream"


def _upload_files_http(
    target_service: QaseService,
    project_code: str,
    named_files: List[Tuple[str, bytes]],
    mappings: MigrationMappings,
    target_api_limiter: Optional[QaseApiRateLimiter] = None,
) -> List[Optional[str]]:
    """
    POST multipart file[] to /v1/attachment/{code}. Returns target hashes aligned with named_files.
    """
    n = len(named_files)
    if n == 0:
        return []
    base = target_service.client.configuration.host.rstrip("/")
    url = f"{base}/attachment/{project_code}"
    token = target_service.api_token
    headers = {"Token": token, "accept": "application/json"}
    # Every file used to be uploaded as application/octet-stream, so Qase could
    # not tell an image from a zip and showed no preview. Derive the type from
    # the filename instead.
    multipart = [
        ("file[]", (fn or "file.bin", content, _media_type_for(fn)))
        for fn, content in named_files
    ]
    last_exc: Optional[Exception] = None
    for attempt in range(_BULK_UPLOAD_RETRIES):
        try:
            if target_api_limiter:
                target_api_limiter.acquire(1)
            r = qase_http.post(url, headers=headers, files=multipart, timeout=300)
            if r.status_code == 507:
                logger.error("Attachment upload failed: insufficient storage (507) for project %s", project_code)
                return [None] * n
            if r.status_code == 413:
                # The request is too large. Deterministic: retrying it unchanged
                # can only fail again (and previously burned ~11 minutes doing
                # so, then dropped all n files). Halve the batch instead.
                if n == 1:
                    name, content = named_files[0]
                    logger.error(
                        "Attachment rejected as too large (413) and skipped: %r (%s bytes, project %s). "
                        "The Qase API accepts at most 32 MB per file.",
                        name,
                        len(content),
                        project_code,
                    )
                    return [None]
                mid = n // 2
                logger.warning(
                    "Attachment upload rejected as too large (413) for %s files (%s bytes); "
                    "splitting into %s + %s and retrying",
                    n,
                    sum(len(c) for _, c in named_files),
                    mid,
                    n - mid,
                )
                left = _upload_files_http(
                    target_service, project_code, named_files[:mid], mappings, target_api_limiter
                )
                right = _upload_files_http(
                    target_service, project_code, named_files[mid:], mappings, target_api_limiter
                )
                return left + right
            if 400 <= r.status_code < 500 and r.status_code not in (408, 429):
                # Deterministic client error: fast-fail rather than sleeping
                # through 12 backoff rounds for a result that cannot change.
                # 408 and 429 are transient and fall through to the retry below.
                logger.error(
                    "Attachment upload failed with HTTP %s (non-retryable) for project %s: %s",
                    r.status_code,
                    project_code,
                    (r.text or "")[:300],
                )
                return [None] * n
            if r.status_code == 429 or r.status_code >= 500:
                delay = exponential_backoff_delay(attempt, base_delay=_ATTACHMENT_GET_BASE_DELAY)
                logger.info(
                    "Attachment upload HTTP %s, retry in %.1fs (%s/%s)",
                    r.status_code,
                    delay,
                    attempt + 1,
                    _BULK_UPLOAD_RETRIES,
                )
                time.sleep(delay)
                continue
            r.raise_for_status()
            payload = r.json()
            _maybe_capture_workspace_hash_from_upload_result(mappings, payload)
            _record_target_urls(mappings, payload)
            hashes = _parse_hashes_from_upload_json(payload, n)
            if len(hashes) < n:
                hashes.extend([None] * (n - len(hashes)))
            elif len(hashes) > n:
                hashes = hashes[:n]
            if all(h is None for h in hashes) and n > 0:
                logger.warning(
                    "Bulk attachment upload returned no hashes (project=%s body_keys=%s)",
                    project_code,
                    list(payload.keys()) if isinstance(payload, dict) else type(payload),
                )
            return hashes
        except Exception as e:
            last_exc = e
            delay = exponential_backoff_delay(attempt, base_delay=_ATTACHMENT_GET_BASE_DELAY)
            logger.info("Attachment upload error: %s; retry in %.1fs", e, delay)
            time.sleep(delay)
    if last_exc:
        logger.error("Attachment upload failed after retries: %s", last_exc)
    return [None] * n


def _wire_cost(filename: str, content: bytes) -> int:
    """Bytes this file costs in the multipart request, envelope included."""
    return len(content) + len(filename or "") + MULTIPART_PART_OVERHEAD


def _take_upload_batch(ready: deque) -> List[Tuple[str, str, bytes]]:
    """
    Greedy batch: at most MAX_FILES_PER_UPLOAD files and MAX_BYTES_PER_UPLOAD
    bytes on the wire; a file over MAX_BYTES_PER_FILE uploads alone.

    The budget counts the multipart envelope, not just file contents, so a full
    batch stays inside the server's request cap instead of landing just over it.
    """
    if not ready:
        return []
    first = ready[0]
    _, first_name, first_bytes = first
    if len(first_bytes) > MAX_BYTES_PER_FILE:
        ready.popleft()
        return [first]
    batch: List[Tuple[str, str, bytes]] = []
    total = 0
    while ready:
        h, fn, c = ready[0]
        if len(c) > MAX_BYTES_PER_FILE:
            break
        if len(batch) >= MAX_FILES_PER_UPLOAD:
            break
        if total + _wire_cost(fn, c) > MAX_BYTES_PER_UPLOAD and batch:
            break
        batch.append(ready.popleft())
        total += _wire_cost(fn, c)
    return batch


def _log_source_attachment_library_totals(
    source_service: QaseService,
    projects: List[Dict[str, Any]],
    source_api_limiter: Optional[QaseApiRateLimiter] = None,
) -> None:
    """Log result.total from GET /v1/attachment/{code} (no bulk download API in Qase)."""
    base = source_service.client.configuration.host.rstrip("/")
    headers = {"Token": source_service.api_token, "accept": "application/json"}
    for project in projects:
        code = project.get("source_code")
        if not code:
            continue
        try:
            if source_api_limiter:
                source_api_limiter.acquire(1)
            r = qase_http.get(
                f"{base}/attachment/{code}",
                headers=headers,
                params={"limit": 1, "offset": 0},
                timeout=30,
            )
            if not r.ok:
                continue
            data = r.json()
            if not isinstance(data, dict):
                continue
            res = data.get("result")
            if isinstance(res, dict) and res.get("total") is not None:
                logger.info(
                    "Source project %s attachment library: %s file(s) (API total)",
                    code,
                    res.get("total"),
                )
        except Exception:
            pass


def check_existing_attachments_in_target(
    target_service: QaseService,
    projects: List[Dict[str, Any]],
    target_api_limiter: Optional[QaseApiRateLimiter] = None,
) -> Set[str]:
    """
    Check for existing attachments in target workspace.
    
    Returns:
        Set of existing attachment hashes
    """
    attachments_api_target = AttachmentsApi(target_service.client)
    cases_api_target = CasesApi(target_service.client)
    existing_attachments = set()
    
    for project in projects:
        project_code_target = project['target_code']
        try:
            offset = 0
            limit = 100
            
            while True:
                try:

                    def _get_cases_page():
                        if target_api_limiter:
                            target_api_limiter.acquire(1)
                        return cases_api_target.get_cases(
                            code=project_code_target,
                            limit=limit,
                            offset=offset,
                        )

                    cases_response = retry_with_backoff(
                        _get_cases_page,
                        max_retries=8,
                        base_delay=_ATTACHMENT_GET_BASE_DELAY,
                    )
                    
                    cases_entities = extract_entities_from_response(cases_response)
                    if not cases_entities:
                        break
                    
                    for case in cases_entities:
                        case_dict = to_dict(case)
                        
                        if case_dict.get('attachments'):
                            for att_item in case_dict['attachments']:
                                att_hash = None
                                if isinstance(att_item, str):
                                    att_hash = att_item
                                elif isinstance(att_item, dict):
                                    if 'hash' in att_item:
                                        att_hash = att_item['hash']
                                    elif 'url' in att_item:
                                        att_hash = attachment_hash_from_url(att_item['url'])
                                if att_hash:
                                    existing_attachments.add(att_hash.lower())
                    
                    if len(cases_entities) < limit:
                        break
                    offset += limit
                except Exception as e:
                    break
        except Exception as e:
            pass
    
    return existing_attachments


def _get_attachment_record(
    source_service: QaseService,
    attachment_hash: str,
    source_api_limiter: Optional[QaseApiRateLimiter] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    GET /v1/attachment/{hash}: the file's record with a fresh link.

    Raw HTTP rather than the SDK, so a record shape the SDK does not model
    cannot cost the file. Transient failures are retried by qase_http.
    """
    base = source_service.client.configuration.host.rstrip("/")
    headers = {"Token": source_service.api_token, "accept": "application/json"}
    try:
        if source_api_limiter:
            source_api_limiter.acquire(1)
        r = qase_http.get(f"{base}/attachment/{attachment_hash}", headers=headers, timeout=60)
    except Exception as e:
        return None, f"looking up the attachment failed: {e}"
    if r.status_code == 404:
        return None, (
            "the source API has no attachment with this hash (404), so the "
            "file is referenced but no longer stored"
        )
    if r.status_code != 200:
        return None, f"looking up the attachment returned HTTP {r.status_code}"
    try:
        data = r.json()
    except ValueError:
        return None, "looking up the attachment returned something other than JSON"
    record = data.get("result") if isinstance(data, dict) else None
    if not isinstance(record, dict):
        return None, "looking up the attachment returned no record"
    return record, None


def download_attachment(
    attachments_api_source: Optional[AttachmentsApi],
    attachment_hash: str,
    markdown_url: Optional[str],
    source_service: QaseService,
    *,
    source_api_limiter: Optional[QaseApiRateLimiter] = None,
    client_lock: Optional[threading.Lock] = None,
) -> Tuple[Optional[bytes], Optional[str]]:
    """
    Download attachment content and extract filename.

    The link found while collecting attachments is tried first. A signed link
    lasts an hour, so on a long run it may have expired; then, or if it fails
    for any other reason, a fresh link is fetched from the attachment's record.

    ``attachments_api_source`` and ``client_lock`` are no longer used and are
    kept so existing callers do not break.

    Returns:
        Tuple of (file_content, filename) or (None, None) if download fails
    """
    filename = _filename_from_url(markdown_url)
    reasons: List[str] = []

    if markdown_url and not signed_url_expired(markdown_url):
        content, reason = fetch_file_url(markdown_url, source_service)
        if content is not None:
            return content, filename or f"attachment_{attachment_hash[:8]}.bin"
        reasons.append(f"from the link in the source: {reason}")

    record, reason = _get_attachment_record(source_service, attachment_hash, source_api_limiter)
    content = None
    if record is None:
        reasons.append(reason)
    else:
        if not filename:
            filename = (
                record.get("filename")
                or _filename_from_url(record.get("full_path") or record.get("url"))
                or (f"attachment_{attachment_hash[:8]}.{record['extension']}" if record.get("extension") else None)
            )
        raw = record.get("file")
        if isinstance(raw, str) and raw:
            try:
                content = base64.b64decode(raw)
            except Exception as e:
                reasons.append(f"the file content could not be decoded: {e}")
        fresh_url = record.get("url") or record.get("full_path")
        if content is None and fresh_url:
            content, reason = fetch_file_url(fresh_url, source_service)
            if content is None:
                reasons.append(f"from the attachment record: {reason}")
        elif content is None:
            reasons.append("the attachment record has no file and no link")

    if content is None:
        # The one place that knows why, so the operator is told rather than left
        # with a count of missing files.
        logger.warning(
            "Attachment %s was not migrated: %s",
            attachment_hash[:12] if isinstance(attachment_hash, str) else attachment_hash,
            "; ".join(r for r in reasons if r) or "the API returned no content and no usable file URL",
        )
        return None, None

    return content, filename or f"attachment_{attachment_hash[:8]}.bin"


def upload_attachment(
    target_service: QaseService,
    project_code_target: str,
    filename: str,
    file_content: bytes,
    mappings: MigrationMappings,
    target_api_limiter: Optional[QaseApiRateLimiter] = None,
) -> Optional[str]:
    """
    Upload a single attachment via HTTP multipart (same endpoint as bulk).

    Returns:
        Target attachment hash, or None if upload fails
    """
    hashes = _upload_files_http(
        target_service,
        project_code_target,
        [(filename, file_content)],
        mappings,
        target_api_limiter=target_api_limiter,
    )
    return hashes[0] if hashes else None


def _download_single_for_migration(
    attachment_hash: str,
    markdown_url: Optional[str],
    attachments_api_source: AttachmentsApi,
    source_service: QaseService,
    sdk_lock: threading.Lock,
    source_api_limiter: Optional[QaseApiRateLimiter] = None,
) -> Tuple[str, Optional[bytes], Optional[str]]:
    """Download one file: its known link first, then a fresh link from its record."""
    content, filename = download_attachment(
        attachments_api_source,
        attachment_hash,
        markdown_url,
        source_service,
        source_api_limiter=source_api_limiter,
        client_lock=sdk_lock,
    )
    return attachment_hash, content, filename


def _download_batch_parallel(
    batch: List[Tuple[str, Optional[str]]],
    attachments_api_source: AttachmentsApi,
    source_service: QaseService,
    sdk_lock: threading.Lock,
    source_api_limiter: Optional[QaseApiRateLimiter] = None,
) -> List[Tuple[str, str, bytes]]:
    if not batch:
        return []
    out: List[Tuple[str, str, bytes]] = []
    if len(batch) == 1:
        h, url = batch[0]
        _, content, filename = _download_single_for_migration(
            h, url, attachments_api_source, source_service, sdk_lock, source_api_limiter
        )
        if content and filename:
            out.append((h, filename, content))
        return out
    workers = min(DOWNLOAD_PARALLEL_WORKERS, len(batch))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(
                _download_single_for_migration,
                h,
                url,
                attachments_api_source,
                source_service,
                sdk_lock,
                source_api_limiter,
            ): h
            for h, url in batch
        }
        for fut in as_completed(future_map):
            src_hash = future_map[fut]
            try:
                _, content, filename = fut.result()
            except Exception as e:
                logger.error("Download failed for attachment %s: %s", src_hash, e)
                continue
            if content and filename:
                out.append((src_hash, filename, content))
            # No else: download_attachment already logged the specific reason.
    return out


def migrate_attachments_workspace(
    source_service: QaseService,
    target_service: QaseService,
    projects: List[Dict[str, Any]],
    mappings: MigrationMappings,
    stats: MigrationStats
) -> Dict[str, Dict[str, str]]:
    """
    Migrate all attachments at workspace level by collecting from all projects.
    Attachments are project-scoped but we collect them all upfront.
    Deduplicates attachments across projects (same hash = same file).
    
    Args:
        projects: List of project mappings with 'source_code' and 'target_code'
    
    Returns:
        Dictionary mapping project_code -> {source_hash -> target_hash}
    """
    attachments_api_source = AttachmentsApi(source_service.client)
    source_api_limiter = QaseApiRateLimiter(QASE_API_MAX_CALLS_PER_MINUTE, 60.0)
    target_api_limiter = QaseApiRateLimiter(QASE_API_MAX_CALLS_PER_MINUTE, 60.0)

    existing_attachments = check_existing_attachments_in_target(
        target_service, projects, target_api_limiter=target_api_limiter
    )

    all_project_attachments = extract_all_attachment_hashes(source_service, projects)

    global_attachment_set = set()
    attachment_urls = {}
    all_attachments = {}

    for project_code_source, (hashes, urls) in all_project_attachments.items():
        all_attachments[project_code_source] = hashes
        global_attachment_set.update(hashes)
        attachment_urls.update(urls)

    total_unique_attachments = len(global_attachment_set)

    global_attachment_mapping = {}
    migrated_count = 0
    skipped_existing_count = 0
    skipped_already_migrated_count = 0

    _log_source_attachment_library_totals(
        source_service, projects, source_api_limiter=source_api_limiter
    )

    upload_queues: Dict[str, deque] = defaultdict(deque)
    work_total = 0

    for attachment_hash_raw in global_attachment_set:
        attachment_hash = str(attachment_hash_raw).lower()

        already_mapped = False
        for project_code in mappings.attachments:
            if attachment_hash in mappings.attachments[project_code]:
                global_attachment_mapping[attachment_hash] = mappings.attachments[project_code][attachment_hash]
                skipped_already_migrated_count += 1
                already_mapped = True
                break
            if attachment_hash_raw in mappings.attachments[project_code]:
                global_attachment_mapping[attachment_hash] = mappings.attachments[project_code][attachment_hash_raw]
                skipped_already_migrated_count += 1
                already_mapped = True
                break

        if already_mapped:
            continue

        if attachment_hash in existing_attachments:
            global_attachment_mapping[attachment_hash] = attachment_hash
            skipped_existing_count += 1
            continue

        source_project = None
        target_project = None
        for project in projects:
            project_code_source = project["source_code"]
            if project_code_source in all_attachments:
                if attachment_hash in all_attachments[project_code_source]:
                    source_project = project_code_source
                    target_project = project["target_code"]
                    break

        if not source_project or not target_project:
            continue

        markdown_url = attachment_urls.get(attachment_hash)
        upload_queues[target_project].append((attachment_hash, markdown_url))
        work_total += 1

    sdk_lock = threading.Lock()
    mapping_lock = threading.Lock()
    pbar_lock = threading.Lock()
    pbar = tqdm(
        total=work_total,
        desc="Migrating attachments",
        unit="file",
        file=sys.stderr,
        dynamic_ncols=True,
    )

    def _pbar_update(n: int = 1) -> None:
        with pbar_lock:
            pbar.update(n)

    def drain_target_queue(target_code: str) -> int:
        local_migrated = 0
        q = upload_queues[target_code]
        ready: deque = deque()
        while q or ready:
            while len(ready) < MAX_FILES_PER_UPLOAD * 3 and q:
                download_batch: List[Tuple[str, Optional[str]]] = []
                while len(download_batch) < MAX_FILES_PER_UPLOAD and q:
                    download_batch.append(q.popleft())
                downloaded = _download_batch_parallel(
                    download_batch,
                    attachments_api_source,
                    source_service,
                    sdk_lock,
                    source_api_limiter,
                )
                ok_hashes = {t[0] for t in downloaded}
                for h, _url in download_batch:
                    if h not in ok_hashes:
                        _pbar_update(1)
                for triple in downloaded:
                    ready.append(triple)
            if not ready:
                break
            upload_batch = _take_upload_batch(ready)
            if not upload_batch:
                continue
            named_files = [(fn, content) for _h, fn, content in upload_batch]
            target_hashes = _upload_files_http(
                target_service,
                target_code,
                named_files,
                mappings,
                target_api_limiter=target_api_limiter,
            )
            if (
                len(upload_batch) > 1
                and target_hashes
                and all(th is None for th in target_hashes)
            ):
                for src_hash, fn, content in upload_batch:
                    th = _upload_files_http(
                        target_service,
                        target_code,
                        [(fn, content)],
                        mappings,
                        target_api_limiter=target_api_limiter,
                    )
                    mapped = th[0] if th else None
                    if mapped:
                        with mapping_lock:
                            global_attachment_mapping[src_hash] = mapped
                        local_migrated += 1
                    _pbar_update(1)
                continue
            for i, (src_hash, _fn, _c) in enumerate(upload_batch):
                th = target_hashes[i] if i < len(target_hashes) else None
                if th:
                    with mapping_lock:
                        global_attachment_mapping[src_hash] = th
                    local_migrated += 1
                _pbar_update(1)
        return local_migrated

    try:
        targets_with_work = [tc for tc in sorted(upload_queues.keys()) if upload_queues[tc]]
        if targets_with_work:
            n_workers = min(MAX_PARALLEL_TARGET_QUEUES, len(targets_with_work))
            with ThreadPoolExecutor(max_workers=n_workers) as upload_pool:
                futures = [
                    upload_pool.submit(drain_target_queue, tc) for tc in targets_with_work
                ]
                for fut in as_completed(futures):
                    migrated_count += fut.result()
    finally:
        pbar.close()
    
    for project in projects:
        project_code_source = project['source_code']
        
        if project_code_source not in mappings.attachments:
            mappings.attachments[project_code_source] = {}
        
        if project_code_source in all_attachments:
            for attachment_hash in all_attachments[project_code_source]:
                normalized_hash = attachment_hash.lower()
                target_hash = global_attachment_mapping.get(normalized_hash) or global_attachment_mapping.get(attachment_hash)
                if target_hash:
                    mappings.attachments[project_code_source][normalized_hash] = target_hash
                    if normalized_hash != attachment_hash:
                        mappings.attachments[project_code_source][attachment_hash] = target_hash
    
    # Attachments already uploaded by a previous run, or already present in the
    # target, are part of the migration too; only genuinely failed ones are missing.
    reused = skipped_already_migrated_count + skipped_existing_count
    attachments_missing = total_unique_attachments - (migrated_count + reused)
    if attachments_missing > 0:
        logger.error(
            "%s of %s attachments did not migrate. The cases, results and defects "
            "that referenced them keep their text but lose the file. The warnings "
            "above name each one.",
            attachments_missing,
            total_unique_attachments,
        )

    stats.add_entity('attachments', total_unique_attachments, migrated_count + reused)
    stats.add_reused('attachments', reused)
    
    return mappings.attachments
