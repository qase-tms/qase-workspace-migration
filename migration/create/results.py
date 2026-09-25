"""
Create results in target Qase workspace using API v2 bulk create.

V2 ``POST /{project}/run/{id}/results`` uses ``ResultCreate`` with execution, message,
attachments, and ``ResultStep`` data.

Mapped results use ``testops_id``. Unmapped automated results omit ``testops_id`` and
use ``fields`` / ``relations`` only (no v1 case bulk create — avoids filling the
repository when "create cases from automated results" is off).

Titles use whatever the result/detail payload already includes; otherwise use
``Automated Test {id}`` (source ``case_id``, result ``id``, mapped target id, or hash
prefix). ``relations.suite`` mirrors the source case suite path
when the API exposes it (e.g. ``suite_title`` or ``suite``).
"""
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from qase.api_client_v1.exceptions import ApiException
from qase.api_client_v2.api.results_api import ResultsApi
from qase.api_client_v2.exceptions import ApiException as ApiExceptionV2
from qase.api_client_v2.models.create_results_request_v2 import CreateResultsRequestV2
from qase.api_client_v2.models.result_create import ResultCreate
from qase.api_client_v2.models.result_create_fields import ResultCreateFields
from qase.api_client_v2.models.result_execution import ResultExecution
from qase.api_client_v2.models.result_relations import ResultRelations
from qase.api_client_v2.models.relation_suite import RelationSuite
from qase.api_client_v2.models.relation_suite_item import RelationSuiteItem
from qase.api_client_v2.models.result_step import ResultStep
from qase.api_client_v2.models.result_step_data import ResultStepData
from qase.api_client_v2.models.result_step_execution import ResultStepExecution
from qase.api_client_v2.models.result_step_status import ResultStepStatus

from qase.api_client_v1.api.runs_api import RunsApi
from qase.api_client_v1.api.cases_api import CasesApi
from qase_service import QaseService
from migration.step_logging import set_current_project, step_log_info
from migration.utils import MigrationMappings, MigrationStats, chunks, to_dict
from migration.extract.results import ResultsFetchError, extract_results, fetch_result_detail_json
from migration.extract.runs import fetch_run_detail_json
from migration.extract.authors import extract_authors
from migration.create.runs import _source_run_should_complete_after_results
from migration.transform.attachments import replace_attachment_hashes_in_text
from migration.trace_log import (
    chunk_results_payload_for_trace,
    summarize_enriched_result,
    summarize_result_create,
)

if TYPE_CHECKING:
    from migration.progress import ProjectMigrationProgress

logger = logging.getLogger(__name__)

# Qase v2 accepts at most 200 results per POST /v2/result/{code}/{run_id}.
# A rejected request is not retried blind: `_send_chunk` halves it and retries,
# so a lowered cap or one bad row costs a retry rather than the whole chunk.
MAX_RESULTS_PER_REQUEST = 200

# I/O-bound: result detail GETs + case metadata. Cap concurrency to limit API load.
_RESULTS_ROW_PARALLEL_MIN = 8
_RESULTS_ROW_MAX_WORKERS = 24
_RESULTS_RUN_PARALLEL_MAX = 6
# Minimum row-pool size when using threads (keeps small projects from going sequential-only).
_RESULTS_ROW_MIN_WORKERS = 4

# Step fields Qase often nests under `execution` (especially automated / reporter runs).
_STEP_EXECUTION_OVERLAY_KEYS = frozenset(
    {
        "comment",
        "Comment",
        "message",
        "Message",
        "text",
        "Text",
        "actual_result",
        "actualResult",
        "actual",
        "Actual",
        "result",
        "Result",
        "body",
        "Body",
        "content",
        "Content",
        "output",
        "Output",
        "log",
        "Log",
        "stdout",
        "stderr",
        "error",
        "Error",
        "errors",
        "attachments",
        "duration",
        "time_ms",
        "time_spent_ms",
        "stacktrace",
        "start_time",
        "end_time",
        "status",
        "status_id",
    }
)


def _is_nonempty_for_merge(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, dict, set)):
        return len(v) > 0
    return True


_STEP_INT_TO_V2: Dict[int, ResultStepStatus] = {
    1: ResultStepStatus.PASSED,
    2: ResultStepStatus.FAILED,
    3: ResultStepStatus.BLOCKED,
    5: ResultStepStatus.SKIPPED,
    7: ResultStepStatus.IN_PROGRESS,
}


# Qase markdown and API URLs: /attachment/{hash}/ or /attachments/{hash}/; hash may be UUID.
_ATTACHMENT_URL_RE = re.compile(
    r"/attachments?/([a-f0-9]{32,64}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/",
    re.IGNORECASE,
)


def _looks_like_attachment_hash(s: str) -> bool:
    t = str(s).strip().replace("-", "")
    return len(t) >= 32 and bool(re.fullmatch(r"[a-f0-9]+", t, re.I))


def _candidate_hash_keys(h: str) -> List[str]:
    s = str(h).strip()
    if not s:
        return []
    keys = [s, s.lower(), s.upper()]
    if "-" in s:
        nd = s.replace("-", "")
        keys.extend([nd, nd.lower(), nd.upper()])
    out: List[str] = []
    seen = set()
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _hashes_from_attachment_item(att_item: Any) -> List[str]:
    found: List[str] = []
    if isinstance(att_item, str):
        s = att_item.strip()
        if _looks_like_attachment_hash(s):
            found.append(s.replace("-", "").lower() if "-" in s else s)
        m = _ATTACHMENT_URL_RE.search(s)
        if m:
            g = m.group(1).replace("-", "").lower()
            if g not in found:
                found.append(g)
        return found
    if isinstance(att_item, dict):
        d = to_dict(att_item)
        for k in (
            "hash",
            "attachment_hash",
            "attachmentHash",
            "file_hash",
            "fileHash",
            "uuid",
            "UUID",
        ):
            v = d.get(k)
            if v is not None and _looks_like_attachment_hash(str(v)):
                found.append(str(v).replace("-", "").lower() if "-" in str(v) else str(v).strip())
        vid = d.get("id")
        if vid is not None and _looks_like_attachment_hash(str(vid)):
            found.append(str(vid).replace("-", "").lower())
        for uk in (
            "url",
            "URL",
            "link",
            "href",
            "thumb_url",
            "thumbnail_url",
            "preview_url",
            "src",
            "file_url",
        ):
            u = d.get(uk)
            if u:
                m = _ATTACHMENT_URL_RE.search(str(u))
                if m:
                    hx = m.group(1).replace("-", "").lower()
                    if hx not in found:
                        found.append(hx)
    return found


def _map_attachment_hashes(
    att_items: Any,
    attachment_mapping: Dict[str, str],
) -> List[str]:
    if not att_items or not attachment_mapping:
        return []
    if not isinstance(att_items, list):
        att_items = [att_items]
    out: List[str] = []
    seen = set()
    for att_item in att_items:
        for raw_h in _hashes_from_attachment_item(att_item):
            mapped_val = None
            for key in _candidate_hash_keys(raw_h):
                mapped_val = (
                    attachment_mapping.get(key)
                    or attachment_mapping.get(key.lower())
                    or attachment_mapping.get(key.upper())
                )
                if mapped_val:
                    break
            if mapped_val:
                ms = str(mapped_val).strip()
                if ms and ms not in seen:
                    seen.add(ms)
                    out.append(ms)
    return out


