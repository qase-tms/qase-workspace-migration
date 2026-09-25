"""Preflight check: validate config and connectivity BEFORE running the migration.

Run:  python preflight.py [config.json]

Checks, in order:
  1. Config file parses; both API tokens present and not placeholders
  2. Source workspace answers (GET /v1/project) and every code in
     projects.import exists there
  3. Target workspace answers, and users.default resolves to a target user
  4. Target projects that already hold data are flagged, since the run skips
     them unless resuming or migration.allow_existing_target is true
  5. SCIM tokens answer, when users.migrate is true

Exit code 0 = all green; 1 = at least one failure. Nothing is written.
"""

import sys

if sys.version_info < (3, 11):
    sys.exit(
        f"This migration requires Python 3.11 or newer "
        f"(found {sys.version_info.major}.{sys.version_info.minor})."
    )

import requests

from migration.config import (
    ConfigError,
    api_base_url,
    is_dedicated_cluster,
    load_config,
    missing_projects,
    resolve_default_user,
    select_projects,
)
from migration.run_report import LEVELS, normalise_level

_PLACEHOLDER_MARKERS = ("<", ">", "your-", "YOUR_", "changeme", "xxxx")

_results = []


def _report(name: str, ok: bool, detail: str = ""):
    print(f"  {'✅' if ok else '❌'} {name}" + (f": {detail}" if detail else ""))
    _results.append(ok)


def _warn(name: str, detail: str = ""):
    print(f"  ⚠️  {name}" + (f": {detail}" if detail else ""))


def _looks_placeholder(value: str) -> bool:
    return any(marker in value for marker in _PLACEHOLDER_MARKERS)


def _list_projects(block: dict) -> list:
    """Every project in a workspace, as dicts with at least code and counts."""
    url = f"{api_base_url(block)}/v1/project"
    headers = {"Token": str(block.get("api_token"))}
    out, offset = [], 0
    while True:
        resp = requests.get(url, headers=headers, params={"limit": 100, "offset": offset}, timeout=(15, 60))
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        entities = ((resp.json() or {}).get("result") or {}).get("entities") or []
        out.extend(entities)
        if len(entities) < 100:
            return out
        offset += 100


