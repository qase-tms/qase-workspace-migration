"""
Create projects in target Qase workspace.
"""
import logging
from typing import Dict, Any, Optional, List
from qase.api_client_v1.api.projects_api import ProjectsApi
from qase.api_client_v1.models import ProjectCreate
from qase.api_client_v1.exceptions import ApiException
from qase_service import QaseService
from migration.utils import MigrationMappings, MigrationStats, retry_with_backoff, to_dict

logger = logging.getLogger(__name__)


def project_has_data(project: Dict[str, Any]) -> bool:
    """True when a target project already holds cases, suites, runs or defects."""
    counts = project.get('counts') or {}
    runs = counts.get('runs') or {}
    total_runs = runs.get('total', 0) if isinstance(runs, dict) else runs
    defects = counts.get('defects') or {}
    total_defects = defects.get('total', 0) if isinstance(defects, dict) else defects
    return any(
        int(n or 0) > 0
        for n in (counts.get('cases'), counts.get('suites'), counts.get('milestones'), total_runs, total_defects)
    )


def create_project(
    project_dict: Dict[str, Any],
    target_service: QaseService,
    mappings: MigrationMappings,
    stats: MigrationStats
) -> Optional[Dict[str, Any]]:
    """
    Create a single project in target workspace.
    
    Args:
        project_dict: Project data dictionary from source
        target_service: Target Qase service
        mappings: Migration mappings object
        stats: Migration stats object
    
    Returns:
        Dictionary with source_code, target_code, source_id, target_id, or None if failed
    """
    projects_api_target = ProjectsApi(target_service.client)
    
    # Check if project already exists in target
    project_exists = False
    try:
        existing = projects_api_target.get_project(code=project_dict['code'])
        if existing and hasattr(existing, 'status') and existing.status:
            if hasattr(existing, 'result') and existing.result:
                result = existing.result
                target_code = getattr(result, 'code', project_dict['code'])
                target_id = getattr(result, 'id', None)
                
                mappings.projects[project_dict['code']] = target_code
                return {
                    'source_code': project_dict['code'],
                    'target_code': target_code,
                    'source_id': project_dict.get('id'),
                    'target_id': target_id,
                    'already_existed': True,
                    'has_data': project_has_data(to_dict(result)),
                }
    except ApiException as e:
        if e.status == 404:
            pass  # Project doesn't exist, continue with creation
        else:
            pass
    except Exception as e:
        pass
    
    project_data = ProjectCreate(
        title=project_dict['title'],
        code=project_dict['code'],
        description=project_dict.get('description', ''),
        settings=project_dict.get('settings', {'runs': {'auto_complete': False}}),
        access=project_dict.get('access', 'all')
    )
    
    try:
        create_response = retry_with_backoff(
            projects_api_target.create_project,
            project_create=project_data
        )
        
        if create_response:
            # Handle different response structures
            target_code = None
            target_id = None
            
            if hasattr(create_response, 'status') and hasattr(create_response, 'result'):
                if create_response.status and create_response.result:
                    result = create_response.result
                    target_code = getattr(result, 'code', None)
                    target_id = getattr(result, 'id', None)
            elif hasattr(create_response, 'code'):
                target_code = create_response.code
                target_id = getattr(create_response, 'id', None)
            elif hasattr(create_response, 'result'):
                result = create_response.result
                target_code = getattr(result, 'code', None)
                target_id = getattr(result, 'id', None)
            
            if target_code:
                mappings.projects[project_dict['code']] = target_code
                return {
                    'source_code': project_dict['code'],
                    'target_code': target_code,
                    'source_id': project_dict.get('id'),
                    'target_id': target_id
                }
            else:
                mappings.projects[project_dict['code']] = project_dict['code']
                return {
                    'source_code': project_dict['code'],
                    'target_code': project_dict['code'],
                    'source_id': project_dict.get('id'),
                    'target_id': None
                }
    except ApiException as e:
        if e.status == 400 and "already exists" in str(e.body).lower():
            try:
                existing = projects_api_target.get_project(code=project_dict['code'])
                if existing and hasattr(existing, 'status') and existing.status:
                    if hasattr(existing, 'result') and existing.result:
                        mappings.projects[project_dict['code']] = existing.result.code
                        return {
                            'source_code': project_dict['code'],
                            'target_code': existing.result.code,
                            'source_id': project_dict.get('id'),
                            'target_id': existing.result.id,
                            'already_existed': True,
                            'has_data': project_has_data(to_dict(existing.result)),
                        }
            except:
                pass
        else:
            raise
    
    return None


def migrate_projects(
    source_service: QaseService,
    target_service: QaseService,
    mappings: MigrationMappings,
    stats: MigrationStats,
    only_projects: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """
    Migrate projects from source to target workspace.
    
    This is a convenience function that combines extraction and creation.
    For more control, use extract_projects() and create_project() separately.
    
    Args:
        source_service: Source Qase service
        target_service: Target Qase service
        mappings: Migration mappings object
        stats: Migration stats object
        only_projects: If provided, migrate only projects with codes in this list
    
    Returns:
        List of project mappings
    """
    from migration.extract.projects import extract_projects
    
    source_projects = extract_projects(source_service, only_projects)
    
    projects = []
    for project_dict in source_projects:
        result = create_project(project_dict, target_service, mappings, stats)
        if result:
            projects.append(result)
    
    stats.add_entity('projects', len(source_projects), len(projects))
    return projects
