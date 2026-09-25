"""
Extract runs from source Qase workspace.
"""
import logging
import requests

from migration import qase_http
from typing import List, Dict, Any
from qase.api_client_v1.api.runs_api import RunsApi
from qase_service import QaseService
from migration.utils import retry_with_backoff, extract_entities_from_response, to_dict

logger = logging.getLogger(__name__)


def extract_runs(source_service: QaseService, project_code: str) -> List[Dict[str, Any]]:
    """
    Extract test runs from source project.
    Uses raw HTTP API to get full response including user_id field.
    
    Args:
        source_service: Source Qase service
        project_code: Project code
    
    Returns:
        List of run dictionaries with full details including user_id
    """
    runs = []
    
    # Get API configuration from service
    try:
        base_url = source_service.client.configuration.host
        api_key_dict = source_service.client.configuration.api_key
        if isinstance(api_key_dict, dict):
            api_token = api_key_dict.get('TokenAuth') or api_key_dict.get('Token') or api_key_dict.get('token')
        else:
            api_token = None
    except Exception:
        logger.error("Cannot get API token/URL from service")
        return runs
    
    if not api_token or not base_url:
        logger.error("API token or base URL not available")
        return runs
    
    # Use raw HTTP API to get full response including user_id
    api_base = base_url.rstrip('/')
    if not api_base.endswith('/v1'):
        api_base = f"{api_base}/v1"
    
    offset = 0
    limit = 100
    
    while True:
        try:
            url = f"{api_base}/run/{project_code}"
            headers = {
                'Token': api_token,
                'accept': 'application/json'
            }
            params = {
                'limit': limit,
                'offset': offset
            }
            
            response = qase_http.get(url, headers=headers, params=params, timeout=30)
            if response.status_code == 200:
                response_data = response.json()
                if response_data.get('status') and response_data.get('result'):
                    result = response_data['result']
                    entities_list = result.get('entities', [])
                    if not entities_list and isinstance(result, list):
                        entities_list = result
                    
                    if entities_list:
                        runs.extend(entities_list)
                        
                        # Check if there are more pages
                        total = result.get('total', len(entities_list))
                        if len(entities_list) < limit or offset + len(entities_list) >= total:
                            break
                        offset += limit
                    else:
                        break
                else:
                    logger.warning(f"Unexpected response format: {response_data}")
                    break
            else:
                logger.error(f"Failed to fetch runs via raw API: {response.status_code} - {response.text[:200]}")
                break
        except Exception as e:
            logger.error(f"Failed to fetch runs via raw API: {e}")
            break
    
    return runs


def fetch_run_detail_json(
    source_service: QaseService,
    project_code: str,
    run_id: int,
) -> Dict[str, Any]:
    """
    GET /v1/run/{code}/{id} — single-run payload (completion flags often absent from list).
    """
    out: Dict[str, Any] = {}
    try:
        base_url = source_service.client.configuration.host
        api_key_dict = source_service.client.configuration.api_key
        if isinstance(api_key_dict, dict):
            api_token = api_key_dict.get("TokenAuth") or api_key_dict.get("Token") or api_key_dict.get("token")
        else:
            api_token = None
    except Exception:
        return out

    if not api_token or not base_url:
        return out

    api_base = base_url.rstrip("/")
    if not api_base.endswith("/v1"):
        api_base = f"{api_base}/v1"

    url = f"{api_base}/run/{project_code}/{int(run_id)}"
    headers = {"Token": api_token, "accept": "application/json"}
    try:
        response = qase_http.get(url, headers=headers, timeout=45)
        if response.status_code != 200:
            logger.debug(
                "fetch_run_detail_json %s/%s: HTTP %s",
                project_code,
                run_id,
                response.status_code,
            )
            return out
        data = response.json()
        if not data.get("status") or not data.get("result"):
            return out
        res = data["result"]
        if isinstance(res, dict):
            return res
        return to_dict(res)
    except Exception as e:
        logger.debug("fetch_run_detail_json failed: %s", e)
        return out


def extract_run_cases(
    source_service: QaseService,
    project_code: str,
    run_id: int,
    case_mapping: Dict[int, int]
) -> List[int]:
    """
    Extract all case IDs from a run (including untested cases).
    
    Uses get_run with include='cases' if supported, otherwise falls back to get_tests().
    
    Args:
        source_service: Source Qase service
        project_code: Project code
        run_id: Run ID
        case_mapping: Mapping of source case ID -> target case ID
    
    Returns:
        List of target case IDs
    """
    runs_api_source = RunsApi(source_service.client)
    target_cases = []
    
    try:
        # Try get_run with include='cases' first
        run_response = retry_with_backoff(
            runs_api_source.get_run,
            code=project_code,
            id=run_id,
            include='cases'
        )
        
        if run_response and hasattr(run_response, 'result'):
            run_result = run_response.result
            run_result_dict = to_dict(run_result)
            
            cases_data = run_result_dict.get('cases', [])
            if not cases_data and hasattr(run_result, 'cases'):
                cases_data = to_dict(run_result.cases) if hasattr(run_result.cases, '__dict__') else run_result.cases
            
            if cases_data:
                for case_item in cases_data:
                    case_id = None
                    if isinstance(case_item, int):
                        case_id = case_item
                    elif isinstance(case_item, dict):
                        case_id = case_item.get('id') or case_item.get('case_id')
                    elif hasattr(case_item, 'id'):
                        case_id = case_item.id
                    
                    if case_id:
                        target_case_id = case_mapping.get(int(case_id))
                        if target_case_id and target_case_id not in target_cases:
                            target_cases.append(target_case_id)
                return target_cases
    except TypeError as e:
        if 'include' in str(e).lower() or 'unexpected keyword' in str(e).lower():
            try:
                tests_offset = 0
                tests_limit = 250
                while True:
                    tests_response = retry_with_backoff(
                        runs_api_source.get_tests,
                        code=project_code,
                        id=run_id,
                        limit=tests_limit,
                        offset=tests_offset
                    )
                    
                    if tests_response and hasattr(tests_response, 'result'):
                        tests_entities = extract_entities_from_response(tests_response)
                        if not tests_entities:
                            break
                        
                        for test in tests_entities:
                            test_dict = to_dict(test)
                            source_case_id = test_dict.get('case_id')
                            if source_case_id:
                                target_case_id = case_mapping.get(int(source_case_id))
                                if target_case_id and target_case_id not in target_cases:
                                    target_cases.append(target_case_id)
                        
                        if len(tests_entities) < tests_limit:
                            break
                        tests_offset += tests_limit
                    else:
                        break
                return target_cases
            except Exception:
                pass
    except Exception:
        pass
    
    return []
