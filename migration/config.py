"""
Configuration loading for the workspace migration.

``config.json`` is read once, validated, and has secrets from the environment
applied on top. Keys renamed for consistency with the other Qase migrations are
still read under their old spelling, with a warning naming the new one, so an
existing config keeps working.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

# Secrets may come from the environment instead of config.json, and the
# environment always wins. This keeps tokens out of a file that might be
# pasted into a support ticket.
ENV_OVERRIDES = {
    "QASE_SOURCE_API_TOKEN": ("source", "api_token"),
    "QASE_TARGET_API_TOKEN": ("target", "api_token"),
    "QASE_SOURCE_SCIM_TOKEN": ("source", "scim_token"),
    "QASE_TARGET_SCIM_TOKEN": ("target", "scim_token"),
}

PUBLIC_CLOUD_HOST = "qase.io"


class ConfigError(Exception):
    """config.json is missing, unreadable, or not a JSON object."""


def load_config(path: str = "config.json") -> Tuple[Dict[str, Any], List[str]]:
    """
    Read config.json, apply environment overrides and legacy key fallbacks.

    Returns the config and a list of warnings about legacy keys, to be logged
    once logging is set up.
    """
    if not os.path.exists(path):
        raise ConfigError(
            f"Config file not found: {path}. "
            f"Copy config.example.json to config.json and fill in your tokens."
        )
    try:
        with open(path, "r", encoding="utf-8") as handle:
            config = json.load(handle)
    except json.JSONDecodeError as e:
        raise ConfigError(f"Config file {path} is not valid JSON: {e}") from e
    except OSError as e:
        raise ConfigError(f"Config file {path} could not be read: {e}") from e
    if not isinstance(config, dict):
        raise ConfigError(f"Config file {path} must contain a JSON object")

    for env_var, (block, key) in ENV_OVERRIDES.items():
        value = os.environ.get(env_var)
        if value and value.strip():
            config.setdefault(block, {})[key] = value.strip()

    return config, _apply_legacy_keys(config)


def _apply_legacy_keys(config: Dict[str, Any]) -> List[str]:
    """Read renamed keys under their old names. New names win when both are set."""
    warnings: List[str] = []
    options = config.get("options") or {}
    projects = config.setdefault("projects", {})
    cases = config.setdefault("cases", {})
    users = config.setdefault("users", {})

    if "only_projects" in options and "import" not in projects and "import_all" not in projects:
        only = [p for p in (options.get("only_projects") or []) if str(p).strip()]
        if only:
            projects["import"] = only
        else:
            projects["import_all"] = True
        warnings.append("options.only_projects is renamed: use projects.import (or projects.import_all: true)")
    if "skip_projects" in options and "exclude" not in projects:
        projects["exclude"] = options.get("skip_projects") or []
        warnings.append("options.skip_projects is renamed: use projects.exclude")
    if "preserve_ids" in options and "preserve_ids" not in cases:
        cases["preserve_ids"] = bool(options.get("preserve_ids"))
        warnings.append("options.preserve_ids is renamed: use cases.preserve_ids")
    if "inactive" in users and "only_active" not in users:
        users["only_active"] = not bool(users.get("inactive"))
        warnings.append(
            "users.inactive is replaced by users.only_active, with the opposite meaning: "
            f"users.inactive: {str(bool(users.get('inactive'))).lower()} is "
            f"users.only_active: {str(users['only_active']).lower()}"
        )
    return warnings


def is_dedicated_cluster(host: str) -> bool:
    return bool(host) and str(host).strip().lower() != PUBLIC_CLOUD_HOST


def api_base_url(block: Dict[str, Any]) -> str:
    """``https://api.qase.io`` on the public cloud, ``https://api-<host>`` on a dedicated cluster."""
    host = str(block.get("host") or PUBLIC_CLOUD_HOST).strip()
    scheme = "http://" if block.get("ssl") is False else "https://"
    delimiter = "-" if is_dedicated_cluster(host) else "."
    return f"{scheme}api{delimiter}{host}"


def select_projects(config: Dict[str, Any], available: List[str]) -> List[str]:
    """
    Resolve projects.import_all / projects.import / projects.exclude against the
    source workspace's project codes. Matching is case-insensitive; exclude wins.
    """
    projects = config.get("projects") or {}
    by_upper = {str(code).upper(): code for code in available}
    exclude = {str(p).strip().upper() for p in (projects.get("exclude") or []) if str(p).strip()}
    if projects.get("import_all"):
        wanted = list(by_upper)
    else:
        wanted = [str(p).strip().upper() for p in (projects.get("import") or []) if str(p).strip()]
    out: List[str] = []
    for code in wanted:
        if code in exclude or code not in by_upper or by_upper[code] in out:
            continue
        out.append(by_upper[code])
    return out


def missing_projects(config: Dict[str, Any], available: List[str]) -> List[str]:
    """Codes in projects.import that do not exist in the source workspace."""
    projects = config.get("projects") or {}
    if projects.get("import_all"):
        return []
    have = {str(code).upper() for code in available}
    return [p for p in (projects.get("import") or []) if str(p).strip().upper() not in have]


def resolve_default_user(target_service: Any, value: Any) -> Tuple[Optional[int], str]:
    """
    ``users.default`` accepts a target user's email or numeric id.

    Returns ``(user_id, detail)``; ``user_id`` is None when an email matches no
    user in the target workspace.
    """
    if value is None or value == "":
        return 1, "users.default not set, using user id 1 (the workspace owner)"
    if isinstance(value, bool):
        return None, "users.default must be an email address or a user id"
    if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
        return int(value), f"user id {int(value)}"
    email = str(value).strip().lower()
    if "@" not in email:
        return None, f"users.default {value!r} is neither an email address nor a user id"

    from qase.api_client_v1.api.authors_api import AuthorsApi

    api = AuthorsApi(target_service.client)
    offset, limit = 0, 100
    while True:
        response = api.get_authors(limit=limit, offset=offset, type="user")
        entities = (getattr(getattr(response, "result", None), "entities", None)) or []
        for author in entities:
            if str(getattr(author, "email", "") or "").strip().lower() == email:
                return int(author.id), f"{email} is user id {author.id}"
        if len(entities) < limit:
            break
        offset += limit
    return None, f"no user with email {email} exists in the target workspace"
