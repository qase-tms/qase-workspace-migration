"""
Migration utilities - Helper functions for data processing, ID mapping, and error handling.
"""
import json
import logging
import threading
import time
import hashlib
import uuid
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime
from qase.api_client_v1.exceptions import ApiException
import requests

from migration import qase_http


logger = logging.getLogger(__name__)

# Bulk POSTs (cases, etc.) can exceed 60s server-side on api.qase.io.
_QASE_RAW_BULK_TIMEOUT = (30.0, 180.0)  # (connect, read) seconds


class MigrationMappings:
    """Stores mappings between source and target entity IDs."""
    
    def __init__(self):
        self.projects = {}
        self.suites = {}
        self.cases = {}
        self.runs = {}
        self.milestones = {}
        self.configurations = {}
        self.configuration_groups = {}
        self.environments = {}
        self.shared_steps = {}
        self.shared_parameters = {}
        self.custom_fields = {}
        self.users = {}
        self.user_email_mapping = {}  # email -> target_user_id mapping
        self.user_uuid_mapping = {}  # source_user_uuid -> target_user_id mapping
        self.author_uuid_to_id_mapping = {}  # source_author_uuid -> source_author_id mapping
        self.attachments = {}
        self.plans = {}
        self.defects = {}
        self.result_hashes = {}
        self.results_done = {}  # project -> source run ids whose results were sent
        self.target_workspace_hash = None
        # Target user id for anything whose source author has no mapping. Set from
        # users.default at startup; not persisted in the mappings file.
        self.default_user_id = 1
        # Optional migration.trace_log.MigrationTrace — not persisted in mappings JSON
        self.trace = None

    def save_to_file(self, filepath: str):
        """Save mappings to JSON file."""
        mappings_dict = {
            'projects': self.projects,
            'suites': self.suites,
            'cases': self.cases,
            'runs': self.runs,
            'milestones': self.milestones,
            'configurations': self.configurations,
            'configuration_groups': self.configuration_groups,
            'environments': self.environments,
            'shared_steps': self.shared_steps,
            'shared_parameters': self.shared_parameters,
            'custom_fields': self.custom_fields,
            'users': self.users,
            'user_email_mapping': getattr(self, 'user_email_mapping', {}),
            'user_uuid_mapping': getattr(self, 'user_uuid_mapping', {}),
            'author_uuid_to_id_mapping': getattr(self, 'author_uuid_to_id_mapping', {}),
            'attachments': self.attachments,
            'plans': self.plans,
            'defects': getattr(self, 'defects', {}),
            'result_hashes': getattr(self, 'result_hashes', {}),
            'results_done': self.results_done,
            'target_workspace_hash': self.target_workspace_hash,
        }
        with open(filepath, 'w') as f:
            json.dump(mappings_dict, f, indent=2)
    
    def get_user_id(self, id: int, default_user_id: Optional[int] = None) -> int:
        """
        Get target user ID from source user ID.

        Maps a source workspace user ID to the target workspace user ID, falling
        back to ``default_user_id`` or, when that is not given, ``users.default``.
        """
        if id in self.users:
            return self.users[id]
        return self.default_user_id if default_user_id is None else default_user_id
    
    def load_from_file(self, filepath: str):
        """Load mappings from JSON file."""
        try:
            with open(filepath, 'r') as f:
                mappings_dict = json.load(f)
            self.projects = mappings_dict.get('projects', {})
            self.suites = mappings_dict.get('suites', {})
            self.cases = mappings_dict.get('cases', {})
            self.runs = mappings_dict.get('runs', {})
            self.milestones = mappings_dict.get('milestones', {})
            self.configurations = mappings_dict.get('configurations', {})
            self.configuration_groups = mappings_dict.get('configuration_groups', {})
            self.environments = mappings_dict.get('environments', {})
            self.shared_steps = mappings_dict.get('shared_steps', {})
            self.shared_parameters = mappings_dict.get('shared_parameters', {})
            self.custom_fields = mappings_dict.get('custom_fields', {})
            # Convert user mapping keys from strings to integers (JSON stores keys as strings)
            users_dict = mappings_dict.get('users', {})
            self.users = {int(k): v for k, v in users_dict.items() if k} if users_dict else {}
            self.user_email_mapping = mappings_dict.get('user_email_mapping', {})
            self.user_uuid_mapping = mappings_dict.get('user_uuid_mapping', {})
            self.author_uuid_to_id_mapping = mappings_dict.get('author_uuid_to_id_mapping', {})
            self.attachments = mappings_dict.get('attachments', {})
            self.plans = mappings_dict.get('plans', {})
            self.defects = mappings_dict.get('defects', {})
            self.result_hashes = mappings_dict.get('result_hashes', {})
            self.results_done = mappings_dict.get('results_done', {})
            self.target_workspace_hash = mappings_dict.get('target_workspace_hash')
        except FileNotFoundError:
            return
        # JSON object keys are always strings, but every step compares source ids
        # as integers. Without this, "already migrated" checks never match on a
        # resumed run and everything is created a second time.
        for attr in PROJECT_ID_KEYED_ATTRS:
            per_project = getattr(self, attr) or {}
            setattr(self, attr, {
                project: {_int_key(k): v for k, v in (ids or {}).items()}
                for project, ids in per_project.items()
            })
        self.custom_fields = {_int_key(k): v for k, v in (self.custom_fields or {}).items()}


