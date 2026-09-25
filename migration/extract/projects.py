"""
Extract projects from source Qase workspace.
"""
import logging
from typing import List, Optional, Dict, Any
from qase.api_client_v1.api.projects_api import ProjectsApi
from qase_service import QaseService
from migration.utils import retry_with_backoff, extract_entities_from_response, to_dict

logger = logging.getLogger(__name__)


def extract_projects(source_service: QaseService, only_projects: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """
    Extract all projects from source workspace.
    
    Args:
        source_service: Source Qase service
        only_projects: If provided, extract only projects with codes in this list
    
    Returns:
        List of project dictionaries
    """
    logger.info("Extracting projects from source workspace...")
    if only_projects:
        logger.info(f"Filtering: Only extracting projects: {only_projects}")
    
    projects_api_source = ProjectsApi(source_service.client)
    projects = []
    offset = 0
    limit = 100
    
    while True:
        api_response = retry_with_backoff(
            projects_api_source.get_projects,
            limit=limit,
            offset=offset
        )
        
        entities = extract_entities_from_response(api_response)
        if not entities:
            break
        
        for project in entities:
            project_dict = to_dict(project)
            project_code = project_dict.get('code', 'UNKNOWN')
            
            # Filter by project code if only_projects is specified
            if only_projects and project_code not in only_projects:
                logger.debug(f"Skipping project {project_code} - not in only_projects list")
                continue
            
            projects.append(project_dict)
        
        if len(entities) < limit:
            break
        offset += limit

    # Same project code can appear more than once (pagination / API quirks). Migrating
    # twice would show duplicate tqdm lines (e.g. two "DAT" bars) and double-write data.
    seen_codes: set = set()
    deduped: List[Dict[str, Any]] = []
    for p in projects:
        code = p.get("code")
        if code is None or code == "":
            deduped.append(p)
            continue
        if code in seen_codes:
            logger.warning(
                "Skipping duplicate source project with code %r (keep first list entry only).",
                code,
            )
            continue
        seen_codes.add(code)
        deduped.append(p)

    logger.info("Extracted %s projects from source workspace", len(deduped))
    return deduped
