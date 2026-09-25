"""
Per-project migration progress (tqdm) and lightweight source totals prefetch.

Totals are estimated before heavy migration steps so progress bars can reflect
cases / runs / results units. Run list is fetched once and reused by migrate_runs.
"""
from __future__ import annotations

import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

from migration import qase_http
from qase.api_client_v1.api.cases_api import CasesApi
from tqdm import tqdm

from qase_service import QaseService
from migration.extract.runs import extract_runs
from migration.utils import retry_with_backoff, extract_entities_from_response, to_dict

logger = logging.getLogger(__name__)

_MAX_RESULT_COUNT_WORKERS = 32


def stderr_supports_progress() -> bool:
    return bool(sys.stderr.isatty())


def _v1_base_and_token(source_service: QaseService) -> Tuple[str, Optional[str]]:
    try:
        base_url = source_service.client.configuration.host
        api_key_dict = source_service.client.configuration.api_key
        if isinstance(api_key_dict, dict):
            token = (
                api_key_dict.get("TokenAuth")
                or api_key_dict.get("Token")
                or api_key_dict.get("token")
            )
        else:
            token = None
    except Exception:
        return "", None
    if not token or not base_url:
        return "", None
    api_base = base_url.rstrip("/")
    if not api_base.endswith("/v1"):
        api_base = f"{api_base}/v1"
    return api_base, token


def fetch_cases_total(source_service: QaseService, project_code: str) -> int:
    """Total test cases in project from list API (limit=1)."""
    try:
        cases_api = CasesApi(source_service.client)
        resp = retry_with_backoff(
            cases_api.get_cases, code=project_code, limit=1, offset=0
        )
        if not resp or not getattr(resp, "result", None):
            return 0
        res = resp.result
        if hasattr(res, "total") and getattr(res, "total", None) is not None:
            try:
                return max(0, int(res.total))
            except (TypeError, ValueError):
                pass
        rd = to_dict(res)
        t = rd.get("total")
        if t is not None:
            try:
                return max(0, int(t))
            except (TypeError, ValueError):
                pass
        ents = extract_entities_from_response(resp) or []
        return len(ents)
    except Exception as e:
        logger.debug("fetch_cases_total failed %s: %s", project_code, e)
        return 0


def infer_results_count_from_run_dict(run_dict: Dict[str, Any]) -> Optional[int]:
    """Best-effort result count from run list payload (no extra HTTP)."""
    st = run_dict.get("stats") or run_dict.get("counters") or run_dict.get("statistics")
    if isinstance(st, dict):
        for k in ("total", "all", "count", "results"):
            v = st.get(k)
            if isinstance(v, int) and v >= 0:
                return v
        parts = (
            "passed",
            "failed",
            "skipped",
            "blocked",
            "invalid",
            "untested",
            "pending",
            "in_progress",
            "retest",
        )
        s = sum(int(st.get(p, 0) or 0) for p in parts)
        if s > 0:
            return s
    for k in ("results_count", "tests_count", "cases_count", "total_tests"):
        v = run_dict.get(k)
        if isinstance(v, int) and v >= 0:
            return v
    cases = run_dict.get("cases")
    if isinstance(cases, list) and cases:
        return len(cases)
    return None


def fetch_result_total_for_run(
    source_service: QaseService, project_code: str, run_id: int
) -> int:
    """GET /v1/result/{code}?run=&limit=1 — read total from result envelope."""
    api_base, token = _v1_base_and_token(source_service)
    if not api_base or not token:
        return 0
    url = f"{api_base}/result/{project_code}"
    headers = {"Token": token, "accept": "application/json"}
    params = {"run": str(int(run_id)), "limit": 1, "offset": 0}
    try:
        r = qase_http.get(url, headers=headers, params=params, timeout=30)
        if r.status_code != 200:
            return 0
        data = r.json()
        if not data.get("status") or not data.get("result"):
            return 0
        result = data["result"]
        if isinstance(result, dict):
            t = result.get("total")
            if t is not None:
                try:
                    return max(0, int(t))
                except (TypeError, ValueError):
                    pass
            entities = result.get("entities") or []
            return len(entities) if entities else 0
        return 0
    except Exception as e:
        logger.debug("fetch_result_total_for_run %s/%s: %s", project_code, run_id, e)
        return 0