# Per-project mappings keyed by numeric source ids (shared steps, attachments
# and result hashes are keyed by hash strings and are left alone).
PROJECT_ID_KEYED_ATTRS = (
    "suites",
    "cases",
    "runs",
    "milestones",
    "configurations",
    "configuration_groups",
    "environments",
    "plans",
    "defects",
)


def _int_key(key: Any) -> Any:
    """``"12"`` -> ``12``; anything that is not a plain integer string is kept."""
    if isinstance(key, str) and key.isdigit():
        return int(key)
    return key


class MigrationStats:
    """Tracks migration statistics (thread-safe for parallel project workers)."""

    def __init__(self):
        self.entities_processed = {}
        self.entities_created = {}
        self.entities_reused = {}
        self.errors = []
        # Every warning and error logged during the run, captured by the
        # ReportHandler in run_report so the report cannot drift from the log.
        self.issues = []
        self._lock = threading.Lock()

    def add_entity(self, entity_type: str, source_count: int, target_count: int):
        """Record entity migration stats. Accumulates counts across multiple calls."""
        with self._lock:
            if entity_type in self.entities_processed:
                self.entities_processed[entity_type] += source_count
                self.entities_created[entity_type] += target_count
            else:
                self.entities_processed[entity_type] = source_count
                self.entities_created[entity_type] = target_count

    def add_reused(self, entity_type: str, count: int):
        """Record entities taken from a previous run's mappings instead of created."""
        if not count:
            return
        with self._lock:
            self.entities_reused[entity_type] = self.entities_reused.get(entity_type, 0) + count

    def add_error(self, entity_type: str, error: str):
        """Record an error."""
        with self._lock:
            self.errors.append(
                {
                    "entity_type": entity_type,
                    "error": error,
                    "timestamp": datetime.now().isoformat(),
                }
            )

    def add_issue(self, level: str, source: str, message: str):
        """Record a warning or error for the end-of-run report."""
        with self._lock:
            self.issues.append({"level": level, "source": source, "message": message})

    def print_summary(self, dry_run: bool = False):
        """Print migration summary."""
        verb = "would be created" if dry_run else "migrated"
        print("\n" + "="*60)
        print("DRY RUN: WHAT A REAL RUN WOULD DO" if dry_run else "MIGRATION SUMMARY")
        print("="*60)
        for entity_type in self.entities_processed:
            processed = self.entities_processed[entity_type]
            created = self.entities_created.get(entity_type, 0)
            reused = self.entities_reused.get(entity_type, 0)
            line = f"{entity_type:30s}: {created}/{processed} {verb}"
            if reused:
                line += f" ({reused} already migrated by a previous run)"
            print(line)
        print(f"\nTotal Errors: {len(self.errors)}")
        if self.errors:
            print("\nErrors:")
            for error in self.errors[:10]:
                print(f"  - {error['entity_type']}: {error['error']}")
            if len(self.errors) > 10:
                print(f"  ... and {len(self.errors) - 10} more, all listed in the run report")
        print("="*60 + "\n")


