"""
Create attachments in target Qase workspace.
"""
import logging
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
from urllib.parse import urlparse
from typing import Dict, Set, List, Any, Optional, Tuple
from tqdm import tqdm
from qase.api_client_v1.api.attachments_api import AttachmentsApi
from qase.api_client_v1.api.cases_api import CasesApi
from qase.api_client_v1.exceptions import ApiException
from qase_service import QaseService
from migration.utils import MigrationMappings, MigrationStats, retry_with_backoff, extract_entities_from_response, to_dict
from migration.extract.attachments import extract_all_attachment_hashes
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
QASE_API_MAX_CALLS_PER_MINUTE = 1000
DOWNLOAD_PARALLEL_WORKERS = min(24, max(8, (os.cpu_count() or 4) * 2))
MAX_PARALLEL_TARGET_QUEUES = 4
_BULK_UPLOAD_RETRIES = 12
_ATTACHMENT_GET_MAX_RETRIES = 12
_ATTACHMENT_GET_BASE_DELAY = 1.5


def _try_download_markdown_url(
    markdown_url: str,
    attachment_hash: str,
    source_service: QaseService,
    filename_hint: Optional[str] = None,
) -> Tuple[Optional[bytes], Optional[str]]:
    """HTTP-only download from a known attachment URL (safe for parallel use)."""
    filename = filename_hint
    if not filename:
        parsed = urlparse(markdown_url)
        url_filename = os.path.basename(parsed.path)
        if url_filename and url_filename != "/":
            filename = url_filename
    try:
        url_response = qase_http.get(markdown_url, timeout=60)
        if url_response.status_code == 200:
            content = url_response.content
            if not filename:
                parsed = urlparse(markdown_url)
                filename = os.path.basename(parsed.path) or f"attachment_{attachment_hash[:8]}.bin"
            return content, filename
        if url_response.status_code == 403:
            headers = {"Token": source_service.api_token}
            url_response = qase_http.get(markdown_url, headers=headers, timeout=60)
            if url_response.status_code == 200:
                if not filename:
                    parsed = urlparse(markdown_url)
                    filename = os.path.basename(parsed.path) or f"attachment_{attachment_hash[:8]}.bin"
                return url_response.content, filename
    except Exception:
        pass
    return None, None