@dataclass
class ProjectPrefetchProfile:
    cases_total: int
    runs_total: int
    results_total: int
    source_runs: List[Dict[str, Any]]


def prefetch_project_migration_profile(
    source_service: QaseService, project_code: str
) -> ProjectPrefetchProfile:
    """
    Cases total (1 list call), full run list (same as extract_runs), results total
    (inferred from run rows with parallel lite API for unknowns).
    """
    cases_total = fetch_cases_total(source_service, project_code)
    source_runs = extract_runs(source_service, project_code)
    runs_total = len(source_runs)

    known = 0
    unknown_ids: List[int] = []
    for run in source_runs:
        rid = run.get("id")
        try:
            rid_int = int(rid) if rid is not None else None
        except (TypeError, ValueError):
            rid_int = None
        n = infer_results_count_from_run_dict(run if isinstance(run, dict) else to_dict(run))
        if n is not None:
            known += max(0, n)
        elif rid_int is not None:
            unknown_ids.append(rid_int)

    extra = 0
    if unknown_ids:
        workers = min(_MAX_RESULT_COUNT_WORKERS, max(1, len(unknown_ids)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(
                    fetch_result_total_for_run, source_service, project_code, u
                ): u
                for u in unknown_ids
            }
            for fut in as_completed(futs):
                try:
                    extra += max(0, int(fut.result()))
                except Exception:
                    pass

    results_total = known + extra
    return ProjectPrefetchProfile(
        cases_total=max(cases_total, 0),
        runs_total=runs_total,
        results_total=results_total,
        source_runs=source_runs,
    )


class ProjectMigrationProgress:
    """
    One tqdm bar: cases + runs + results units. Thread-safe updates for parallel workers.
    """

    def __init__(
        self,
        project_code: str,
        cases_cap: int,
        runs_cap: int,
        results_cap: int,
        position: int = 0,
    ):
        self._label = project_code
        self._lock = threading.Lock()
        self._cases_cap = max(0, cases_cap)
        self._runs_cap = max(0, runs_cap)
        self._results_cap = max(0, results_cap)
        self._cases_done = 0
        self._runs_done = 0
        self._results_done = 0
        total_units = max(1, self._cases_cap + self._runs_cap + self._results_cap)
        self._pbar = tqdm(
            total=total_units,
            desc=self._format_desc(),
            unit="it",
            unit_scale=False,
            file=sys.stderr,
            dynamic_ncols=True,
            position=position,
            leave=True,
            mininterval=0.15,
            smoothing=0.1,
        )

    def _format_desc(self) -> str:
        return (
            f"{self._label}  "
            f"cases {self._cases_done}/{self._cases_cap} · "
            f"runs {self._runs_done}/{self._runs_cap} · "
            f"results {self._results_done}/{self._results_cap}"
        )

    def _refresh_desc(self) -> None:
        self._pbar.set_description_str(self._format_desc(), refresh=False)

    def reconcile_case_cap(self, actual_extracted: int) -> None:
        """Expand bar if extract_cases count exceeds API total hint."""
        if actual_extracted <= self._cases_cap:
            return
        with self._lock:
            delta = actual_extracted - self._cases_cap
            self._cases_cap = actual_extracted
            self._pbar.total += delta
            self._refresh_desc()
            self._pbar.refresh()

    def add_cases(self, n: int) -> None:
        if n <= 0:
            return
        with self._lock:
            self._cases_done += n
            self._pbar.update(n)
            self._refresh_desc()

    def add_runs(self, n: int) -> None:
        if n <= 0:
            return
        with self._lock:
            self._runs_done += n
            self._pbar.update(n)
            self._refresh_desc()

    def add_results(self, n: int) -> None:
        if n <= 0:
            return
        with self._lock:
            self._results_done += n
            self._pbar.update(n)
            self._refresh_desc()

    def close(self) -> None:
        with self._lock:
            try:
                self._pbar.close()
            except Exception:
                pass


def init_tqdm_lock() -> None:
    """Call once before parallel workers use tqdm bars."""
    try:
        tqdm.set_lock(threading.RLock())
    except AttributeError:
        pass
