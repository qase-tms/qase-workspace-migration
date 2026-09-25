"""
Extract test plans from source Qase workspace.
"""
import logging
from typing import List, Dict, Any
from qase.api_client_v1.api.plans_api import PlansApi
from qase_service import QaseService
from migration.utils import retry_with_backoff, extract_entities_from_response, to_dict

logger = logging.getLogger(__name__)


def extract_plans(source_service: QaseService, project_code: str) -> List[Dict[str, Any]]:
    """
    Extract all test plans from source project.
    
    Args:
        source_service: Source Qase service
        project_code: Project code
    
    Returns:
        List of test plan dictionaries (with cases included)
    """
    plans_api_source = PlansApi(source_service.client)
    
    plans = []
    offset = 0
    limit = 100
    
    while True:
        try:
            api_response = retry_with_backoff(
                plans_api_source.get_plans,
                code=project_code,
                limit=limit,
                offset=offset
            )
            
            entities = extract_entities_from_response(api_response)
            if not entities:
                break
            
            for entity in entities:
                plan_dict = to_dict(entity)
                plan_id = plan_dict.get('id')
                
                if plan_id:
                    # Get full plan details including cases
                    try:
                        plan_detail_response = retry_with_backoff(
                            plans_api_source.get_plan,
                            code=project_code,
                            id=plan_id
                        )
                        
                        if plan_detail_response and hasattr(plan_detail_response, 'result'):
                            detail_dict = to_dict(plan_detail_response.result)
                            plan_dict.update(detail_dict)
                    except Exception as e:
                        logger.warning(f"Failed to get details for plan {plan_id}: {e}")
                
                plans.append(plan_dict)
            
            if len(entities) < limit:
                break
            
            offset += limit
        except Exception as e:
            logger.error(f"Error fetching plans: {e}")
            break
    
    return plans
