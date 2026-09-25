"""
Creation module - functions to create entities in target Qase workspace.
"""
from migration.create.projects import migrate_projects
from migration.create.users import migrate_users
from migration.create.custom_fields import migrate_custom_fields
from migration.create.shared_parameters import migrate_shared_parameters
from migration.create.milestones import migrate_milestones
from migration.create.configurations import migrate_configurations
from migration.create.environments import migrate_environments
from migration.create.shared_steps import migrate_shared_steps
from migration.create.suites import migrate_suites
from migration.create.cases import migrate_cases
from migration.create.plans import migrate_plans
from migration.create.runs import migrate_runs
from migration.create.results import migrate_results
from migration.create.defects import migrate_defects
from migration.create.attachments import migrate_attachments_workspace
from migration.create.groups import migrate_groups

__all__ = [
    'migrate_projects',
    'migrate_users',
    'migrate_custom_fields',
    'migrate_shared_parameters',
    'migrate_milestones',
    'migrate_configurations',
    'migrate_environments',
    'migrate_shared_steps',
    'migrate_suites',
    'migrate_cases',
    'migrate_plans',
    'migrate_runs',
    'migrate_results',
    'migrate_defects',
    'migrate_attachments_workspace',
    'migrate_groups',
]
