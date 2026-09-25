"""
Extract results from source Qase workspace.
"""
import logging
import requests

from migration import qase_http
from typing import List, Dict, Any, Optional
from qase_service import QaseService

logger = logging.getLogger(__name__)


class ResultsFetchError(Exception):
    """The results of a run could not be read in full."""


def extract_results(
    source_service: QaseService, project_code: str, run_id: int, strict: bool = False
) -> List[Dict[str, Any]]:
    """
    Extract test results from a specific run.
    Uses raw HTTP API to get full response including member_id field.
    
    Args:
        source_service: Source Qase service
        project_code: Project code
        run_id: Run ID
    
    Returns:
        List of result dictionaries with full details including member_id

    With ``strict``, a failed read raises ResultsFetchError instead of returning
    whatever was read so far, so a caller cannot mistake it for an empty run.
    """
    results = []
    
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
        return results
    
    if not api_token or not base_url:
        logger.error("API token or base URL not available")
        return results
    
    # Use raw HTTP API to get full response including member_id
    api_base = base_url.rstrip('/')
    if not api_base.endswith('/v1'):
        api_base = f"{api_base}/v1"
    
    offset = 0
    limit = 100  # API limit is max 100
    
    while True:
        try:
            url = f"{api_base}/result/{project_code}"
            headers = {
                'Token': api_token,
                'accept': 'application/json'
            }
            params = {
                'run': str(run_id),
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
                        results.extend(entities_list)
                        
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
                message = f"Failed to fetch results via raw API: {response.status_code} - {response.text[:200]}"
                if strict:
                    raise ResultsFetchError(message)
                logger.error(message)
                break
        except ResultsFetchError:
            raise
        except Exception as e:
            if strict:
                raise ResultsFetchError(f"Failed to fetch results via raw API: {e}") from e
            logger.error(f"Failed to fetch results via raw API: {e}")
            break
    
    return results


def fetch_result_detail_json(
    source_service: QaseService,
    project_code: str,
    result_hash: str,
) -> Optional[Dict[str, Any]]:
    """
    GET /v1/result/{code}/{hash} — full result payload (steps often richer than list run).
    """
    if not result_hash:
        return None
    try:
        base_url = source_service.client.configuration.host
        api_key_dict = source_service.client.configuration.api_key
        if isinstance(api_key_dict, dict):
            api_token = api_key_dict.get("TokenAuth") or api_key_dict.get("Token") or api_key_dict.get("token")
        else:
            api_token = None
    except Exception:
        return None

    if not api_token or not base_url:
        return None

    api_base = base_url.rstrip("/")
    if not api_base.endswith("/v1"):
        api_base = f"{api_base}/v1"

    url = f"{api_base}/result/{project_code}/{result_hash}"
    headers = {"Token": api_token, "accept": "application/json"}
    try:
        response = qase_http.get(url, headers=headers, timeout=60)
        if response.status_code != 200:
            logger.debug(
                "fetch_result_detail_json %s: HTTP %s",
                (result_hash[:16] + "…") if result_hash else "",
                response.status_code,
            )
            return None
        data = response.json()
        if not data.get("status") or not data.get("result"):
            return None
        res = data["result"]
        if isinstance(res, dict):
            return res
        from migration.utils import to_dict as util_to_dict

        return util_to_dict(res)
    except Exception as e:
        logger.debug("fetch_result_detail_json failed: %s", e)
        return None
