"""
Extract attachment references from source Qase workspace.
"""
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Set, List, Any, Tuple
from qase.api_client_v1.api.cases_api import CasesApi
from qase.api_client_v1.api.runs_api import RunsApi
from qase.api_client_v1.api.results_api import ResultsApi
from qase_service import QaseService
from migration.utils import retry_with_backoff, extract_entities_from_response, to_dict
from migration.transform.attachments import (
    extract_attachment_hashes_from_dict,
    extract_attachment_urls_from_dict
)

logger = logging.getLogger(__name__)

_ATT_URL_RE = re.compile(
    r"/attachments?/([a-f0-9]{32,64}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/",
    re.IGNORECASE,
)


def _looks_like_hash(s: str) -> bool:
    t = str(s).strip().replace("-", "")
    return len(t) >= 32 and bool(re.fullmatch(r"[a-f0-9]+", t, re.I))


def _ingest_attachment_like_items(
    items: Any,
    all_hashes: Set[str],
    url_map: Dict[str, str],
) -> None:
    """Collect hashes from API attachment lists (including automated files/screenshots shapes)."""
    if not isinstance(items, list):
        return
    for item in items:
        if isinstance(item, str):
            if _looks_like_hash(item):
                all_hashes.add(str(item).replace("-", "").lower())
            m = _ATT_URL_RE.search(item)
            if m:
                all_hashes.add(m.group(1).replace("-", "").lower())
        elif isinstance(item, dict):
            d = to_dict(item)
            for k in ("hash", "attachment_hash", "attachmentHash", "file_hash", "fileHash", "uuid"):
                v = d.get(k)
                if v is not None and _looks_like_hash(str(v)):
                    all_hashes.add(str(v).replace("-", "").lower())
            vid = d.get("id")
            if vid is not None and _looks_like_hash(str(vid)):
                all_hashes.add(str(vid).replace("-", "").lower())
            for uk in ("url", "thumb_url", "thumbnail_url", "preview_url", "src", "file_url"):
                u = d.get(uk)
                if u:
                    m = _ATT_URL_RE.search(str(u))
                    if m:
                        hx = m.group(1).replace("-", "").lower()
                        all_hashes.add(hx)
                        url_map.setdefault(hx, str(u))


def _ingest_nested_result_media(result_dict: Dict[str, Any], all_hashes: Set[str], url_map: Dict[str, str]) -> None:
    _ingest_attachment_like_items(result_dict.get("attachments"), all_hashes, url_map)
    for key in ("files", "screenshots", "media", "images", "evidence", "artifacts"):
        _ingest_attachment_like_items(result_dict.get(key), all_hashes, url_map)
    ex = result_dict.get("execution")
    if isinstance(ex, dict):
        exd = to_dict(ex)
        _ingest_attachment_like_items(exd.get("attachments"), all_hashes, url_map)
        for key in ("files", "screenshots", "media", "images"):
            _ingest_attachment_like_items(exd.get(key), all_hashes, url_map)


def extract_attachment_hashes_from_cases(
    source_service: QaseService,
    project_code: str
) -> tuple[Set[str], Dict[str, str]]:
    """
    Extract attachment hashes and URLs from all cases in a project.
    
    Returns:
        Tuple of (set of hashes, dict of hash -> URL)
    """
    cases_api_source = CasesApi(source_service.client)
    all_hashes = set()
    url_map = {}
    offset = 0
    limit = 100
    
    while True:
        try:
            cases_response = retry_with_backoff(
                cases_api_source.get_cases,
                code=project_code,
                limit=limit,
                offset=offset
            )
            
            cases_entities = extract_entities_from_response(cases_response)
            if not cases_entities:
                break
            
            for case in cases_entities:
                case_dict = to_dict(case)
                
                # Case-level attachments
                if case_dict.get('attachments'):
                    for att_item in case_dict['attachments']:
                        if isinstance(att_item, str):
                            all_hashes.add(att_item.lower())
                        elif isinstance(att_item, dict):
                            if 'hash' in att_item:
                                all_hashes.add(str(att_item['hash']).lower())
                            elif 'url' in att_item:
                                url = att_item['url']
                                match = re.search(r'/attachment/([a-f0-9]{32,64})/', url, re.IGNORECASE)
                                if match:
                                    all_hashes.add(match.group(1).lower())
                
                # Case text fields
                case_text_fields = ['description', 'preconditions', 'postconditions']
                case_text_hashes = extract_attachment_hashes_from_dict(case_dict, case_text_fields)
                case_text_urls = extract_attachment_urls_from_dict(case_dict, case_text_fields)
                all_hashes.update(case_text_hashes)
                url_map.update(case_text_urls)
                
                # Custom fields
                if case_dict.get('custom_fields'):
                    custom_field_hashes = extract_attachment_hashes_from_dict(
                        {'custom_fields': case_dict['custom_fields']}, ['custom_fields']
                    )
                    custom_field_urls = extract_attachment_urls_from_dict(
                        {'custom_fields': case_dict['custom_fields']}, ['custom_fields']
                    )
                    all_hashes.update(custom_field_hashes)
                    url_map.update(custom_field_urls)
                
                # Steps
                if case_dict.get('steps'):
                    for step in case_dict['steps']:
                        step_dict = to_dict(step)
                        if step_dict.get('attachments'):
                            normalized = {str(att).lower() for att in step_dict['attachments'] if att}
                            all_hashes.update(normalized)
                        
                        step_text_fields = ['action', 'expected_result', 'data']
                        step_text_hashes = extract_attachment_hashes_from_dict(step_dict, step_text_fields)
                        step_text_urls = extract_attachment_urls_from_dict(step_dict, step_text_fields)
                        all_hashes.update(step_text_hashes)
                        url_map.update(step_text_urls)
            
            if len(cases_entities) < limit:
                break
            offset += limit
        except Exception as e:
            break
    
    return all_hashes, url_map


