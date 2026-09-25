"""
Extract cases from source Qase workspace.
"""
import logging
from typing import List, Dict, Any
from qase.api_client_v1.api.cases_api import CasesApi
from qase_service import QaseService
from migration.utils import retry_with_backoff, extract_entities_from_response, to_dict

logger = logging.getLogger(__name__)


def extract_cases(source_service: QaseService, project_code: str, limit: int = 20) -> List[Dict[str, Any]]:
    """
    Extract test cases from source project.
    Uses bulk extraction via get_cases() which returns full case details including steps.
    
    Args:
        source_service: Source Qase service
        project_code: Project code
        limit: Batch size (default 20)
    
    Returns:
        List of case dictionaries with full details including steps and member_id
    """
    cases_api_source = CasesApi(source_service.client)
    
    cases = []
    offset = 0
    
    # Use bulk extraction - get_cases() returns full case details
    while True:
        source_cases_response = retry_with_backoff(
            cases_api_source.get_cases,
            code=project_code,
            limit=limit,
            offset=offset
        )
        
        source_cases_entities = extract_entities_from_response(source_cases_response)
        if not source_cases_entities:
            break
        
        # Convert entities to dictionaries - get_cases() should return full details
        for source_case in source_cases_entities:
            case_dict = to_dict(source_case)
            # Check if we have steps (full details) or need to fetch individually
            if case_dict.get('steps') is not None or case_dict.get('steps_type') is not None:
                # Full details available, use directly
                cases.append(case_dict)
            else:
                # Need to fetch full details for steps/shared steps
                case_id = case_dict.get('id')
                if case_id:
                    try:
                        case_response = retry_with_backoff(
                            cases_api_source.get_case,
                            code=project_code,
                            id=case_id
                        )
                        
                        if case_response and hasattr(case_response, 'result') and case_response.result:
                            full_case_dict = to_dict(case_response.result)
                            cases.append(full_case_dict)
                    except Exception:
                        # If individual fetch fails, use summary data
                        cases.append(case_dict)
        
        if len(source_cases_entities) < limit:
            break
        offset += limit
    
    return cases
