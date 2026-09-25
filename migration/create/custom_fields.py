"""
Create custom fields in target Qase workspace.
"""
import logging
from typing import Dict, Any, List, Optional
from qase.api_client_v1.api.custom_fields_api import CustomFieldsApi
from qase.api_client_v1.models import CustomFieldCreate, CustomFieldCreateValueInner
from qase_service import QaseService
from migration.utils import MigrationMappings, MigrationStats, retry_with_backoff, extract_entities_from_response, to_dict

logger = logging.getLogger(__name__)


def get_existing_custom_fields(target_service: QaseService) -> Dict[str, Dict[str, Any]]:
    """
    Get all existing custom fields from target workspace, indexed by normalized title.
    
    Returns:
        Dictionary mapping normalized_title -> {original_title, id}
    """
    custom_fields_api_target = CustomFieldsApi(target_service.client)
    existing_fields_by_title = {}
    
    try:
        all_existing_entities = []
        existing_offset = 0
        existing_limit = 100
        
        while True:
            existing_fields_response = custom_fields_api_target.get_custom_fields(
                limit=existing_limit,
                offset=existing_offset
            )
            existing_entities = extract_entities_from_response(existing_fields_response)
            
            if not existing_entities:
                break
            
            all_existing_entities.extend(existing_entities)
            
            if len(existing_entities) < existing_limit:
                break
            
            existing_offset += existing_limit
        
        if all_existing_entities:
            for existing in all_existing_entities:
                existing_dict = to_dict(existing)
                existing_title = existing_dict.get('title')
                existing_id = existing_dict.get('id')
                
                if existing_title and existing_id:
                    normalized_title = existing_title.strip().lower()
                    # type and entity are kept so a same-named field of the wrong
                    # shape is not silently adopted (see create_custom_field).
                    existing_fields_by_title[normalized_title] = {
                        'original_title': existing_title,
                        'id': existing_id,
                        'type': existing_dict.get('type'),
                        'entity': existing_dict.get('entity'),
                    }
    except Exception as e:
        logger.error(f"Error fetching existing custom fields: {e}")
    
    return existing_fields_by_title