def extract_attachment_hashes_from_results(
    source_service: QaseService,
    project_code: str
) -> tuple[Set[str], Dict[str, str]]:
    """
    Extract attachment hashes and URLs from all results in a project.
    
    Returns:
        Tuple of (set of hashes, dict of hash -> URL)
    """
    runs_api_source = RunsApi(source_service.client)
    results_api_source = ResultsApi(source_service.client)
    all_hashes = set()
    url_map = {}
    offset = 0
    limit = 100
    
    while True:
        try:
            runs_response = retry_with_backoff(
                runs_api_source.get_runs,
                code=project_code,
                limit=limit,
                offset=offset
            )
            
            runs_entities = extract_entities_from_response(runs_response)
            if not runs_entities:
                break
            
            for run in runs_entities:
                run_dict = to_dict(run)
                run_id = run_dict.get('id')
                
                results_offset = 0
                results_limit = 100
                
                while True:
                    try:
                        results_response = retry_with_backoff(
                            results_api_source.get_results,
                            code=project_code,
                            run=str(run_id),
                            limit=results_limit,
                            offset=results_offset
                        )
                        
                        results_entities = extract_entities_from_response(results_response)
                        if not results_entities:
                            break
                        
                        for result in results_entities:
                            result_dict = to_dict(result)

                            _ingest_nested_result_media(result_dict, all_hashes, url_map)

                            # Result text fields (reporters put links in message / execution.comment)
                            result_text_fields = [
                                "comment",
                                "message",
                                "text",
                                "stacktrace",
                            ]
                            result_text_hashes = extract_attachment_hashes_from_dict(
                                result_dict, result_text_fields
                            )
                            result_text_urls = extract_attachment_urls_from_dict(
                                result_dict, result_text_fields
                            )
                            all_hashes.update(result_text_hashes)
                            url_map.update(result_text_urls)
                            ex = result_dict.get("execution")
                            if isinstance(ex, dict):
                                exh = extract_attachment_hashes_from_dict(
                                    to_dict(ex), ["comment", "message", "stacktrace"]
                                )
                                exu = extract_attachment_urls_from_dict(
                                    to_dict(ex), ["comment", "message", "stacktrace"]
                                )
                                all_hashes.update(exh)
                                url_map.update(exu)

                            # Result steps
                            if result_dict.get('steps'):
                                for step in result_dict['steps']:
                                    try:
                                        step_dict = to_dict(step) if not isinstance(step, dict) else step
                                        if isinstance(step_dict, dict):
                                            _ingest_nested_result_media(step_dict, all_hashes, url_map)
                                            inner = step_dict.get("data")
                                            if isinstance(inner, dict):
                                                _ingest_nested_result_media(
                                                    to_dict(inner), all_hashes, url_map
                                                )
                                            step_text_fields = [
                                                "action",
                                                "expected_result",
                                                "comment",
                                                "message",
                                                "text",
                                                "actual_result",
                                            ]
                                            step_text_hashes = extract_attachment_hashes_from_dict(
                                                step_dict, step_text_fields
                                            )
                                            step_text_urls = extract_attachment_urls_from_dict(
                                                step_dict, step_text_fields
                                            )
                                            valid_hashes = {h for h in step_text_hashes if isinstance(h, str)}
                                            all_hashes.update(valid_hashes)
                                            url_map.update(step_text_urls)
                                            s_ex = step_dict.get("execution")
                                            if isinstance(s_ex, dict):
                                                sd = to_dict(s_ex)
                                                _ingest_attachment_like_items(
                                                    sd.get("attachments"), all_hashes, url_map
                                                )
                                                _ingest_attachment_like_items(
                                                    sd.get("files"), all_hashes, url_map
                                                )
                                                _ingest_attachment_like_items(
                                                    sd.get("screenshots"), all_hashes, url_map
                                                )
                                                seh = extract_attachment_hashes_from_dict(
                                                    sd, ["comment", "message"]
                                                )
                                                seu = extract_attachment_urls_from_dict(
                                                    sd, ["comment", "message"]
                                                )
                                                all_hashes.update(seh)
                                                url_map.update(seu)
                                    except Exception:
                                        continue
                        
                        if len(results_entities) < results_limit:
                            break
                        results_offset += results_limit
                    except Exception as e:
                        break
            
            if len(runs_entities) < limit:
                break
            offset += limit
        except Exception as e:
            break
    
    return all_hashes, url_map


