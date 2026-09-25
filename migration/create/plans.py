"""
Create test plans in target Qase workspace.
"""
import logging
from typing import Dict, Any
from qase.api_client_v1.api.plans_api import PlansApi
from qase.api_client_v1.models import PlanCreate
from qase_service import QaseService
from migration.utils import MigrationMappings, MigrationStats, retry_with_backoff, to_dict

logger = logging.getLogger(__name__)


def migrate_plans(
    source_service: QaseService,
    target_service: QaseService,
    project_code_source: str,
    project_code_target: str,
    case_mapping: Dict[int, int],
    mappings: MigrationMappings,
    stats: MigrationStats
) -> Dict[int, int]:
    """
    Migrate test plans from source to target workspace.
    
    Args:
        source_service: Source Qase service
        target_service: Target Qase service
        project_code_source: Source project code
        project_code_target: Target project code
        case_mapping: Mapping of source case IDs to target case IDs
        mappings: Migration mappings object
        stats: Migration stats object
    
    Returns:
        Dictionary mapping source plan ID to target plan ID
    """
    from migration.extract.plans import extract_plans
    
    plans = extract_plans(source_service, project_code_source)
    
    plans_api_target = PlansApi(target_service.client)
    plan_mapping = {}
    reused = 0
    
    for plan_dict in plans:
        source_id = plan_dict.get('id')
        if not source_id:
            continue
        
        # Skip if already mapped
        if project_code_source in mappings.plans and source_id in mappings.plans[project_code_source]:
            plan_mapping[source_id] = mappings.plans[project_code_source][source_id]
            reused += 1
            continue
        
        title = plan_dict.get('title')
        if not title:
            continue
        
        description = plan_dict.get('description')
        
        # Map case IDs from source to target
        cases_list = plan_dict.get('cases', [])
        target_case_ids = []
        
        for case_item in cases_list:
            case_item_dict = to_dict(case_item) if not isinstance(case_item, dict) else case_item
            source_case_id = case_item_dict.get('case_id')
            
            if source_case_id and source_case_id in case_mapping:
                target_case_id = case_mapping[source_case_id]
                target_case_ids.append(target_case_id)
        
        if not target_case_ids:
            logger.warning(f"Plan '{title}' has no valid cases to migrate, skipping")
            continue
        
        plan_data = PlanCreate(
            title=title,
            cases=target_case_ids
        )
        
        if description:
            plan_data.description = description
        
        create_response = retry_with_backoff(
            plans_api_target.create_plan,
            code=project_code_target,
            plan_create=plan_data
        )
        
        if create_response:
            target_id = None
            if hasattr(create_response, 'status') and hasattr(create_response, 'result'):
                if create_response.status and create_response.result:
                    target_id = getattr(create_response.result, 'id', None)
            elif hasattr(create_response, 'id'):
                target_id = create_response.id
            elif hasattr(create_response, 'result'):
                result = create_response.result
                target_id = getattr(result, 'id', None)
            
            if target_id:
                plan_mapping[source_id] = target_id
    
    if project_code_source not in mappings.plans:
        mappings.plans[project_code_source] = {}
    mappings.plans[project_code_source].update(plan_mapping)
    
    stats.add_entity('plans', len(plans), len(plan_mapping))
    stats.add_reused('plans', reused)
    return plan_mapping