# Per-project keys merged back into main mappings after parallel workers finish.
PARALLEL_PROJECT_MAPPING_ATTRS = (
    "milestones",
    "configurations",
    "configuration_groups",
    "environments",
    "shared_steps",
    "suites",
    "cases",
    "plans",
    "runs",
    "result_hashes",
    "results_done",
    "defects",
)


def fork_mappings_for_parallel_project(base: "MigrationMappings") -> "MigrationMappings":
    """
    Fork for a parallel worker: share read-only workspace-level dicts, and copy
    the project-specific buckets so a resumed run can see what a previous run
    already migrated. The copies are per project, so the worker never mutates a
    dict the main thread may be saving at the same time.
    """
    m = MigrationMappings()
    for attr in PARALLEL_PROJECT_MAPPING_ATTRS:
        setattr(m, attr, {
            project: (list(ids) if isinstance(ids, list) else dict(ids))
            for project, ids in (getattr(base, attr) or {}).items()
        })
    m.users = base.users
    m.user_email_mapping = base.user_email_mapping
    m.user_uuid_mapping = base.user_uuid_mapping
    m.author_uuid_to_id_mapping = base.author_uuid_to_id_mapping
    m.custom_fields = base.custom_fields
    m.shared_parameters = base.shared_parameters
    m.attachments = base.attachments
    m.projects = base.projects
    m.target_workspace_hash = base.target_workspace_hash
    m.default_user_id = base.default_user_id
    m.trace = base.trace
    return m


def merge_parallel_project_into_main(
    main: "MigrationMappings", worker: "MigrationMappings", project_source: str
) -> None:
    """Copy this project's mapping slices from worker into main (main thread)."""
    for attr in PARALLEL_PROJECT_MAPPING_ATTRS:
        wmap = getattr(worker, attr)
        if project_source in wmap:
            getattr(main, attr)[project_source] = wmap[project_source]


def merge_migration_stats(dst: "MigrationStats", src: "MigrationStats") -> None:
    """Accumulate worker stats into dst (call from main thread after each worker)."""
    for et, proc in src.entities_processed.items():
        cre = src.entities_created.get(et, 0)
        dst.add_entity(et, proc, cre)
    for err in src.errors:
        dst.add_error(err["entity_type"], err["error"])
    for et, n in src.entities_reused.items():
        dst.add_reused(et, n)


MAX_SAFE_ID = 2**31 - 1


def preserve_or_hash_id(original_id: int, preserve_ids: bool = True) -> int:
    """
    Preserve ID if within int32 range, otherwise hash it.
    
    Args:
        original_id: Original entity ID
        preserve_ids: Whether to preserve IDs when possible
    
    Returns:
        Preserved or hashed ID
    """
    if original_id <= MAX_SAFE_ID:
        if preserve_ids:
            return original_id
        else:
            return int(time.time() * 1000) % MAX_SAFE_ID
    else:
        hashed = int(hashlib.md5(str(original_id).encode()).hexdigest()[:8], 16)
        return hashed % MAX_SAFE_ID