def _coalesce_result_attachments(result_dict: Dict[str, Any]) -> None:
    """
    Automated / reporter runs often store media under files, screenshots, execution.* — not only `attachments`.
    """
    merged: List[Any] = []
    seen_sig: set = set()

    def _add(items: Any) -> None:
        if not isinstance(items, list):
            return
        for it in items:
            sig = repr(it) if isinstance(it, dict) else str(it)
            if sig in seen_sig:
                continue
            seen_sig.add(sig)
            merged.append(it)

    _add(result_dict.get("attachments"))
    for key in ("files", "screenshots", "media", "images", "evidence", "artifacts"):
        _add(result_dict.get(key))
    ex = result_dict.get("execution")
    if isinstance(ex, dict):
        exd = to_dict(ex)
        _add(exd.get("attachments"))
        for key in ("files", "screenshots", "media", "images"):
            _add(exd.get(key))
    if merged:
        result_dict["attachments"] = merged


def _rewrite_text(
    text: Optional[str],
    attachment_mapping: Dict[str, str],
    target_workspace_hash: Optional[str],
) -> Optional[str]:
    if not text or not isinstance(text, str):
        return text
    if not attachment_mapping:
        return text
    return replace_attachment_hashes_in_text(text, attachment_mapping, target_workspace_hash)


def _iter_result_steps(result_dict: Dict[str, Any]) -> Optional[List[Any]]:
    for key in (
        "steps",
        "step_results",
        "elements",
        "result_steps",
        "test_results",
        "substeps",
    ):
        v = result_dict.get(key)
        if isinstance(v, list) and len(v) > 0:
            return v
    return None


