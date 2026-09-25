"""
Migration utilities module.
This module re-exports everything from migration.utils (the parent utils.py file)
and also exports the SCIM client from this directory.
"""
import sys
import importlib.util
from pathlib import Path

# Import SCIM client from this directory
from migration.utils.scim_client import QaseScimClient

# Import everything from the parent utils.py file
# We need to do this because Python treats utils/ as a package, so we need to
# explicitly load utils.py and re-export its contents
_parent_utils_path = Path(__file__).parent.parent / 'utils.py'
if _parent_utils_path.exists():
    spec = importlib.util.spec_from_file_location('migration.utils_module', _parent_utils_path)
    if spec and spec.loader:
        utils_module = importlib.util.module_from_spec(spec)
        sys.modules['migration.utils_module'] = utils_module
        spec.loader.exec_module(utils_module)
        
        # Re-export all public items from utils.py
        MigrationMappings = utils_module.MigrationMappings
        MigrationStats = utils_module.MigrationStats
        retry_with_backoff = utils_module.retry_with_backoff
        extract_entities_from_response = utils_module.extract_entities_from_response
        to_dict = utils_module.to_dict
        format_datetime = utils_module.format_datetime
        format_date = utils_module.format_date
        preserve_or_hash_id = utils_module.preserve_or_hash_id
        chunks = utils_module.chunks
        convert_uuids_to_strings = utils_module.convert_uuids_to_strings
        QaseRawApiClient = utils_module.QaseRawApiClient
        PARALLEL_PROJECT_MAPPING_ATTRS = utils_module.PARALLEL_PROJECT_MAPPING_ATTRS
        fork_mappings_for_parallel_project = utils_module.fork_mappings_for_parallel_project
        merge_parallel_project_into_main = utils_module.merge_parallel_project_into_main
        merge_migration_stats = utils_module.merge_migration_stats

        __all__ = [
            'QaseScimClient',
            'MigrationMappings',
            'MigrationStats',
            'retry_with_backoff',
            'extract_entities_from_response',
            'to_dict',
            'format_datetime',
            'format_date',
            'preserve_or_hash_id',
            'chunks',
            'convert_uuids_to_strings',
            'QaseRawApiClient',
            'PARALLEL_PROJECT_MAPPING_ATTRS',
            'fork_mappings_for_parallel_project',
            'merge_parallel_project_into_main',
            'merge_migration_stats',
        ]
    else:
        # Fallback: only export SCIM client if utils.py can't be loaded
        __all__ = ['QaseScimClient']
else:
    # Fallback: only export SCIM client if utils.py doesn't exist
    __all__ = ['QaseScimClient']