def safe_api_call(api_method, *args, **kwargs):
    """
    Safely call API method with error handling.
    
    Args:
        api_method: API method to call
        *args: Positional arguments
        **kwargs: Keyword arguments
    
    Returns:
        Result object or None on error
    """
    try:
        response = api_method(*args, **kwargs)
        if response.status:
            return response.result
        else:
            error_msg = getattr(response, 'error', 'Unknown error')
            logger.error(f"API call failed: {error_msg}")
            return None
    except ApiException as e:
        logger.error(f"API exception: {e.status} - {e.reason}")
        if e.body:
            try:
                error_data = json.loads(e.body)
                logger.error(f"Error details: {error_data}")
            except:
                logger.error(f"Error body: {e.body}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None


def _retry_after_seconds(exc: Exception) -> Optional[float]:
    """Seconds from a ``Retry-After`` header on an ApiException, if it sent one."""
    headers = getattr(exc, "headers", None) or {}
    try:
        value = headers.get("Retry-After") or headers.get("retry-after")
        return min(float(value), 300.0) if value else None
    except (TypeError, ValueError, AttributeError):
        return None


def retry_with_backoff(func, max_retries: int = 3, base_delay: float = 1.0, *args, **kwargs):
    """
    Retry function with exponential backoff.
    
    Args:
        func: Function to retry
        max_retries: Maximum number of attempts (not retries-after-first); default 3
        base_delay: Base delay in seconds (doubled each 429/5xx retry)
        *args: Positional arguments for func
        **kwargs: Keyword arguments for func
    
    Returns:
        Function result (even if None)
    """
    last_exception = None
    for attempt in range(max_retries):
        try:
            result = func(*args, **kwargs)
            return result
        except ApiException as e:
            last_exception = e
            status = getattr(e, "status", None)
            retriable = status in (408, 429) or (
                status is not None and status >= 500
            )
            if retriable:
                if attempt < max_retries - 1:
                    delay = _retry_after_seconds(e) or min(base_delay * (2 ** attempt), 60.0)
                    logger.info(
                        "API status %s on %s (attempt %s/%s), retrying in %.1fs",
                        status,
                        getattr(func, "__name__", repr(func)),
                        attempt + 1,
                        max_retries,
                        delay,
                    )
                    time.sleep(delay)
                    continue
            is_attachment_error = False
            if e.status == 404:
                if e.body:
                    try:
                        import json
                        error_data = json.loads(e.body)
                        error_msg = str(error_data).lower()
                        if 'attachment' in error_msg or 'attachment not found' in error_msg:
                            is_attachment_error = True
                    except:
                        if 'attachment' in str(e.body).lower():
                            is_attachment_error = True
                if not is_attachment_error and hasattr(func, '__name__'):
                    func_name = func.__name__.lower()
                    if 'attachment' in func_name:
                        is_attachment_error = True
            
            if is_attachment_error:
                return None
            else:
                logger.error(f"API exception (status {e.status}): {e.reason}")
                if e.body:
                    try:
                        import json
                        error_data = json.loads(e.body)
                        logger.error(f"Error details: {error_data}")
                    except:
                        logger.error(f"Error body: {e.body}")
                raise
        except Exception as e:
            last_exception = e
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                time.sleep(delay)
                continue
            raise
    
    if last_exception:
        raise last_exception
    return None


def format_datetime(dt: Any) -> Optional[str]:
    """
    Format datetime to Qase format: YYYY-MM-DD HH:MM:SS
    
    Args:
        dt: Datetime object or string
    
    Returns:
        Formatted datetime string or None
    """
    if not dt:
        return None
    
    if isinstance(dt, str):
        try:
            # Handle ISO 8601 format with timezone (e.g., "2026-01-13T13:31:55+00:00")
            # Remove timezone info and parse
            dt_clean = dt.strip()
            
            # Remove timezone offset (e.g., "+00:00", "-05:00", or "Z")
            if dt_clean.endswith('Z'):
                dt_clean = dt_clean[:-1]
            elif '+' in dt_clean:
                # Split on '+' to remove timezone offset
                dt_clean = dt_clean.split('+')[0]
            elif '-' in dt_clean and dt_clean.count('-') > 2:
                # Handle negative timezone (e.g., "2026-01-13T13:31:55-05:00")
                # Find the last '-' that's part of the timezone
                parts = dt_clean.rsplit('-', 2)
                if len(parts) == 3 and ':' in parts[2]:
                    # Last part is timezone, remove it
                    dt_clean = '-'.join(parts[:2])
            
            # Try parsing various formats (order matters - try more specific first)
            formats = [
                '%Y-%m-%d %H:%M:%S.%f',  # With microseconds
                '%Y-%m-%d %H:%M:%S',     # Standard format
                '%Y-%m-%dT%H:%M:%S.%f',  # ISO with microseconds
                '%Y-%m-%dT%H:%M:%S',     # ISO format
                '%Y-%m-%d',              # Date only
            ]
            
            for fmt in formats:
                try:
                    parsed = datetime.strptime(dt_clean, fmt)
                    return parsed.strftime('%Y-%m-%d %H:%M:%S')
                except ValueError:
                    continue
            
            # If all parsing fails, try using datetime.fromisoformat (Python 3.7+)
            try:
                parsed = datetime.fromisoformat(dt.replace('Z', '+00:00'))
                return parsed.strftime('%Y-%m-%d %H:%M:%S')
            except (ValueError, AttributeError):
                pass
            
            # Last resort: return as-is (might cause API error, but better than None)
            logger.warning(f"Could not parse datetime format: {dt}, returning as-is")
            return dt
        except Exception as e:
            logger.warning(f"Error formatting datetime {dt}: {e}, returning as-is")
            return dt
    
    if isinstance(dt, datetime):
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    
    return None


def format_date(dt: Any) -> Optional[str]:
    """
    Format datetime to date format: YYYY-MM-DD
    
    Args:
        dt: Datetime object or string
    
    Returns:
        Formatted date string or None
    """
    if not dt:
        return None
    
    if isinstance(dt, str):
        try:
            for fmt in ['%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S']:
                try:
                    parsed = datetime.strptime(dt[:10], '%Y-%m-%d')
                    return parsed.strftime('%Y-%m-%d')
                except ValueError:
                    continue
            return dt[:10] if len(dt) >= 10 else dt
        except:
            return dt[:10] if len(dt) >= 10 else dt
    
    if isinstance(dt, datetime):
        return dt.strftime('%Y-%m-%d')
    
    return None


def chunks(lst: List, n: int):
    """
    Split list into chunks of size n.
    
    Args:
        lst: List to chunk
        n: Chunk size
    
    Yields:
        Chunks of the list
    """
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def to_dict(obj: Any) -> Dict[str, Any]:
    """
    Convert object to dictionary.
    
    Args:
        obj: Object to convert
    
    Returns:
        Dictionary representation
    """
    if hasattr(obj, 'to_dict'):
        return obj.to_dict()
    elif isinstance(obj, dict):
        return obj
    elif hasattr(obj, '__dict__'):
        return obj.__dict__
    else:
        return {}


def extract_entities_from_response(response: Any) -> Optional[List]:
    """
    Extract entities list from Qase API response.
    Handles different response structures: response.result.entities or response.entities
    
    Args:
        response: API response object
    
    Returns:
        List of entities or None
    """
    if not response:
        return None
    
    if hasattr(response, 'status') and hasattr(response, 'result'):
        if not response.status or not response.result:
            return None
        entities = getattr(response.result, 'entities', None)
    elif hasattr(response, 'entities'):
        entities = response.entities
    else:
        entities = getattr(response, 'entities', None)
    
    return entities if entities else None


def convert_uuids_to_strings(obj: Any) -> Any:
    """
    Recursively convert UUID objects to strings for JSON serialization.
    
    Args:
        obj: Object that may contain UUIDs
    
    Returns:
        Object with UUIDs converted to strings
    """
    if isinstance(obj, uuid.UUID):
        return str(obj)
    elif isinstance(obj, dict):
        return {key: convert_uuids_to_strings(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [convert_uuids_to_strings(item) for item in obj]
    else:
        return obj


class QaseRawApiClient:
    """Raw HTTP API client for operations that SDK doesn't support well."""
    
    def __init__(self, base_url: str, api_token: str):
        """
        Initialize raw API client.
        
        Args:
            base_url: Base API URL (e.g., https://api.qase.io/v1)
            api_token: API token
        """
        self.base_url = base_url.rstrip('/')
        self.headers = {
            'Token': api_token,
            'Content-Type': 'application/json'
        }
    
    def create_cases_bulk(self, project_code: str, cases: List[Dict[str, Any]]) -> Optional[List[int]]:
        """
        Create test cases in bulk using raw HTTP API.
        This bypasses SDK validation which may reject cases with shared steps.
        
        Args:
            project_code: Project code
            cases: List of case dictionaries
        
        Returns:
            List of created case IDs if successful, None otherwise
        """
        url = f"{self.base_url}/case/{project_code}/bulk"
        # Convert UUIDs to strings before JSON serialization
        cases_serializable = convert_uuids_to_strings(cases)
        payload = {"cases": cases_serializable}

        for attempt in range(3):
            if attempt:
                time.sleep(min(2 ** (attempt - 1), 8))
            try:
                response = qase_http.post(
                    url,
                    headers=self.headers,
                    json=payload,
                    timeout=_QASE_RAW_BULK_TIMEOUT,
                )
            except requests.exceptions.ReadTimeout as e:
                logger.info(
                    "create_cases_bulk read timeout (attempt %s/3) project=%s n_cases=%s: %s",
                    attempt + 1,
                    project_code,
                    len(cases),
                    e,
                )
                if attempt == 2:
                    logger.error("Exception creating cases bulk: %s", e)
                    return None
                continue
            except Exception as e:
                logger.error("Exception creating cases bulk: %s", e)
                return None

            if response.status_code == 200:
                response_data = response.json()
                if "result" in response_data:
                    if "ids" in response_data["result"]:
                        return response_data["result"]["ids"]
                    if "id" in response_data["result"]:
                        return [response_data["result"]["id"]]
                return []
            logger.error(
                "Failed to create cases bulk: %s - %s",
                response.status_code,
                response.text,
            )
            return None

    def create_results_bulk(
        self, project_code: str, run_id: int, results: List[Dict[str, Any]]
    ) -> bool:
        """
        POST /v1/result/{code}/{run_id}/bulk. Qase now caps bulk result creation
        at 200 per request (it was higher historically), so chunk accordingly.

        Not used by the default results migration path (v2 ``create_results_v2``);
        kept for ad-hoc or future bulk v1 flows.
        """
        url = f"{self.base_url}/result/{project_code}/{run_id}/bulk"
        payload = {"results": convert_uuids_to_strings(results)}

        try:
            response = qase_http.post(
                url, headers=self.headers, json=payload, timeout=_QASE_RAW_BULK_TIMEOUT
            )
            if response.status_code == 200:
                return True
            logger.error(
                "Failed to create results bulk: %s %s",
                response.status_code,
                (response.text or "")[:800],
            )
            return False
        except Exception as e:
            logger.error("Exception creating results bulk: %s", e)
            return False

    def patch_result(
        self,
        project_code: str,
        run_id: int,
        result_hash: str,
        body: Dict[str, Any],
    ) -> bool:
        """
        PATCH a single test result (comment, steps, attachments, etc.).
        Bulk create often ignores these fields; this matches ResultUpdate.
        """
        url = f"{self.base_url}/result/{project_code}/{run_id}/{result_hash}"
        try:
            response = qase_http.patch(
                url, headers=self.headers, json=body, timeout=120
            )
            if response.status_code == 200:
                return True
            logger.warning(
                "patch_result failed: %s %s — %s",
                response.status_code,
                url,
                (response.text or "")[:500],
            )
            return False
        except Exception as e:
            logger.error("patch_result exception: %s", e)
            return False

    def create_run(self, project_code: str, run_data: Dict[str, Any]) -> Optional[int]:
        """
        Create a test run using raw HTTP API.
        This ensures milestone_id and other fields are properly sent.
        Retries on HTTP 429 and 5xx with exponential backoff (aligned with SDK path).

        Args:
            project_code: Project code
            run_data: Run data dictionary with title, description, milestone_id, etc.

        Returns:
            Created run ID if successful, None otherwise
        """
        url = f"{self.base_url}/run/{project_code}"
        max_attempts = 7
        delay = 1.5

        for attempt in range(max_attempts):
            try:
                response = qase_http.post(
                    url, headers=self.headers, json=run_data, timeout=60
                )
                if response.status_code == 200:
                    response_data = response.json()
                    if response_data.get("status") and response_data.get("result"):
                        result = response_data["result"]
                        if isinstance(result, dict):
                            return result.get("id")
                        if isinstance(result, int):
                            return result
                    return None

                code = response.status_code
                if code == 429 or (500 <= code <= 599):
                    if attempt < max_attempts - 1:
                        logger.info(
                            "create_run HTTP %s (attempt %s/%s), retrying in %.1fs — %s",
                            code,
                            attempt + 1,
                            max_attempts,
                            delay,
                            (response.text or "")[:200],
                        )
                        time.sleep(delay)
                        delay = min(delay * 2, 60.0)
                        continue

                logger.error(
                    "Failed to create run: %s — %s",
                    code,
                    (response.text or "")[:500],
                )
                return None
            except requests.exceptions.RequestException as e:
                if attempt < max_attempts - 1:
                    logger.info(
                        "create_run request error (attempt %s/%s), retrying in %.1fs: %s",
                        attempt + 1,
                        max_attempts,
                        delay,
                        e,
                    )
                    time.sleep(delay)
                    delay = min(delay * 2, 60.0)
                    continue
                logger.error("Exception creating run: %s", e)
                return None

        return None
    
    def create_defect(self, project_code: str, defect_data: Dict[str, Any]) -> Optional[int]:
        """
        Create a defect using raw HTTP API.
        
        Args:
            project_code: Project code
            defect_data: Defect data dictionary with title, actual_result, severity, etc.
        
        Returns:
            Created defect ID if successful, None otherwise
        """
        url = f"{self.base_url}/defect/{project_code}"
        
        try:
            response = qase_http.post(url, headers=self.headers, json=defect_data, timeout=60)
            if response.status_code == 200:
                response_data = response.json()
                if response_data.get('status') and response_data.get('result'):
                    result = response_data['result']
                    if isinstance(result, dict):
                        return result.get('id')
                    elif isinstance(result, int):
                        return result
                return None
            else:
                logger.error(f"Failed to create defect: {response.status_code} - {response.text[:500]}")
                return None
        except Exception as e:
            logger.error(f"Exception creating defect: {e}")
            return None
    
    def resolve_defect(self, project_code: str, defect_id: int) -> bool:
        """
        Resolve a defect using PATCH endpoint.
        
        Args:
            project_code: Project code
            defect_id: Defect ID to resolve
        
        Returns:
            True if successful, False otherwise
        """
        url = f"{self.base_url}/defect/{project_code}/resolve/{defect_id}"
        
        try:
            response = qase_http.patch(url, headers=self.headers, timeout=60)
            return response.status_code == 200
        except Exception:
            return False
    
    def attach_defect_to_results(
        self,
        project_code: str,
        defect_id: int,
        run_ids: List[int],
        result_hashes: List[str]
    ) -> bool:
        """
        Attach a defect to runs and results.
        Since the API doesn't have a public attach endpoint, we try updating the defect.
        
        Args:
            project_code: Project code
            defect_id: Target defect ID
            run_ids: List of target run IDs
            result_hashes: List of target result hashes
        
        Returns:
            True if successful, False otherwise
        """
        if not run_ids and not result_hashes:
            return True
        
        # Try updating the defect with runs and results via PUT
        url = f"{self.base_url}/defect/{project_code}/{defect_id}"
        payload = {}
        
        if run_ids:
            payload['runs'] = run_ids
        if result_hashes:
            payload['results'] = result_hashes
        
        try:
            # Try PUT first
            response = qase_http.put(url, headers=self.headers, json=payload, timeout=60)
            if response.status_code == 200:
                return True
            
            # Try PATCH if PUT doesn't work
            patch_response = qase_http.patch(url, headers=self.headers, json=payload, timeout=60)
            if patch_response.status_code == 200:
                return True
                
        except Exception:
            pass
        
        # If update doesn't work, the defect is created but not linked
        # Note: Qase API may not support linking defects to runs/results via API token
        # Defects will need to be manually linked via UI or the linking may happen automatically
        # if runs/results were included in creation payload
        return False
    
    def attach_external_issues(self, project_code: str, links: List[Dict[str, Any]], issue_type: str = "jira-cloud") -> bool:
        """
        Attach external issues to test cases.
        
        Args:
            project_code: Project code
            links: List of links with case_id and external_issues
            issue_type: Type of external issue system (jira-cloud, jira-server, etc.)
        
        Returns:
            True if successful, False otherwise
        """
        url = f"{self.base_url}/case/{project_code}/external-issue/attach"
        payload = {
            "type": issue_type,
            "links": links
        }
        
        try:
            response = qase_http.post(url, headers=self.headers, json=payload, timeout=60)
            if response.status_code == 200:
                return True
            else:
                logger.error(f"Failed to attach external issues: {response.status_code} - {response.text}")
                return False
        except Exception as e:
            logger.error(f"Exception attaching external issues: {e}")
            return False