def _pick_str(d: Dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = d.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _flatten_step_dict(step: Any) -> Dict[str, Any]:
    """Merge nested `data` and `execution` onto the step dict (manual + automated shapes)."""
    sd = to_dict(step) if not isinstance(step, dict) else dict(step)
    inner = sd.get("data")
    if isinstance(inner, dict):
        inn = to_dict(inner)
        for k, v in inn.items():
            if sd.get(k) in (None, "", [], {}):
                sd[k] = v
    for ex_key in ("execution", "Execution"):
        ex = sd.get(ex_key)
        if not isinstance(ex, dict):
            continue
        inn = to_dict(ex)
        for k, v in inn.items():
            if k in _STEP_EXECUTION_OVERLAY_KEYS:
                if k in ("status", "status_id"):
                    if v is not None:
                        sd[k] = v
                elif _is_nonempty_for_merge(v):
                    sd[k] = v
            elif sd.get(k) in (None, "", [], {}):
                sd[k] = v
    return sd


def _merge_result_top_level_execution(result_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Promote result-level `execution` (automated runs) into flat fields for v2 message/duration."""
    out = dict(result_dict)
    ex = out.get("execution")
    if not isinstance(ex, dict):
        return out
    exd = to_dict(ex)
    if _is_nonempty_for_merge(exd.get("comment")) and not _is_nonempty_for_merge(out.get("comment")):
        out["comment"] = exd["comment"]
    if _is_nonempty_for_merge(exd.get("message")) and not _is_nonempty_for_merge(out.get("message")):
        out["message"] = exd["message"]
    st = exd.get("stacktrace") or exd.get("stack_trace")
    if _is_nonempty_for_merge(st) and not _is_nonempty_for_merge(out.get("stacktrace")):
        out["stacktrace"] = st
    for ex_k, out_k in (
        ("time_spent_ms", "time_spent_ms"),
        ("time_ms", "time_spent_ms"),
        ("duration_ms", "time_spent_ms"),
    ):
        if ex_k in exd and exd[ex_k] is not None and out.get(out_k) in (None, 0, ""):
            try:
                out[out_k] = int(exd[ex_k])
            except (TypeError, ValueError):
                out[out_k] = exd[ex_k]
    dur = exd.get("duration")
    if dur is not None and out.get("time_spent_ms") in (None, 0, ""):
        try:
            out["time_spent_ms"] = int(float(dur))
        except (TypeError, ValueError):
            pass
    if not _iter_result_steps(out) and isinstance(exd.get("steps"), list) and len(exd["steps"]) > 0:
        out["steps"] = exd["steps"]
    return out


def _merge_result_detail_into_summary(summary: Dict[str, Any], detail: Dict[str, Any]) -> Dict[str, Any]:
    if not detail:
        return dict(summary)
    out = dict(summary)
    for k in (
        "steps",
        "comment",
        "message",
        "stacktrace",
        "attachments",
        "status",
        "status_id",
        "time_spent_ms",
        "member_id",
        "author_uuid",
        "files",
        "screenshots",
        "media",
        "images",
        "params",
        "param",
        "param_groups",
        "parameters",
        "signature",
        "suite_title",
        "suiteTitle",
    ):
        if k not in detail:
            continue
        v = detail[k]
        if v is None:
            continue
        if k == "steps" and isinstance(v, list):
            prev = out.get("steps")
            if not isinstance(prev, list) or len(v) >= len(prev):
                out[k] = v
        elif k in ("files", "screenshots", "media", "images") and isinstance(v, list):
            prev = out.get(k)
            if not isinstance(prev, list) or len(v) > len(prev):
                out[k] = v
        else:
            out[k] = v
    for k, v in detail.items():
        if v is None or k in out:
            continue
        out[k] = v

    if not _pick_str(out, "title", "Title", "case_title", "caseTitle", "name", "Name"):
        ct = _pick_str(detail, "case_title", "caseTitle")
        if ct:
            out["title"] = ct
        else:
            case_d = detail.get("case")
            if isinstance(case_d, dict):
                t = _pick_str(case_d, "title", "Title", "name", "Name")
                if t:
                    out["title"] = t
    return out


def _merge_result_steps_with_case_steps(
    result_steps: List[Any],
    case_steps: List[Any],
    parent_status_id: Any = None,
) -> List[Dict[str, Any]]:
    """
    Overlay case step definitions (action / expected / shared ref) onto result rows.
    Case dict is merged first so result execution fields (status, attachments, comment) win.
    """
    rs = [_flatten_step_dict(x) for x in (result_steps or [])]
    cs = [_flatten_step_dict(x) for x in (case_steps or [])]
    if not cs:
        return rs
    if not rs:
        synth: List[Dict[str, Any]] = []
        for c in cs:
            d = dict(c)
            if d.get("status") is None and d.get("status_id") is None and parent_status_id is not None:
                d["status_id"] = parent_status_id
            synth.append(d)
        return synth

    out: List[Dict[str, Any]] = []
    n = max(len(rs), len(cs))
    for i in range(n):
        r = rs[i] if i < len(rs) else {}
        c = cs[i] if i < len(cs) else {}
        merged = {**c, **r}
        r_act = _pick_str(merged, "action", "Action", "title", "Title", "name", "Name")
        if not r_act or r_act == ".":
            c_act = _pick_str(c, "action", "Action", "title", "Title", "name", "Name")
            if c_act:
                merged["action"] = c_act
        r_exp = _pick_str(merged, "expected_result", "expectedResult", "expected", "Expected")
        if not r_exp:
            c_exp = _pick_str(c, "expected_result", "expectedResult", "expected", "Expected")
            if c_exp:
                merged["expected_result"] = c_exp
        rnest = r.get("steps") if isinstance(r.get("steps"), list) else None
        cnest = c.get("steps") if isinstance(c.get("steps"), list) else None
        if rnest is not None or cnest is not None:
            merged["steps"] = _merge_result_steps_with_case_steps(
                rnest or [],
                cnest or [],
                parent_status_id,
            )
        out.append(merged)
    return out


def _suite_path_titles_from_case_dict(cd: Dict[str, Any]) -> List[str]:
    """Build root→leaf suite titles for v2 ``relations.suite`` when present on case JSON."""
    st = cd.get("suite_title") or cd.get("suiteTitle")
    if isinstance(st, str) and st.strip():
        parts = [x.strip() for x in re.split(r"[\t\n\r]+", st) if x.strip()]
        if parts:
            return parts
    su = cd.get("suite")
    if isinstance(su, dict):
        t = _pick_str(su, "title", "Title", "name", "Name")
        if t:
            return [t]
    nested = cd.get("suites")
    if isinstance(nested, list) and nested:
        out: List[str] = []
        for item in nested:
            if isinstance(item, dict):
                t = _pick_str(item, "title", "Title", "name", "Name")
                if t:
                    out.append(t)
            elif isinstance(item, str) and item.strip():
                out.append(item.strip())
        if out:
            return out
    return []


def _fetch_source_case_metadata(
    source_service: QaseService,
    project_code: str,
    case_id: int,
) -> Dict[str, Any]:
    """
    Single get_case for source workspace. Does not use retry_with_backoff so 404s stay quiet
    (deleted / missing cases still appear in old run results).
    """
    meta: Dict[str, Any] = {"steps": [], "suite_path_titles": []}
    try:
        cases_api = CasesApi(source_service.client)
        resp = cases_api.get_case(code=project_code, id=case_id)
        if resp and getattr(resp, "result", None):
            cd = to_dict(resp.result)
            meta["steps"] = cd.get("steps") or []
            meta["suite_path_titles"] = _suite_path_titles_from_case_dict(cd)
    except ApiException as e:
        if e.status == 404:
            logger.debug(
                "get_case 404 (case missing or removed): project=%s case_id=%s",
                project_code,
                case_id,
            )
        else:
            logger.warning("get_case failed project=%s case_id=%s: %s", project_code, case_id, e)
    except Exception as e:
        logger.debug("get_case error project=%s case_id=%s: %s", project_code, case_id, e)
    return meta


def _get_source_case_cached(
    cache: Dict[int, Dict[str, Any]],
    source_service: QaseService,
    project_code: str,
    case_id: int,
    cache_lock: Optional[threading.Lock] = None,
) -> Dict[str, Any]:
    if cache_lock is None:
        if case_id in cache:
            return cache[case_id]
        meta = _fetch_source_case_metadata(source_service, project_code, case_id)
        cache[case_id] = meta
        return meta

    with cache_lock:
        if case_id in cache:
            return cache[case_id]
    meta = _fetch_source_case_metadata(source_service, project_code, case_id)
    with cache_lock:
        if case_id in cache:
            return cache[case_id]
        cache[case_id] = meta
        return meta


def enrich_source_result_for_v2(
    result_dict: Dict[str, Any],
    source_service: QaseService,
    project_code: str,
    case_steps_cache: Dict[int, Dict[str, Any]],
    cache_lock: Optional[threading.Lock] = None,
) -> Dict[str, Any]:
    out = dict(result_dict)
    h = out.get("hash")
    if h:
        detail = fetch_result_detail_json(source_service, project_code, str(h))
        if detail:
            out = _merge_result_detail_into_summary(out, detail)

    out = _merge_result_top_level_execution(out)

    if not out.get("_source_suite_path_titles"):
        spt = _suite_path_titles_from_case_dict(out)
        if spt:
            out["_source_suite_path_titles"] = spt
    if not out.get("_source_suite_path_titles"):
        for ck in ("case", "test_case", "Case", "testCase"):
            c = out.get(ck)
            if isinstance(c, dict):
                spt = _suite_path_titles_from_case_dict(to_dict(c))
                if spt:
                    out["_source_suite_path_titles"] = spt
                    break

    steps_list = _iter_result_steps(out)
    sid = out.get("case_id")
    if sid is not None:
        try:
            cid = int(sid)
        except (TypeError, ValueError):
            cid = None
        if cid is not None:
            meta = _get_source_case_cached(
                case_steps_cache, source_service, project_code, cid, cache_lock
            )
            case_steps = meta.get("steps") or []
            spt = meta.get("suite_path_titles") or []
            if isinstance(spt, list) and spt:
                out["_source_suite_path_titles"] = spt
            if case_steps:
                merged = _merge_result_steps_with_case_steps(
                    steps_list or [],
                    case_steps,
                    parent_status_id=out.get("status_id"),
                )
                if merged:
                    out["steps"] = merged
    _coalesce_result_attachments(out)
    return out


def _resolve_target_author_id(
    result_dict: Dict[str, Any],
    mappings: MigrationMappings,
    author_uuid_to_id_mapping: Dict[str, int],
) -> int:
    author_uuid = result_dict.get("author_uuid")
    if author_uuid:
        source_author_id = author_uuid_to_id_mapping.get(author_uuid)
        if source_author_id:
            try:
                sid = int(source_author_id)
                if sid == 0:
                    return 1
                return mappings.get_user_id(sid)
            except (ValueError, TypeError):
                pass

    for key in ("member_id", "author_id", "user_id", "created_by"):
        raw = result_dict.get(key)
        if raw is None:
            continue
        try:
            uid = int(raw)
            if uid == 0:
                return 1
            return mappings.get_user_id(uid)
        except (ValueError, TypeError):
            continue
    return 1


def _resolve_result_status_string(result_dict: Dict[str, Any]) -> str:
    status_map = {
        1: "passed",
        2: "blocked",
        3: "skipped",
        4: "retest",
        5: "failed",
    }
    status_id = result_dict.get("status_id")
    status_str = result_dict.get("status", "").lower() if result_dict.get("status") else None

    if status_id is not None and status_id in status_map:
        return status_map[status_id]
    if status_str:
        sl = status_str.lower()
        if sl in ("passed", "pass"):
            return "passed"
        if sl in ("failed", "fail"):
            return "failed"
        if sl in ("blocked", "block"):
            return "blocked"
        if sl in ("skipped", "skip"):
            return "skipped"
        if sl in ("retest", "retry"):
            return "retest"
        if sl in ("invalid",):
            return "invalid"
        if sl in ("in_progress", "in progress", "pending"):
            return "in_progress"
        if sl in ("untested",):
            return "untested"
        return "skipped"
    return "skipped"


def _map_v2_run_execution_status(core: str) -> str:
    """ResultExecution.status — coerce values that commonly 422 on historical runs."""
    c = (core or "skipped").lower()
    if c == "retest":
        return "blocked"
    if c == "in_progress":
        return "blocked"
    if c in ("passed", "failed", "blocked", "skipped", "invalid", "untested"):
        return c
    return "skipped"


def _map_step_status_v2(val: Any) -> ResultStepStatus:
    if val is None:
        return ResultStepStatus.PASSED
    if isinstance(val, str):
        v = val.lower().strip()
        if v in ("passed", "pass"):
            return ResultStepStatus.PASSED
        if v in ("failed", "fail"):
            return ResultStepStatus.FAILED
        if v in ("blocked", "block"):
            return ResultStepStatus.BLOCKED
        if v in ("skipped", "skip"):
            return ResultStepStatus.SKIPPED
        if v in ("in_progress", "in progress", "pending"):
            return ResultStepStatus.IN_PROGRESS
        return ResultStepStatus.PASSED
    if isinstance(val, int):
        return _STEP_INT_TO_V2.get(val, ResultStepStatus.PASSED)
    return ResultStepStatus.PASSED


def _result_duration_ms(result_dict: Dict[str, Any]) -> Optional[int]:
    for key in ("time_spent_ms", "time_ms", "duration_ms"):
        v = result_dict.get(key)
        if v is not None:
            try:
                n = int(v)
                return n if n >= 0 else None
            except (TypeError, ValueError):
                pass
    for key in ("time", "duration"):
        v = result_dict.get(key)
        if v is not None:
            try:
                n = int(v)
                return n * 1000 if n >= 0 else None
            except (TypeError, ValueError):
                pass
    return None


def _build_v2_step(
    step: Any,
    attachment_mapping: Dict[str, str],
    target_workspace_hash: Optional[str],
) -> Optional[ResultStep]:
    sd = _flatten_step_dict(step)
    if not sd:
        return None

    action = _pick_str(sd, "action", "Action", "title", "Title", "name", "Name") or "."
    er = _pick_str(sd, "expected_result", "expectedResult", "expected", "Expected")
    expected_result: Optional[str] = er or None

    data = ResultStepData(action=action, expected_result=expected_result)

    actual = _pick_str(
        sd,
        "actual_result",
        "actualResult",
        "actual",
        "Actual",
        "result",
        "Result",
        "body",
        "Body",
        "content",
        "Content",
        "output",
        "Output",
        "log",
        "Log",
        "stdout",
        "stderr",
        "error",
        "Error",
    ) or None
    extra_comment = _pick_str(sd, "comment", "Comment", "message", "Message", "text", "Text") or None
    parts: List[str] = []
    if actual:
        parts.append(f"**Actual result**\n{actual}")
    if extra_comment:
        parts.append(f"**Comment**\n{extra_comment}")
    comment_md = "\n\n".join(parts) if parts else None
    if comment_md:
        rw = _rewrite_text(comment_md, attachment_mapping, target_workspace_hash)
        comment_md = rw if rw else comment_md

    step_attachments = _map_attachment_hashes(sd.get("attachments"), attachment_mapping)
    step_status_raw = sd.get("status")
    if step_status_raw is None:
        step_status_raw = sd.get("status_id")
    step_exec_kwargs: Dict[str, Any] = {
        "status": _map_step_status_v2(step_status_raw),
        "start_time": None,
        "end_time": None,
    }
    if comment_md:
        step_exec_kwargs["comment"] = comment_md
    if step_attachments:
        step_exec_kwargs["attachments"] = step_attachments
    sdur = sd.get("duration") or sd.get("time_ms") or sd.get("time_spent_ms")
    if sdur is not None:
        try:
            step_exec_kwargs["duration"] = int(sdur)
        except (TypeError, ValueError):
            pass

    execution = ResultStepExecution(**step_exec_kwargs)

    nested = sd.get("steps")
    nested_dicts: Optional[List[Dict[str, Any]]] = None
    if nested and isinstance(nested, list):
        nested_dicts = []
        for ch in nested:
            child_rs = _build_v2_step(ch, attachment_mapping, target_workspace_hash)
            if child_rs:
                nested_dicts.append(child_rs.model_dump(by_alias=True, exclude_none=True))

    if nested_dicts:
        return ResultStep(
            data=data,
            execution=execution,
            steps=nested_dicts,
        )
    return ResultStep(data=data, execution=execution)


def _coerce_str_params_map(d: Dict[str, Any]) -> Dict[str, str]:
    return {str(k): "" if v is None else str(v) for k, v in d.items()}


def _params_dict_from_list_items(items: List[Any]) -> Optional[Dict[str, str]]:
    acc: Dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = _pick_str(
            item,
            "title",
            "Title",
            "name",
            "Name",
            "key",
            "Key",
            "parameter",
            "Parameter",
        )
        if not name:
            continue
        val = item.get("value")
        if val is None:
            val = item.get("Value")
        if val is None:
            val = _pick_str(
                item,
                "selected",
                "Selected",
                "selected_value",
                "selectedValue",
            )
        acc[name] = "" if val is None else str(val)
    return acc if acc else None


def _normalize_param_groups_v1(raw: Any) -> Optional[List[List[str]]]:
    if not isinstance(raw, list) or not raw:
        return None
    out: List[List[str]] = []
    for g in raw:
        if isinstance(g, str):
            s = g.strip()
            if s:
                out.append([s])
        elif isinstance(g, list):
            inner = [str(x) for x in g if x is not None and str(x).strip() != ""]
            if inner:
                out.append(inner)
        elif isinstance(g, dict):
            gd = to_dict(g)
            titles: List[str] = []
            for it in gd.get("parameters") or gd.get("items") or []:
                if isinstance(it, dict):
                    t = _pick_str(it, "title", "Title", "name", "Name")
                    if t:
                        titles.append(t)
            if titles:
                out.append(titles)
    return out or None


def _extract_v2_params_from_result(
    result_dict: Dict[str, Any],
) -> Tuple[Optional[Dict[str, str]], Optional[List[List[str]]]]:
    """
    Map v1 result payloads to v2 params / param_groups.
    Covers `params` vs `param`, nested case, execution, and list-shaped `parameters`.
    """
    str_params: Optional[Dict[str, str]] = None
    groups_raw: Any = result_dict.get("param_groups")

    def take_from_mapping(m: Dict[str, Any]) -> None:
        nonlocal str_params, groups_raw
        if groups_raw is None:
            groups_raw = m.get("param_groups")
        if str_params is not None:
            return
        for key in ("params", "param"):
            v = m.get(key)
            if isinstance(v, dict) and v:
                str_params = _coerce_str_params_map(v)
                return
            if isinstance(v, list) and v:
                str_params = _params_dict_from_list_items(v)
                return
        pl = m.get("parameters")
        if isinstance(pl, list) and pl:
            str_params = _params_dict_from_list_items(pl)

    take_from_mapping(result_dict)

    if str_params is None:
        ex = result_dict.get("execution")
        if isinstance(ex, dict):
            take_from_mapping(to_dict(ex))

    if str_params is None:
        for ck in ("case", "test_case", "Case", "testCase"):
            c = result_dict.get(ck)
            if isinstance(c, dict):
                take_from_mapping(to_dict(c))
            if str_params is not None:
                break

    param_groups = _normalize_param_groups_v1(groups_raw)
    return str_params, param_groups


def _v2_result_row_identity(
    target_case_id: Optional[int],
    str_params: Optional[Dict[str, str]],
    result_dict: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str]]:
    """
    v2 `id` (idempotency) + `signature` so bulk create does not collapse parameterized rows.
    Prefer source hash for `id`; build `signature` from case id + params (+ hash when present).
    """
    row_id: Optional[str] = None
    sh = result_dict.get("hash") or result_dict.get("result_hash")
    if isinstance(sh, str) and sh.strip():
        row_id = sh.strip()

    existing = result_dict.get("signature")
    if isinstance(existing, str) and existing.strip():
        return row_id, existing.strip()

    tc_key = str(int(target_case_id)) if target_case_id is not None else "none"
    if str_params:
        ppart = "|".join(f"{k}={v}" for k, v in sorted(str_params.items()))
        sig = f"{tc_key}:{ppart}"
        if row_id:
            sig = f"{sig}:{row_id}"
        return row_id, sig
    if row_id:
        return row_id, f"{tc_key}:{row_id}"
    return row_id, None


def _standalone_migration_description(result_dict: Dict[str, Any], source_case_id: Any) -> str:
    lines = [
        "Imported by workspace migration (no mapped test case on target).",
    ]
    if source_case_id is not None:
        lines.append(f"Source case_id: {source_case_id}")
    h = result_dict.get("hash") or result_dict.get("result_hash")
    if h:
        lines.append(f"Source result hash: {h}")
    return "\n".join(lines)


def _suite_relations_from_path_titles(titles: List[str]) -> Optional[ResultRelations]:
    data: List[RelationSuiteItem] = []
    for t in titles:
        s = str(t).strip()
        if not s:
            continue
        if len(s) > 500:
            s = s[:497] + "..."
        data.append(RelationSuiteItem(title=s))
    if not data:
        return None
    return ResultRelations(suite=RelationSuite(data=data))


def _placeholder_automated_test_title(
    result_dict: Dict[str, Any], target_case_id: Optional[int]
) -> str:
    """When the public API omits case/result titles, use a readable stable label."""
    for key in ("case_id", "CaseId", "test_case_id"):
        v = result_dict.get(key)
        if v is not None and str(v).strip():
            try:
                return f"Automated Test {int(v)}"
            except (TypeError, ValueError):
                pass
    rid = result_dict.get("id")
    if rid is not None and str(rid).strip():
        try:
            return f"Automated Test {int(rid)}"
        except (TypeError, ValueError):
            pass
    if target_case_id is not None:
        return f"Automated Test {int(target_case_id)}"
    sh = result_dict.get("hash") or result_dict.get("result_hash")
    if isinstance(sh, str) and sh.strip():
        s = sh.strip()
        return f"Automated Test {s[:12]}"
    return "Automated Test"


def _resolve_result_display_title(
    result_dict: Dict[str, Any], target_case_id: Optional[int]
) -> str:
    """Use payload title fields when present; otherwise ``Automated Test {…}`` placeholder."""
    title = _pick_str(
        result_dict,
        "title",
        "Title",
        "case_title",
        "caseTitle",
        "name",
        "Name",
    )
    if title:
        return title
    for key in ("case", "test_case", "TestCase"):
        c = result_dict.get(key)
        if isinstance(c, dict):
            title = _pick_str(c, "title", "Title", "name", "Name")
            if title:
                return title
    return _placeholder_automated_test_title(result_dict, target_case_id)


def _source_to_result_create_v2(
    result_dict: Dict[str, Any],
    target_case_id: Optional[int],
    mappings: MigrationMappings,
    author_uuid_to_id_mapping: Dict[str, int],
    attachment_mapping: Dict[str, str],
) -> ResultCreate:
    """Build one API v2 ResultCreate from a v1-style result payload."""
    _coalesce_result_attachments(result_dict)
    target_workspace_hash = getattr(mappings, "target_workspace_hash", None)
    core = _resolve_result_status_string(result_dict)

    title = _resolve_result_display_title(result_dict, target_case_id)

    duration_ms = _result_duration_ms(result_dict)
    stack_raw = result_dict.get("stacktrace")
    stacktrace = None
    if stack_raw:
        stacktrace = _rewrite_text(str(stack_raw), attachment_mapping, target_workspace_hash)
        if isinstance(stacktrace, str) and not stacktrace.strip():
            stacktrace = None

    execution = ResultExecution(
        start_time=None,
        end_time=None,
        status=_map_v2_run_execution_status(core),
        duration=duration_ms,
        stacktrace=stacktrace,
    )

    msg_parts: List[str] = []
    for key in ("comment", "message", "text"):
        t = result_dict.get(key)
        if t:
            msg_parts.append(str(t))
    message = "\n\n".join(msg_parts) if msg_parts else None
    if message:
        rw = _rewrite_text(message, attachment_mapping, target_workspace_hash)
        message = rw if rw else message
        if isinstance(message, str) and not message.strip():
            message = None

    res_attachments = _map_attachment_hashes(result_dict.get("attachments"), attachment_mapping)

    steps_src = _iter_result_steps(result_dict)
    steps_models: Optional[List[ResultStep]] = None
    if steps_src:
        built: List[ResultStep] = []
        for s in steps_src:
            rs = _build_v2_step(s, attachment_mapping, target_workspace_hash)
            if rs:
                built.append(rs)
        if built:
            steps_models = built

    str_params, param_groups = _extract_v2_params_from_result(result_dict)
    row_id, v2_signature = _v2_result_row_identity(target_case_id, str_params, result_dict)

    defect = result_dict.get("defect")
    defect_b: Optional[bool] = bool(defect) if defect is not None else None

    rels: Optional[ResultRelations] = None
    sp = result_dict.get("_source_suite_path_titles")
    if isinstance(sp, list) and sp:
        rels = _suite_relations_from_path_titles([str(x) for x in sp])

    author_internal_id = _resolve_target_author_id(
        result_dict, mappings, author_uuid_to_id_mapping
    )
    author_str = str(author_internal_id) if author_internal_id is not None else None

    kwargs: Dict[str, Any] = {
        "title": title,
        "execution": execution,
    }
    if target_case_id is not None:
        kwargs["testops_id"] = int(target_case_id)
    else:
        desc = _standalone_migration_description(result_dict, result_dict.get("case_id"))
        if attachment_mapping:
            rw = _rewrite_text(desc, attachment_mapping, target_workspace_hash)
            desc = rw if rw else desc
        kwargs["fields"] = ResultCreateFields(
            description=desc,
            author=author_str,
            executed_by=author_str,
        )
    if rels:
        kwargs["relations"] = rels
    if message:
        kwargs["message"] = message
    if res_attachments:
        kwargs["attachments"] = res_attachments
    if steps_models is not None:
        kwargs["steps"] = steps_models
    if str_params:
        kwargs["params"] = str_params
    if param_groups:
        kwargs["param_groups"] = param_groups
    if row_id:
        kwargs["id"] = row_id
    if v2_signature:
        kwargs["signature"] = v2_signature
    if defect_b is not None:
        kwargs["defect"] = defect_b

    return ResultCreate(**kwargs)


def _create_results_v2_with_retry(
    results_api: ResultsApi,
    project_code: str,
    run_id: int,
    request: CreateResultsRequestV2,
    max_attempts: int = 4,
    trace: Any = None,
    trace_ctx: Optional[Dict[str, Any]] = None,
    full_payloads: bool = False,
) -> Tuple[bool, Optional[int]]:
    """
    Send one results request. Returns (ok, http_status).

    The status is returned so the caller can tell a deterministic rejection (too
    many results, or one unacceptable row) from a transient failure, and split
    the chunk instead of dropping it. Transient statuses are retried here; other
    4xx fast-fail, since retrying them only burns backoff.
    """
    delay = 1.0
    ctx = dict(trace_ctx or {})
    for attempt in range(max_attempts):
        try:
            results_api.create_results_v2(project_code, run_id, request)
            if trace:
                trace.event(
                    "results_v2_api_ok",
                    project_target=project_code,
                    target_run_id=run_id,
                    attempt=attempt + 1,
                    **ctx,
                )
            return True, 200
        except ApiExceptionV2 as e:
            http_status = getattr(e, "status", None)
            transient = http_status in (408, 429) or (
                isinstance(http_status, int) and 500 <= http_status <= 599
            )
            if transient and attempt < max_attempts - 1:
                time.sleep(delay)
                delay *= 2
                continue
            body = getattr(e, "body", None) or str(e)
            logger.error(
                "create_results_v2 failed run=%s: HTTP %s %s",
                run_id,
                http_status,
                (body[:800] if isinstance(body, str) else body),
            )
            if trace:
                payload = None
                req_results = getattr(request, "results", None)
                if full_payloads and req_results:
                    try:
                        payload = chunk_results_payload_for_trace(
                            list(req_results), full_payloads=True
                        )
                    except Exception:
                        payload = None
                trace.event(
                    "results_v2_api_failed",
                    project_target=project_code,
                    target_run_id=run_id,
                    http_status=http_status,
                    error_body=(body[:2000] if isinstance(body, str) else str(body)[:2000]),
                    payload_full=payload,
                    **ctx,
                )
            return False, http_status
        except Exception as e:
            logger.error("create_results_v2 unexpected error run=%s: %s", run_id, e, exc_info=True)
            if trace:
                trace.event(
                    "results_v2_api_error",
                    project_target=project_code,
                    target_run_id=run_id,
                    error=str(e),
                    **ctx,
                )
            return False, None
    return False, None


def _complete_target_run_safely(
    runs_api: RunsApi,
    project_code: str,
    run_id: int,
    max_attempts: int = 4,
    trace: Any = None,
    trace_extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Call complete_run without retry_with_backoff (avoids ERROR spam + swallowed failures on 4xx)."""
    delay = 1.0
    extra = dict(trace_extra or {})
    for attempt in range(max_attempts):
        try:
            runs_api.complete_run(code=project_code, id=run_id)
            step_log_info(
                logger,
                "complete_run ok project=%s run_id=%s",
                project_code,
                run_id,
            )
            if trace:
                trace.event(
                    "run_complete_ok",
                    project_target=project_code,
                    target_run_id=run_id,
                    **extra,
                )
            return
        except ApiException as e:
            http_status = getattr(e, "status", None)
            if http_status == 429 and attempt < max_attempts - 1:
                time.sleep(delay)
                delay *= 2
                continue
            body = getattr(e, "body", None) or getattr(e, "reason", None) or str(e)
            logger.warning(
                "complete_run failed project=%s run_id=%s HTTP %s %s",
                project_code,
                run_id,
                http_status,
                (body[:500] if isinstance(body, str) else body),
            )
            if trace:
                trace.event(
                    "run_complete_failed",
                    project_target=project_code,
                    target_run_id=run_id,
                    http_status=http_status,
                    error_body=(body[:1200] if isinstance(body, str) else str(body)[:1200]),
                    **extra,
                )
            return
        except Exception as e:
            logger.warning(
                "complete_run failed project=%s run_id=%s: %s",
                project_code,
                run_id,
                e,
            )
            if trace:
                trace.event(
                    "run_complete_failed",
                    project_target=project_code,
                    target_run_id=run_id,
                    error=str(e),
                    **extra,
                )
            return


def _result_rows_to_hash_set(rows: List[Any]) -> set:
    """Build a set of result hash strings from v1-style result rows."""
    out: set = set()
    for r in rows:
        d = r if isinstance(r, dict) else to_dict(r)
        h = d.get("hash") or d.get("result_hash")
        if h:
            out.add(str(h))
    return out


def _collect_run_result_hashes(
    target_service: QaseService,
    project_code: str,
    run_id: int,
) -> set:
    """Set of result hash strings currently on the target run (v1 list API)."""
    try:
        rows = extract_results(target_service, project_code, int(run_id))
        return _result_rows_to_hash_set(rows)
    except Exception as e:
        logger.debug("collect_run_result_hashes failed run=%s: %s", run_id, e)
        return set()


def _map_chunk_hashes_fallback(
    chunk_rows: List[Dict[str, Any]],
    target_results_after: List[Any],
    new_hashes: set,
    result_hash_mapping: Dict[str, str],
) -> None:
    used_th: set = set()
    for row in chunk_rows:
        sh = row["source"].get("hash")
        if not sh:
            continue
        rc = row["create"]
        tid = getattr(rc, "testops_id", None)
        tit = (getattr(rc, "title", None) or "").strip()
        for r in target_results_after:
            trd = r if isinstance(r, dict) else to_dict(r)
            th = trd.get("hash") or trd.get("result_hash")
            if not th or str(th) not in new_hashes or str(th) in used_th:
                continue
            tc = trd.get("case_id")
            if tid is not None and tc != tid:
                continue
            if tid is None:
                trti = _pick_str(trd, "title", "Title", "name", "Name")
                if tit and trti and tit != trti.strip():
                    continue
            used_th.add(str(th))
            result_hash_mapping[str(sh)] = str(th)
            break


def _map_chunk_hashes_by_delta(
    chunk_rows: List[Dict[str, Any]],
    target_results_after: List[Any],
    new_hashes: set,
    result_hash_mapping: Dict[str, str],
) -> None:
    """Pair each created row with its source hash using hashes added since the previous fetch."""
    ordered_new: List[str] = []
    for r in target_results_after:
        d = r if isinstance(r, dict) else to_dict(r)
        h = d.get("hash") or d.get("result_hash")
        if h and str(h) in new_hashes:
            ordered_new.append(str(h))
    n_chunk = len(chunk_rows)
    if len(ordered_new) > n_chunk:
        logger.debug(
            "Result hash delta: %s new hashes for chunk size %s; using trailing %s",
            len(ordered_new),
            n_chunk,
            n_chunk,
        )
        ordered_new = ordered_new[-n_chunk:]
    if len(ordered_new) != n_chunk:
        logger.debug(
            "Result hash delta count mismatch (chunk=%s, new_hashes=%s); using fallback matching.",
            n_chunk,
            len(ordered_new),
        )
        _map_chunk_hashes_fallback(
            chunk_rows, target_results_after, new_hashes, result_hash_mapping
        )
        return
    for row, th in zip(chunk_rows, ordered_new):
        sh = row["source"].get("hash")
        if sh and th:
            result_hash_mapping[str(sh)] = th


def _augment_runs_to_complete_from_source_details(
    source_service: QaseService,
    project_code_source: str,
    project_code_target: str,
    mappings: MigrationMappings,
) -> None:
    """
    List runs sometimes omit completion flags. For any target run not yet queued, GET source
    run detail and queue complete_run when the source run is finished.
    """
    run_m = mappings.runs.get(project_code_source) or {}
    if not run_m:
        return
    if not hasattr(mappings, "_runs_to_complete"):
        mappings._runs_to_complete = {}
    qc = mappings._runs_to_complete.setdefault(project_code_source, {})

    def _as_int(x: Any) -> Optional[int]:
        try:
            return int(x)
        except (TypeError, ValueError):
            return None

    queued = {tid for tid in (_as_int(k) for k in qc.keys()) if tid is not None}

    for src_raw, tgt_raw in run_m.items():
        src = _as_int(src_raw)
        tgt = _as_int(tgt_raw)
        if src is None or tgt is None or tgt in queued:
            continue
        detail = fetch_run_detail_json(source_service, project_code_source, src)
        if not detail or not _source_run_should_complete_after_results(detail):
            continue
        qc[tgt] = {
            "project_code": project_code_target,
            "is_completed": True,
            "source_is_completed": bool(
                detail.get("is_completed") or detail.get("is_complete")
            ),
            "has_end_time": bool(
                detail.get("end_time")
                or detail.get("time_end")
                or detail.get("completed_at")
            ),
        }
        queued.add(tgt)
        logger.debug(
            "Queued target run %s for complete_run (finished source run %s from GET detail)",
            tgt,
            src,
        )


def _build_batch_rows_for_source_run(
    source_results: List[Any],
    *,
    source_service: QaseService,
    project_code_source: str,
    source_run_id: int,
    target_run_id: int,
    case_mapping: Dict[int, int],
    mappings: MigrationMappings,
    author_uuid_to_id_mapping: Dict[str, int],
    attachment_mapping: Dict[str, str],
    case_steps_cache: Dict[int, Dict[str, Any]],
    cache_lock: Optional[threading.Lock],
    trace: Any,
    trace_full: bool,
    row_max_workers: int,
) -> List[Dict[str, Any]]:
    trace_lock = threading.Lock() if trace else None

    def _trace_event(name: str, **kwargs: Any) -> None:
        if not trace:
            return
        if trace_lock is not None:
            with trace_lock:
                trace.event(name, **kwargs)
        else:
            trace.event(name, **kwargs)

    def _process_one_raw(raw: Any) -> Optional[Dict[str, Any]]:
        result_dict = raw if isinstance(raw, dict) else to_dict(raw)
        raw_preview = {
            "case_id": result_dict.get("case_id"),
            "hash": result_dict.get("hash"),
            "status_id": result_dict.get("status_id"),
        }
        result_dict = enrich_source_result_for_v2(
            result_dict,
            source_service,
            project_code_source,
            case_steps_cache,
            cache_lock=cache_lock,
        )

        source_case_id = result_dict.get("case_id")
        lookup_id: Any = source_case_id
        if source_case_id is not None:
            try:
                lookup_id = int(source_case_id)
            except (TypeError, ValueError):
                pass
        target_case_id = case_mapping.get(lookup_id)
        if target_case_id is None and source_case_id is not None:
            target_case_id = case_mapping.get(source_case_id)

        if target_case_id is None:
            _trace_event(
                "result_standalone_no_testops_id",
                project_source=project_code_source,
                source_run_id=source_run_id,
                target_run_id=target_run_id,
                source_case_id=source_case_id,
                lookup_id=lookup_id,
                raw_preview=raw_preview,
                enriched_summary=summarize_enriched_result(result_dict),
            )

        try:
            rc = _source_to_result_create_v2(
                result_dict,
                target_case_id,
                mappings,
                author_uuid_to_id_mapping,
                attachment_mapping,
            )
            _trace_event(
                "result_row_built",
                project_source=project_code_source,
                source_run_id=source_run_id,
                target_run_id=target_run_id,
                source_case_id=source_case_id,
                target_case_id=target_case_id,
                raw_preview=raw_preview,
                enriched=summarize_enriched_result(result_dict),
                v2_create_summary=summarize_result_create(rc),
            )
            return {"source": result_dict, "create": rc}
        except Exception as e:
            logger.error("Error building v2 ResultCreate: %s", e, exc_info=True)
            _trace_event(
                "result_build_failed",
                project_source=project_code_source,
                source_run_id=source_run_id,
                target_run_id=target_run_id,
                source_case_id=source_case_id,
                error=str(e),
                enriched_summary=summarize_enriched_result(result_dict),
            )
            return None

    n = len(source_results)
    use_parallel = row_max_workers > 1 and n >= _RESULTS_ROW_PARALLEL_MIN
    if not use_parallel:
        out: List[Dict[str, Any]] = []
        for raw in source_results:
            row = _process_one_raw(raw)
            if row:
                out.append(row)
        return out

    workers = min(row_max_workers, n, _RESULTS_ROW_MAX_WORKERS)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_process_one_raw, raw) for raw in source_results]
        out = []
        for fut in futures:
            row = fut.result()
            if row:
                out.append(row)
    return out


