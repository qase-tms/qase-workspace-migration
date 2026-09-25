"""
JSON Lines trace log for migration debugging (extract vs target payloads).

Each line is one JSON object with an "event" field. Default file: migration_trace.jsonl
Configure via config.json options.migration_trace_file (use null to disable).
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, date
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set


class MigrationTrace:
    """Thread-safe append-only JSONL trace."""

    def __init__(self, path: str, full_payloads: bool = False) -> None:
        self.path = path
        self.full_payloads = full_payloads
        self._fh = open(path, "a", encoding="utf-8")
        self._lock = threading.Lock()

    def event(self, event: str, **fields: Any) -> None:
        rec: Dict[str, Any] = {"event": event, "ts": datetime.utcnow().isoformat() + "Z"}
        for k, v in fields.items():
            if v is not None:
                rec[k] = _json_safe(v, deep=True)
        line = json.dumps(rec, ensure_ascii=False, default=_fallback_json)
        with self._lock:
            fh = self._fh
            if fh is None:
                return
            try:
                fh.write(line + "\n")
                fh.flush()
            except (ValueError, OSError):
                # Closed handle or I/O error (e.g. interrupt during migration)
                return

    def close(self) -> None:
        with self._lock:
            if self._fh:
                self._fh.close()
                self._fh = None


def _fallback_json(o: Any) -> Any:
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, bytes):
        return f"<bytes len={len(o)}>"
    return str(o)


def _json_safe(obj: Any, deep: bool = True, _depth: int = 0, _seen: Optional[Set[int]] = None) -> Any:
    if _seen is None:
        _seen = set()
    max_depth = 12
    max_str = 8000
    max_list = 200

    if _depth > max_depth:
        return "<max depth>"

    if obj is None or isinstance(obj, (bool, int, float)):
        return obj

    if isinstance(obj, str):
        if len(obj) > max_str:
            return obj[:max_str] + f"...<truncated {len(obj) - max_str} chars>"
        return obj

    oid = id(obj)
    if oid in _seen:
        return "<cycle>"
    if isinstance(obj, dict):
        _seen.add(oid)
        try:
            out: Dict[str, Any] = {}
            keys = list(obj.keys())
            for i, k in enumerate(keys[:80]):
                ks = str(k)
                v = obj[k]
                if deep and isinstance(v, (dict, list)):
                    out[ks] = _json_safe(v, deep=True, _depth=_depth + 1, _seen=_seen)
                elif isinstance(v, str) and len(v) > max_str:
                    out[ks] = v[:max_str] + "..."
                else:
                    out[ks] = _json_safe(v, deep=False, _depth=_depth + 1, _seen=_seen) if deep else v
            if len(keys) > 80:
                out["_truncated_keys"] = len(keys) - 80
            return out
        finally:
            _seen.discard(oid)

    if isinstance(obj, (list, tuple)):
        _seen.add(oid)
        try:
            lst = list(obj)[:max_list]
            return [_json_safe(x, deep=deep, _depth=_depth + 1, _seen=_seen) for x in lst]
        finally:
            _seen.discard(oid)

    if hasattr(obj, "model_dump"):
        try:
            return _json_safe(obj.model_dump(by_alias=True, exclude_none=True), deep=deep, _depth=_depth + 1, _seen=_seen)
        except Exception:
            pass

    return str(obj)[:max_str]


def summarize_source_run(run_dict: Dict[str, Any]) -> Dict[str, Any]:
    d = run_dict
    cases = d.get("cases")
    n_cases = len(cases) if isinstance(cases, list) else None
    return {
        "source_run_id": d.get("id"),
        "title": (d.get("title") or "")[:200],
        "description_len": len(str(d.get("description") or "")),
        "user_id": d.get("user_id"),
        "is_completed": d.get("is_completed"),
        "is_complete": d.get("is_complete"),
        "state": d.get("state"),
        "status": d.get("status"),
        "start_time": d.get("start_time"),
        "end_time": d.get("end_time"),
        "plan_id": d.get("plan_id"),
        "milestone_id": d.get("milestone_id"),
        "milestone_title": (d.get("milestone") or {}).get("title") if isinstance(d.get("milestone"), dict) else None,
        "n_cases_on_run": n_cases,
        "top_level_keys": sorted(d.keys())[:50],
    }


def summarize_enriched_result(result_dict: Dict[str, Any]) -> Dict[str, Any]:
    rd = result_dict
    att = rd.get("attachments")
    steps = rd.get("steps")
    ex = rd.get("execution")
    return {
        "source_case_id": rd.get("case_id"),
        "hash": rd.get("hash"),
        "status_id": rd.get("status_id"),
        "title": (rd.get("title") or "")[:200],
        "n_steps": len(steps) if isinstance(steps, list) else 0,
        "n_attachments_coalesced": len(att) if isinstance(att, list) else 0,
        "n_files": len(rd.get("files") or []) if isinstance(rd.get("files"), list) else 0,
        "n_screenshots": len(rd.get("screenshots") or []) if isinstance(rd.get("screenshots"), list) else 0,
        "has_execution_block": isinstance(ex, dict),
        "comment_len": len(str(rd.get("comment") or "")),
        "message_len": len(str(rd.get("message") or "")),
        "time_spent_ms": rd.get("time_spent_ms"),
        "sample_keys": sorted(rd.keys())[:45],
    }


def summarize_result_create(rc: Any) -> Dict[str, Any]:
    if hasattr(rc, "model_dump"):
        try:
            d = rc.model_dump(by_alias=True, exclude_none=True)
        except Exception:
            d = {}
    else:
        d = {}
    steps = d.get("steps") or []
    att = d.get("attachments") or []
    ex = d.get("execution") or {}
    return {
        "testops_id": d.get("testops_id"),
        "title": (d.get("title") or "")[:200],
        "n_step_roots": len(steps) if isinstance(steps, list) else 0,
        "n_attachments": len(att) if isinstance(att, list) else 0,
        "execution_status": ex.get("status") if isinstance(ex, dict) else None,
        "execution_duration_ms": ex.get("duration") if isinstance(ex, dict) else None,
        "message_len": len(str(d.get("message") or "")),
    }


def chunk_results_payload_for_trace(
    results_list: List[Any], full_payloads: bool
) -> Dict[str, Any]:
    summaries = [summarize_result_create(r) for r in results_list]
    out: Dict[str, Any] = {"count": len(results_list), "items": summaries}
    if full_payloads:
        out["full_results"] = _json_safe(
            [r.model_dump(by_alias=True, exclude_none=True) for r in results_list if hasattr(r, "model_dump")],
            deep=True,
        )
    return out