def create_custom_field(
    field_dict: Dict[str, Any],
    target_service: QaseService,
    existing_fields_by_title: Dict[str, Dict[str, Any]],
    mappings: MigrationMappings,
    stats: Optional[MigrationStats] = None
) -> bool:
    """
    Create a single custom field in target workspace.

    Args:
        field_dict: Custom field data dictionary from source
        target_service: Target Qase service
        existing_fields_by_title: Dictionary of existing fields by normalized title
        mappings: Migration mappings object
        stats: Migration stats object, used to report unusable existing fields

    Returns:
        True if field was created or already existed, False otherwise
    """
    custom_fields_api_target = CustomFieldsApi(target_service.client)

    field_title = field_dict.get('title')
    source_id = field_dict.get('id')

    if not field_title:
        return False

    normalized_title = field_title.strip().lower()

    if normalized_title in existing_fields_by_title:
        existing_info = existing_fields_by_title[normalized_title]
        existing_id = existing_info['id']

        # Custom fields are workspace-global, so a field of this name may already
        # exist from an earlier run or another project. Adopting one whose type or
        # entity differs from the source is worse than not mapping it at all: case
        # values then fail validation and the API rejects the whole chunk of cases
        # they were sent in. A field's type cannot be changed after creation
        # (PATCH accepts the request and ignores it), so the only remedy is for the
        # operator to delete the field and re-run.
        def _norm(value: Any) -> str:
            return str(value or '').strip().lower()

        differences = []
        source_type, existing_type = _norm(field_dict.get('type')), _norm(existing_info.get('type'))
        source_entity, existing_entity = _norm(field_dict.get('entity')), _norm(existing_info.get('entity'))
        if source_type and existing_type and source_type != existing_type:
            differences.append(f"type (target {existing_type!r}, source {source_type!r})")
        if source_entity and existing_entity and source_entity != existing_entity:
            differences.append(f"entity (target {existing_entity!r}, source {source_entity!r})")

        if differences:
            message = (
                f"Custom field {field_title!r} already exists in the target workspace "
                f"(id {existing_id}) but differs by {' and '.join(differences)}. It was left "
                f"unmapped, so its values will not migrate; cases themselves are unaffected. "
                f"Qase cannot change a field's type after creation - delete field {existing_id} "
                f"in the target workspace (Settings > Custom fields) and re-run to migrate its values."
            )
            logger.error(message)
            print(f"ERROR: {message}")
            if stats is not None:
                stats.add_error('custom_fields', message)
            return False

        mappings.custom_fields[source_id] = existing_id
        return True

    entity_mapping = {
        'case': 0,
        'run': 1,
        'defect': 2
    }
    entity_value = field_dict.get('entity', 0)
    if isinstance(entity_value, str):
        entity_value = entity_mapping.get(entity_value.lower(), 0)
    elif not isinstance(entity_value, int):
        entity_value = 0
    
    # Qase type ids (CustomFieldCreate): 0 - number; 1 - string; 2 - text;
    # 3 - selectbox; 4 - checkbox; 5 - radio; 6 - multiselect; 7 - url;
    # 8 - user; 9 - datetime.
    #
    # `number` and `string` used to be mapped to each other's ids, so every
    # source string field was created as a number in the target. Cases carrying
    # a value in such a field were then rejected by the bulk create with
    # "The custom_field.<id> must be a number", taking the whole chunk of cases
    # down with them. GET returns the type as a name ('string', 'datetime', …),
    # so these keys must match what the API reports.
    type_mapping = {
        'number': 0, 'string': 1, 'text': 2, 'selectbox': 3,
        'checkbox': 4, 'radio': 5, 'multiselect': 6,
        'url': 7, 'user': 8, 'datetime': 9, 'date': 9,
    }
    # Unknown type names fall back to string, not number: string accepts any
    # scalar value, whereas number rejects most of them and fails the case import.
    FALLBACK_TYPE = 1
    type_value = field_dict.get('type', FALLBACK_TYPE)
    if isinstance(type_value, str):
        mapped_type = type_mapping.get(type_value.lower())
        if mapped_type is None:
            logger.warning(
                "Unknown custom field type %r on field %r — creating it as a string field",
                type_value,
                field_dict.get('title'),
            )
            mapped_type = FALLBACK_TYPE
        type_value = mapped_type
    elif not isinstance(type_value, int):
        type_value = FALLBACK_TYPE
    
    value_options = []
    if field_dict.get('value'):
        seen_titles = set()
        idx = 1
        for val in field_dict['value']:
            if isinstance(val, dict):
                title = val.get('title', val.get('value', ''))
                original_title = title
                title_suffix = 1
                while title in seen_titles:
                    title = f"{original_title} ({title_suffix})"
                    title_suffix += 1
                seen_titles.add(title)
                value_options.append(CustomFieldCreateValueInner(id=idx, title=title))
            else:
                title = str(val)
                original_title = title
                title_suffix = 1
                while title in seen_titles:
                    title = f"{original_title} ({title_suffix})"
                    title_suffix += 1
                seen_titles.add(title)
                value_options.append(CustomFieldCreateValueInner(id=idx, title=title))
            idx += 1
    
    projects_codes = field_dict.get('projects_codes', [])
    is_enabled_for_all = field_dict.get('is_enabled_for_all_projects', False)
    
    field_data = CustomFieldCreate(
        title=field_dict['title'],
        entity=entity_value,
        type=type_value,
        value=value_options if value_options else None,
        is_filterable=field_dict.get('is_filterable', True),
        is_visible=field_dict.get('is_visible', True),
        is_required=field_dict.get('is_required', False),
        is_enabled_for_all_projects=is_enabled_for_all,
        projects_codes=projects_codes if not is_enabled_for_all else None,
        default_value=field_dict.get('default_value')
    )
    
    create_response = retry_with_backoff(
        custom_fields_api_target.create_custom_field,
        custom_field_create=field_data
    )
    
    if create_response:
        target_field_id = None
        if hasattr(create_response, 'status') and hasattr(create_response, 'result'):
            if create_response.status and create_response.result:
                target_field_id = getattr(create_response.result, 'id', None)
        elif hasattr(create_response, 'id'):
            target_field_id = create_response.id
        elif hasattr(create_response, 'result'):
            result = create_response.result
            target_field_id = getattr(result, 'id', None)
        
        if target_field_id:
            mappings.custom_fields[source_id] = target_field_id
            return True
        else:
            if hasattr(create_response, 'result'):
                result_dict = to_dict(create_response.result)
                if 'id' in result_dict:
                    target_field_id = result_dict['id']
                    mappings.custom_fields[source_id] = target_field_id
                    return True
    
    return False


def migrate_custom_fields(
    source_service: QaseService,
    target_service: QaseService,
    mappings: MigrationMappings,
    stats: MigrationStats
) -> Dict[int, int]:
    """
    Migrate custom fields from source to target workspace.
    
    Args:
        source_service: Source Qase service
        target_service: Target Qase service
        mappings: Migration mappings object
        stats: Migration stats object
    
    Returns:
        Dictionary mapping source field ID to target field ID
    """
    from migration.extract.custom_fields import extract_custom_fields
    
    source_fields = extract_custom_fields(source_service)
    existing_fields = get_existing_custom_fields(target_service)
    
    field_mapping = {}
    for field_dict in source_fields:
        if create_custom_field(field_dict, target_service, existing_fields, mappings, stats):
            source_id = field_dict.get('id')
            if source_id in mappings.custom_fields:
                field_mapping[source_id] = mappings.custom_fields[source_id]
    
    stats.add_entity('custom_fields', len(source_fields), len(field_mapping))
    return field_mapping