def _migrate_results_one_source_run(
    source_run_id: int,
    target_run_id: int,
    *,
    source_service: QaseService,
    target_service: QaseService,
    project_code_source: str,
    project_code_target: str,
    case_mapping: Dict[int, int],
    mappings: MigrationMappings,
    author_uuid_to_id_mapping: Dict[str, int],
    attachment_mapping: Dict[str, str],
    results_api_v2: ResultsApi,
    case_steps_cache: Dict[int, Dict[str, Any]],
    cache_lock: Optional[threading.Lock],
    trace: Any,
    trace_full: bool,
    progress: Optional[Any],
    row_max_workers: int,
) -> Tuple[int, int, Dict[str, str]]:
    """Returns (raw_result_count, created_count, hash_mapping_for_this_run)."""
    local_hashes: Dict[str, str] = {}
    source_results = extract_results(
        source_service, project_code_source, source_run_id, strict=True
    )
    if not source_results:
        if trace:
            trace.event(
                "results_run_empty_extract",
                project_source=project_code_source,
                source_run_id=source_run_id,
                target_run_id=target_run_id,
            )
        return 0, 0, {}

    if trace:
        trace.event(
            "results_run_extracted",
            project_source=project_code_source,
            source_run_id=source_run_id,
            target_run_id=target_run_id,
            n_raw_results=len(source_results),
        )

    batch_rows = _build_batch_rows_for_source_run(
        source_results,
        source_service=source_service,
        project_code_source=project_code_source,
        source_run_id=source_run_id,
        target_run_id=target_run_id,
        case_mapping=case_mapping,
        mappings=mappings,
        author_uuid_to_id_mapping=author_uuid_to_id_mapping,
        attachment_mapping=attachment_mapping,
        case_steps_cache=case_steps_cache,
        cache_lock=cache_lock,
        trace=trace,
        trace_full=trace_full,
        row_max_workers=row_max_workers,
    )

    if not batch_rows:
        if trace:
            trace.event(
                "results_run_no_rows_to_send",
                project_source=project_code_source,
                source_run_id=source_run_id,
                target_run_id=target_run_id,
                n_raw_results=len(source_results),
            )
        return len(source_results), 0, {}

    seen_target_hashes = _collect_run_result_hashes(
        target_service, project_code_target, int(target_run_id)
    )
    created = 0

    def _send_chunk(chunk_rows: List[Dict[str, Any]], chunk_label: str) -> None:
        """
        Send one request of results, then map the hashes it created.

        A deterministic rejection of a multi-result request is halved and
        retried rather than dropped: the API cap can change under us, and one
        unacceptable row should cost one result, not every result sent with it.
        """
        nonlocal created, seen_target_hashes

        if not chunk_rows:
            return

        results_list: List[ResultCreate] = [r["create"] for r in chunk_rows]
        request = CreateResultsRequestV2(results=results_list)

        if trace:
            trace.event(
                "results_chunk_payload",
                project_source=project_code_source,
                source_run_id=source_run_id,
                target_run_id=target_run_id,
                chunk_index=chunk_label,
                chunk_size=len(chunk_rows),
                payload=chunk_results_payload_for_trace(results_list, trace_full),
            )

        ok, http_status = _create_results_v2_with_retry(
            results_api_v2,
            project_code_target,
            int(target_run_id),
            request,
            trace=trace,
            trace_ctx={
                "project_source": project_code_source,
                "source_run_id": source_run_id,
                "chunk_index": chunk_label,
                "chunk_size": len(chunk_rows),
            },
            full_payloads=trace_full,
        )

        if ok:
            created += len(chunk_rows)
            if progress:
                progress.add_results(len(chunk_rows))
            try:
                after_hashes: set = set()
                new_hashes: set = set()
                delay_s = 0.0
                for attempt in range(6):
                    if delay_s > 0:
                        time.sleep(delay_s)
                    after_hashes = _collect_run_result_hashes(
                        target_service, project_code_target, int(target_run_id)
                    )
                    new_hashes = after_hashes - seen_target_hashes
                    if new_hashes or len(chunk_rows) == 0 or attempt == 5:
                        break
                    delay_s = 0.5 if attempt == 0 else min(0.5 * (2**attempt), 3.0)

                target_results = extract_results(
                    target_service, project_code_target, int(target_run_id)
                )
                _map_chunk_hashes_by_delta(
                    chunk_rows,
                    target_results,
                    new_hashes,
                    local_hashes,
                )
                seen_target_hashes = after_hashes
            except Exception as hash_error:
                logger.warning(
                    "Could not map result hashes after v2 bulk run %s: %s",
                    target_run_id,
                    hash_error,
                )
            return

        deterministic = (
            isinstance(http_status, int)
            and 400 <= http_status < 500
            and http_status not in (408, 429)
        )
        if deterministic and len(chunk_rows) > 1:
            mid = len(chunk_rows) // 2
            logger.warning(
                "V2 results bulk rejected with HTTP %s for run %s (%s results); "
                "splitting into %s + %s and retrying",
                http_status,
                target_run_id,
                len(chunk_rows),
                mid,
                len(chunk_rows) - mid,
            )
            _send_chunk(chunk_rows[:mid], f"{chunk_label}a")
            _send_chunk(chunk_rows[mid:], f"{chunk_label}b")
            return

        logger.error(
            "V2 results bulk failed for run %s (HTTP %s): %s result(s) in chunk %s "
            "were not migrated",
            target_run_id,
            http_status,
            len(chunk_rows),
            chunk_label,
        )
        if trace:
            trace.event(
                "results_chunk_skipped_after_fail",
                project_source=project_code_source,
                source_run_id=source_run_id,
                target_run_id=target_run_id,
                chunk_index=chunk_label,
                chunk_size=len(chunk_rows),
                http_status=http_status,
            )

    for chunk_idx, chunk_rows in enumerate(
        chunks(batch_rows, MAX_RESULTS_PER_REQUEST)
    ):
        _send_chunk(chunk_rows, str(chunk_idx))

    return len(source_results), created, local_hashes