def _parse_hashes_from_upload_json(data: Any, expected_n: int) -> List[Optional[str]]:
    """Normalize attachment upload JSON into a list of target hashes."""
    if not isinstance(data, dict) or not data.get("status"):
        return [None] * expected_n
    result = data.get("result")
    if result is None:
        return [None] * expected_n
    if isinstance(result, list):
        out = []
        for item in result:
            if isinstance(item, dict):
                out.append(item.get("hash"))
            elif hasattr(item, "hash"):
                out.append(getattr(item, "hash", None))
            else:
                out.append(None)
        return out
    if isinstance(result, dict):
        if "hash" in result:
            return [result.get("hash")]
        entities = result.get("entities")
        if isinstance(entities, list):
            return [e.get("hash") if isinstance(e, dict) else None for e in entities]
    return [None] * expected_n


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
                m = re.search(r"/public/team/([a-f0-9]{32,64})/", u, re.IGNORECASE)
                if m:
                    mappings.target_workspace_hash = m.group(1)
        elif isinstance(obj, list):
            for x in obj:
                scan(x)

    scan(data)


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
    multipart = [
        ("file[]", (fn or "file.bin", content, "application/octet-stream"))
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
                                        url = att_item['url']
                                        match = re.search(r'/attachment/([a-f0-9]{32,64})/', url, re.IGNORECASE)
                                        if match:
                                            att_hash = match.group(1)
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


def download_attachment(
    attachments_api_source: AttachmentsApi,
    attachment_hash: str,
    markdown_url: Optional[str],
    source_service: QaseService,
    *,
    source_api_limiter: Optional[QaseApiRateLimiter] = None,
    client_lock: Optional[threading.Lock] = None,
) -> Tuple[Optional[bytes], Optional[str]]:
    """
    Download attachment content and extract filename.

    Returns:
        Tuple of (file_content, filename) or (None, None) if download fails
    """
    file_content = None
    filename = None

    if markdown_url:
        parsed = urlparse(markdown_url)
        url_filename = os.path.basename(parsed.path)
        if url_filename and url_filename != "/":
            filename = url_filename

    download_response = None
    for attempt in range(_ATTACHMENT_GET_MAX_RETRIES):
        try:

            def _do_get():
                if source_api_limiter:
                    source_api_limiter.acquire(1)
                return attachments_api_source.get_attachment(hash=attachment_hash)

            if client_lock is not None:
                with client_lock:
                    download_response = _do_get()
            else:
                download_response = _do_get()
            break
        except ApiException as e:
            if e.status == 404:
                download_response = None
                break
            if e.status == 429 or (e.status is not None and e.status >= 500):
                if attempt < _ATTACHMENT_GET_MAX_RETRIES - 1:
                    delay = exponential_backoff_delay(attempt, base_delay=_ATTACHMENT_GET_BASE_DELAY)
                    logger.info(
                        "get_attachment %s HTTP %s, backoff %.1fs (%s/%s)",
                        attachment_hash[:12],
                        e.status,
                        delay,
                        attempt + 1,
                        _ATTACHMENT_GET_MAX_RETRIES,
                    )
                    time.sleep(delay)
                    continue
            logger.debug("get_attachment failed for %s: %s", attachment_hash[:12], e)
            download_response = None
            break
        except Exception as api_error:
            if attempt < _ATTACHMENT_GET_MAX_RETRIES - 1:
                delay = exponential_backoff_delay(attempt, base_delay=_ATTACHMENT_GET_BASE_DELAY)
                time.sleep(delay)
                continue
            logger.debug("get_attachment error for %s: %s", attachment_hash[:12], api_error)
            download_response = None
            break

    if download_response and hasattr(download_response, 'status') and download_response.status:
        if hasattr(download_response, 'result') and download_response.result:
            attachment_obj = download_response.result
            
            # Try to get file content from 'file' field
            if hasattr(attachment_obj, 'file') and attachment_obj.file is not None:
                if isinstance(attachment_obj.file, bytes):
                    file_content = attachment_obj.file
                elif isinstance(attachment_obj.file, str):
                    try:
                        file_content = base64.b64decode(attachment_obj.file)
                    except Exception:
                        pass
            
            if not filename:
                if hasattr(attachment_obj, 'filename') and attachment_obj.filename:
                    filename = attachment_obj.filename
                elif hasattr(attachment_obj, 'full_path') and attachment_obj.full_path:
                    filename = os.path.basename(attachment_obj.full_path)
                elif hasattr(attachment_obj, 'extension') and attachment_obj.extension:
                    filename = f"attachment_{attachment_hash[:8]}.{attachment_obj.extension}"
            
            if not file_content:
                url_to_download = None
                if hasattr(attachment_obj, 'url'):
                    url_value = getattr(attachment_obj, 'url', None)
                    if url_value:
                        url_to_download = url_value
                if not url_to_download and hasattr(attachment_obj, 'full_path'):
                    full_path_value = getattr(attachment_obj, 'full_path', None)
                    if full_path_value:
                        url_to_download = full_path_value
                
                if url_to_download:
                    try:
                        url_response = qase_http.get(url_to_download, timeout=60)
                        if url_response.status_code == 200:
                            file_content = url_response.content
                        elif url_response.status_code == 403:
                            headers = {'Token': source_service.api_token}
                            url_response = qase_http.get(url_to_download, headers=headers, timeout=60)
                            if url_response.status_code == 200:
                                file_content = url_response.content
                    except Exception as e:
                        pass
    
    if not file_content and markdown_url:
        try:
            url_response = qase_http.get(markdown_url, timeout=60)
            if url_response.status_code == 200:
                file_content = url_response.content
                if not filename:
                    parsed = urlparse(markdown_url)
                    filename = os.path.basename(parsed.path) or f"attachment_{attachment_hash[:8]}.bin"
            elif url_response.status_code == 403:
                headers = {'Token': source_service.api_token}
                url_response = qase_http.get(markdown_url, headers=headers, timeout=60)
                if url_response.status_code == 200:
                    file_content = url_response.content
                    if not filename:
                        parsed = urlparse(markdown_url)
                        filename = os.path.basename(parsed.path) or f"attachment_{attachment_hash[:8]}.bin"
        except Exception as e:
            pass
    
    if not filename:
        filename = f"attachment_{attachment_hash[:8]}.bin"
    
    return file_content, filename


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
    """Try public URL first (parallel-friendly), then SDK get_attachment (throttled, short lock)."""
    if markdown_url:
        content, filename = _try_download_markdown_url(
            markdown_url, attachment_hash, source_service
        )
        if content and filename:
            return attachment_hash, content, filename
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
    stats.add_entity('attachments', total_unique_attachments, migrated_count + reused)
    stats.add_reused('attachments', reused)
    
    return mappings.attachments