def extract_attachment_hashes_from_defects(
    source_service: QaseService,
    project_code: str
) -> tuple[Set[str], Dict[str, str]]:
    """
    Extract attachment hashes and URLs from all defects in a project.
    
    Args:
        source_service: Source Qase service
        project_code: Project code
    
    Returns:
        Tuple of (set of attachment hashes, dict of hash -> URL)
    """
    from migration.extract.defects import extract_defects
    
    all_hashes = set()
    url_map = {}
    
    defects = extract_defects(source_service, project_code)
    
    for defect in defects:
        defect_dict = to_dict(defect) if hasattr(defect, '__dict__') else defect
        
        # Defect-level attachments
        if defect_dict.get('attachments'):
            for att_item in defect_dict['attachments']:
                if isinstance(att_item, str):
                    all_hashes.add(att_item.lower())
                elif isinstance(att_item, dict):
                    if 'hash' in att_item:
                        hash_val = str(att_item['hash']).lower()
                        all_hashes.add(hash_val)
                        if 'url' in att_item:
                            url_map[hash_val] = att_item['url']
                    elif 'url' in att_item:
                        url = att_item['url']
                        match = re.search(r'/attachment/([a-f0-9]{32,64})/', url, re.IGNORECASE)
                        if match:
                            hash_val = match.group(1).lower()
                            all_hashes.add(hash_val)
                            url_map[hash_val] = url
        
        # Defect text fields (actual_result may contain attachment references)
        defect_text_fields = ['actual_result']
        defect_text_hashes = extract_attachment_hashes_from_dict(defect_dict, defect_text_fields)
        defect_text_urls = extract_attachment_urls_from_dict(defect_dict, defect_text_fields)
        all_hashes.update(defect_text_hashes)
        url_map.update(defect_text_urls)
    
    return all_hashes, url_map


def _qase_service_kw(svc: QaseService) -> Dict[str, Any]:
    return {
        "api_token": svc.api_token,
        "host": svc.host,
        "ssl": svc.ssl,
        "scim_token": getattr(svc, "scim_token", None),
        "scim_host": getattr(svc, "scim_host", None),
    }


def _extract_attachment_hashes_for_one_project(
    source_kw: Dict[str, Any], project: Dict[str, Any]
) -> Tuple[str, Tuple[Set[str], Dict[str, str]]]:
    project_code_source = project["source_code"]
    src = QaseService(**source_kw)
    case_hashes, case_urls = extract_attachment_hashes_from_cases(src, project_code_source)
    result_hashes, result_urls = extract_attachment_hashes_from_results(src, project_code_source)
    defect_hashes, defect_urls = extract_attachment_hashes_from_defects(src, project_code_source)
    all_hashes = case_hashes | result_hashes | defect_hashes
    all_urls = {**case_urls, **result_urls, **defect_urls}
    return project_code_source, (all_hashes, all_urls)


def extract_all_attachment_hashes(
    source_service: QaseService,
    projects: List[Dict[str, Any]],
    max_project_workers: int = 0,
) -> Dict[str, tuple[Set[str], Dict[str, str]]]:
    """
    Extract all attachment hashes from all projects.

    max_project_workers: 0 = auto (min(8, len(projects), cpu)), 1 = sequential.

    Returns:
        Dictionary mapping project_code -> (set of hashes, dict of hash -> URL)
    """
    if not projects:
        return {}

    if len(projects) == 1 or max_project_workers == 1:
        all_project_attachments: Dict[str, tuple[Set[str], Dict[str, str]]] = {}
        for project in projects:
            project_code_source = project["source_code"]
            case_hashes, case_urls = extract_attachment_hashes_from_cases(
                source_service, project_code_source
            )
            result_hashes, result_urls = extract_attachment_hashes_from_results(
                source_service, project_code_source
            )
            defect_hashes, defect_urls = extract_attachment_hashes_from_defects(
                source_service, project_code_source
            )
            all_hashes = case_hashes | result_hashes | defect_hashes
            all_urls = {**case_urls, **result_urls, **defect_urls}
            all_project_attachments[project_code_source] = (all_hashes, all_urls)
        return all_project_attachments

    ncpu = os.cpu_count() or 4
    workers = max_project_workers if max_project_workers > 1 else min(8, max(2, len(projects), ncpu))
    workers = max(1, min(workers, len(projects)))
    source_kw = _qase_service_kw(source_service)
    all_project_attachments = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_extract_attachment_hashes_for_one_project, source_kw, p) for p in projects
        ]
        for fut in as_completed(futures):
            code, payload = fut.result()
            all_project_attachments[code] = payload
    return all_project_attachments