def migrate_results(
    source_service: QaseService,
    target_service: QaseService,
    project_code_source: str,
    project_code_target: str,
    run_mapping: Dict[int, int],
    case_mapping: Dict[int, int],
    mappings: MigrationMappings,
    stats: MigrationStats,
    progress: Optional["ProjectMigrationProgress"] = None,
) -> Dict[str, str]:
    """
    Migrate test results via API v2 bulk create, then map hashes using v1 list API.

    Uses thread pools to overlap I/O: per-result enrichment (detail + case meta GETs)
    in parallel within each run, and up to ``_RESULTS_RUN_PARALLEL_MAX`` runs at once
    when the run mapping has multiple entries. Chunk POST + hash polling stay sequential
    per target run to preserve ordering.
    """
    runs_api_target = RunsApi(target_service.client)
    result_hash_mapping: Dict[str, str] = {}

    author_uuid_to_id_mapping = extract_authors(source_service)
    mappings.author_uuid_to_id_mapping = author_uuid_to_id_mapping

    attachment_mapping: Dict[str, str] = {}
    if project_code_source in mappings.attachments:
        attachment_mapping = dict(mappings.attachments[project_code_source])
        normalized_mapping: Dict[str, str] = {}
        for key, value in attachment_mapping.items():
            normalized_mapping[str(key).lower()] = value
            normalized_mapping[str(key)] = value
        attachment_mapping = normalized_mapping

    results_api_v2 = ResultsApi(target_service.client_v2)

    total_results = 0
    created_results = 0
    case_steps_cache: Dict[int, Dict[str, Any]] = {}
    trace = getattr(mappings, "trace", None)
    trace_full = bool(getattr(trace, "full_payloads", False)) if trace else False

    if trace:
        trace.event(
            "results_phase_start",
            project_source=project_code_source,
            project_target=project_code_target,
            n_runs_in_mapping=len(run_mapping),
            attachment_mapping_size=len(attachment_mapping),
        )

    # Runs whose results a previous run already sent are skipped, so resuming
    # does not post the same results twice.
    done_runs = mappings.results_done.setdefault(project_code_source, [])
    done_set = {int(r) for r in done_runs}
    run_items = [pair for pair in run_mapping.items() if int(pair[0]) not in done_set]
    skipped_runs = len(run_mapping) - len(run_items)
    if skipped_runs:
        logger.info(
            "%s: results for %s run(s) were already migrated by a previous run, skipping them",
            project_code_source, skipped_runs,
        )
    n_runs = len(run_items)
    run_workers = (
        min(_RESULTS_RUN_PARALLEL_MAX, n_runs) if n_runs > 1 else 1
    )
    cpu_n = os.cpu_count() or 4
    if run_workers == 1:
        row_max_workers = min(
            _RESULTS_ROW_MAX_WORKERS,
            max(_RESULTS_ROW_MIN_WORKERS, cpu_n * 4),
        )
    else:
        row_max_workers = max(
            _RESULTS_ROW_MIN_WORKERS,
            min(
                _RESULTS_ROW_MAX_WORKERS,
                max(_RESULTS_ROW_MIN_WORKERS, (cpu_n * 8) // run_workers),
            ),
        )

    cache_lock: Optional[threading.Lock] = None
    if run_workers > 1 or row_max_workers > 1:
        cache_lock = threading.Lock()

    done_lock = threading.Lock()

    def _run_one(pair: Tuple[Any, Any]) -> Tuple[int, int, Dict[str, str]]:
        s_raw, t_raw = pair
        set_current_project(project_code_source)
        try:
            outcome = _migrate_results_one_source_run(
                int(s_raw),
                int(t_raw),
                source_service=source_service,
                target_service=target_service,
                project_code_source=project_code_source,
                project_code_target=project_code_target,
                case_mapping=case_mapping,
                mappings=mappings,
                author_uuid_to_id_mapping=author_uuid_to_id_mapping,
                attachment_mapping=attachment_mapping,
                results_api_v2=results_api_v2,
                case_steps_cache=case_steps_cache,
                cache_lock=cache_lock,
                trace=trace,
                trace_full=trace_full,
                progress=progress,
                row_max_workers=row_max_workers,
            )
        except ResultsFetchError as e:
            # Nothing was posted for this run, and it is not marked done, so a
            # resumed run retries it instead of treating it as empty.
            logger.error(
                "Results for run %s were not migrated because the source could not be read (%s). "
                "Re-run with --resume to retry them.", s_raw, e,
            )
            return 0, 0, {}
        with done_lock:
            done_runs.append(int(s_raw))
        return outcome

    if run_workers == 1:
        for pair in run_items:
            ts, cs, hm = _run_one(pair)
            total_results += ts
            created_results += cs
            result_hash_mapping.update(hm)
    else:
        with ThreadPoolExecutor(max_workers=run_workers) as run_pool:
            for ts, cs, hm in run_pool.map(_run_one, run_items):
                total_results += ts
                created_results += cs
                result_hash_mapping.update(hm)

    stats.add_entity("results", total_results, created_results)

    _augment_runs_to_complete_from_source_details(
        source_service,
        project_code_source,
        project_code_target,
        mappings,
    )

    if hasattr(mappings, "_runs_to_complete") and project_code_source in mappings._runs_to_complete:
        runs_to_complete = mappings._runs_to_complete[project_code_source]
        if trace:
            trace.event(
                "results_complete_run_phase_start",
                project_source=project_code_source,
                n_runs_to_complete=len(runs_to_complete),
                target_run_ids=list(runs_to_complete.keys()),
            )
        for target_run_id, run_info in runs_to_complete.items():
            if run_info.get("is_completed"):
                _complete_target_run_safely(
                    runs_api_target,
                    str(run_info["project_code"]),
                    int(target_run_id),
                    trace=trace,
                    trace_extra={
                        "project_source": project_code_source,
                        "source_flags": {
                            "source_is_completed": run_info.get("source_is_completed"),
                            "has_end_time": run_info.get("has_end_time"),
                        },
                    },
                )

    if trace:
        trace.event(
            "results_phase_end",
            project_source=project_code_source,
            total_results_seen=total_results,
            created_results=created_results,
        )

    if project_code_source not in mappings.result_hashes:
        mappings.result_hashes[project_code_source] = {}
    mappings.result_hashes[project_code_source].update(result_hash_mapping)

    return result_hash_mapping