def _scim_ok(block: dict) -> tuple:
    from qase_service import QaseService

    svc = QaseService(
        api_token=str(block.get("api_token")),
        host=str(block.get("host") or "qase.io"),
        ssl=block.get("ssl", True) is not False,
        scim_token=None,
        scim_host=block.get("scim_host") or None,
    )
    scheme = "http://" if block.get("ssl") is False else "https://"
    resp = requests.get(
        f"{scheme}{svc.scim_host}/scim/v2/Users",
        headers={"Authorization": f"Bearer {block.get('scim_token')}", "Accept": "application/scim+json"},
        params={"count": 100},
        timeout=(15, 60),
    )
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code} from {svc.scim_host}", None
    return True, svc.scim_host, resp.json()


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.json"

    print("\n- Config -")
    try:
        config, legacy_warnings = load_config(config_path)
    except ConfigError as e:
        _report(f"Config file {config_path}", False, str(e))
        return _finish()
    _report(f"Config file {config_path}", True, "parses OK")
    for warning in legacy_warnings:
        _warn("Renamed key", warning)

    config_ok = True
    for block_name in ("source", "target"):
        value = str((config.get(block_name) or {}).get("api_token") or "").strip()
        key = f"{block_name}.api_token"
        if not value:
            _report(key, False, f"missing/empty (or set QASE_{block_name.upper()}_API_TOKEN)")
            config_ok = False
        elif _looks_placeholder(value):
            _report(key, False, f"looks like a placeholder: {value[:12]!r}")
            config_ok = False
        else:
            _report(key, True)

    raw_level = (config.get("logging") or {}).get("level")
    if raw_level and normalise_level(raw_level) == "info" and str(raw_level).lower() != "info":
        _warn(f"logging.level {raw_level!r} is not recognised", f"falling back to 'info'; valid: {sorted(LEVELS)}")

    if not config_ok:
        return _finish()

    source, target = config["source"], config["target"]

    print("\n- Source workspace -")
    if is_dedicated_cluster(source.get("host")):
        _report("Source host", True, f"dedicated cluster: {api_base_url(source)}")
    try:
        source_projects = _list_projects(source)
    except (RuntimeError, requests.exceptions.RequestException) as e:
        _report("Source auth (GET /v1/project)", False, str(e)[:200])
        return _finish()
    codes = [p.get("code") for p in source_projects if p.get("code")]
    _report("Source auth (GET /v1/project)", True, f"{len(codes)} project(s)")

    unknown = missing_projects(config, codes)
    selected = select_projects(config, codes)
    if unknown:
        _report("projects.import", False, f"not in the source workspace: {unknown}")
    elif not selected:
        _report("projects.import", False, "empty: list project codes, or set projects.import_all: true")
    else:
        _report("projects.import", True, f"{len(selected)} project(s): {selected}")

    print("\n- Target workspace -")
    if is_dedicated_cluster(target.get("host")):
        _report("Target host", True, f"dedicated cluster: {api_base_url(target)}")
    try:
        target_projects = _list_projects(target)
    except (RuntimeError, requests.exceptions.RequestException) as e:
        _report("Target auth (GET /v1/project)", False, str(e)[:200])
        return _finish()
    _report("Target auth (GET /v1/project)", True, f"{len(target_projects)} project(s)")

    from qase_service import QaseService
    from migration.create.projects import project_has_data

    target_service = QaseService(
        api_token=str(target.get("api_token")),
        host=str(target.get("host") or "qase.io"),
        ssl=target.get("ssl", True) is not False,
    )
    try:
        user_id, detail = resolve_default_user(target_service, (config.get("users") or {}).get("default"))
        _report("users.default", user_id is not None, detail)
    except Exception as e:  # noqa: BLE001
        _report("users.default", False, str(e)[:200])

    allow_existing = bool((config.get("migration") or {}).get("allow_existing_target"))
    resuming = bool((config.get("options") or {}).get("resume"))
    by_code = {str(p.get("code")).upper(): p for p in target_projects}
    for code in selected:
        existing = by_code.get(code.upper())
        if existing and project_has_data(existing):
            if allow_existing:
                _warn(f"Target project {code}", "already has data; migration.allow_existing_target is true, so it will be added to")
            elif resuming:
                _warn(f"Target project {code}", "already has data; resuming, so entities in the mappings file are reused")
            else:
                _report(
                    f"Target project {code}", False,
                    "already has data and would be skipped. Resume the previous run, or set "
                    "migration.allow_existing_target: true",
                )

    users = config.get("users") or {}
    if users.get("migrate"):
        print("\n- SCIM (users.migrate is true) -")
        for block_name, block in (("source", source), ("target", target)):
            if not str(block.get("scim_token") or "").strip():
                _report(f"{block_name}.scim_token", False, "required when users.migrate is true")
                continue
            try:
                ok, detail, body = _scim_ok(block)
            except requests.exceptions.RequestException as e:
                ok, detail, body = False, str(e)[:200], None
            _report(f"{block_name} SCIM", ok, detail)
            if ok and block_name == "source" and body:
                people = body.get("Resources") or []
                active = sum(1 for u in people if u.get("active", True))
                total = body.get("totalResults", len(people))
                _warn(
                    "Source users",
                    f"{active} active, {len(people) - active} inactive in the first {len(people)} of {total}. "
                    + ("users.only_active is false, so inactive users are created too and consume seats"
                       if users.get("only_active") is False else "inactive users are not created"),
                )
        if users.get("create"):
            _warn("users.create is true", "missing users are created in the target through SCIM, and each consumes a seat")

    try:
        import openpyxl  # noqa: F401
    except ImportError:
        _warn("openpyxl not installed", "stats/*.xlsx will not be written; run pip install -r requirements.txt")

    return _finish()


def _finish():
    failed = _results.count(False)
    print()
    if failed:
        print(f"❌ Preflight FAILED, {failed} check(s) failed. Fix the items above before migrating.")
        sys.exit(1)
    print("✅ Preflight passed, ready to run: python start.py")
    sys.exit(0)


if __name__ == "__main__":
    main()
