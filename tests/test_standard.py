"""Unit tests for the parts of this migration that can be verified offline.

Config handling, host derivation, project selection, resume de-duplication,
retry semantics, logging levels and the end-of-run report. The migration as a
whole is exercised end to end against real workspaces, not here.

Run:  python -m pytest tests/ -q
"""

import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from migration import qase_http
from migration.config import (
    ConfigError,
    api_base_url,
    is_dedicated_cluster,
    load_config,
    missing_projects,
    resolve_default_user,
    select_projects,
)
from migration.run_report import (
    LEVELS,
    REPORT,
    VERBOSE,
    normalise_level,
    setup_run_logging,
    write_stats_files,
)
from migration.step_logging import set_current_project
from migration.utils import (
    MigrationMappings,
    MigrationStats,
    fork_mappings_for_parallel_project,
    merge_migration_stats,
)
from qase_service import QaseService


def _write(tmp_path, data, name="config.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data) if not isinstance(data, str) else data)
    return str(path)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def test_missing_config_fails_loudly(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(str(tmp_path / "nope.json"))


def test_invalid_json_fails_loudly(tmp_path):
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_config(_write(tmp_path, "{ not json"))


def test_non_object_config_fails(tmp_path):
    with pytest.raises(ConfigError, match="JSON object"):
        load_config(_write(tmp_path, "[1, 2]"))


def test_environment_overrides_file(tmp_path, monkeypatch):
    path = _write(tmp_path, {"source": {"api_token": "file"}, "target": {"api_token": "file"}})
    monkeypatch.setenv("QASE_SOURCE_API_TOKEN", "from-env")
    monkeypatch.setenv("QASE_TARGET_SCIM_TOKEN", "scim-env")
    config, _ = load_config(path)
    assert config["source"]["api_token"] == "from-env"
    assert config["target"]["api_token"] == "file"
    assert config["target"]["scim_token"] == "scim-env"


def test_legacy_keys_are_still_read_with_a_warning(tmp_path):
    path = _write(tmp_path, {
        "options": {"only_projects": ["ABC"], "skip_projects": ["XYZ"], "preserve_ids": True},
        "users": {"inactive": False},
    })
    config, warnings = load_config(path)
    assert config["projects"]["import"] == ["ABC"]
    assert config["projects"]["exclude"] == ["XYZ"]
    assert config["cases"]["preserve_ids"] is True
    assert config["users"]["only_active"] is True
    assert len(warnings) == 4


def test_empty_legacy_only_projects_means_all(tmp_path):
    config, _ = load_config(_write(tmp_path, {"options": {"only_projects": []}}))
    assert config["projects"]["import_all"] is True


def test_new_keys_win_over_legacy(tmp_path):
    config, warnings = load_config(_write(tmp_path, {
        "projects": {"import": ["NEW"]},
        "options": {"only_projects": ["OLD"]},
    }))
    assert config["projects"]["import"] == ["NEW"]
    assert warnings == []


def test_example_config_parses_and_has_only_placeholders():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config, warnings = load_config(os.path.join(root, "config.example.json"))
    assert warnings == []
    assert config["source"]["api_token"].startswith("<")
    assert config["target"]["api_token"].startswith("<")
    assert config["migration"]["allow_existing_target"] is False


# --------------------------------------------------------------------------
# Hosts
# --------------------------------------------------------------------------

@pytest.mark.parametrize("host,expected", [
    ("qase.io", "https://api.qase.io"),
    ("acme.qase.io", "https://api-acme.qase.io"),
])
def test_api_base_url(host, expected):
    assert api_base_url({"host": host}) == expected


def test_dedicated_cluster_detection():
    assert is_dedicated_cluster("qase.io") is False
    assert is_dedicated_cluster("acme.qase.io") is True


@pytest.mark.parametrize("host,scim", [
    ("qase.io", "app.qase.io"),
    ("acme.qase.io", "app-acme.qase.io"),
])
def test_scim_host_follows_api_host(host, scim):
    assert QaseService("t", host=host).scim_host == scim


def test_sdk_clients_carry_the_retry_policy():
    svc = QaseService("t")
    for client in (svc.client, svc.client_v2):
        retries = client.rest_client.pool_manager.connection_pool_kw["retries"]
        assert 429 in retries.status_forcelist and 408 in retries.status_forcelist
        assert 404 not in retries.status_forcelist


# --------------------------------------------------------------------------
# Project selection
# --------------------------------------------------------------------------

AVAILABLE = ["ABC", "Def", "GHI"]


def test_select_import_list_is_case_insensitive():
    assert select_projects({"projects": {"import": ["abc", "DEF"]}}, AVAILABLE) == ["ABC", "Def"]


def test_select_import_all_minus_exclude():
    assert select_projects({"projects": {"import_all": True, "exclude": ["ghi"]}}, AVAILABLE) == ["ABC", "Def"]


def test_select_nothing_when_unset():
    assert select_projects({}, AVAILABLE) == []


def test_missing_projects_are_reported():
    assert missing_projects({"projects": {"import": ["ABC", "NOPE"]}}, AVAILABLE) == ["NOPE"]
    assert missing_projects({"projects": {"import_all": True}}, AVAILABLE) == []


# --------------------------------------------------------------------------
# users.default
# --------------------------------------------------------------------------

def test_default_user_accepts_an_id():
    assert resolve_default_user(None, 7)[0] == 7
    assert resolve_default_user(None, "7")[0] == 7


def test_default_user_unset_is_the_owner():
    assert resolve_default_user(None, None)[0] == 1


def test_default_user_rejects_nonsense():
    assert resolve_default_user(None, "someone")[0] is None
    assert resolve_default_user(None, True)[0] is None


def test_default_user_resolves_an_email(monkeypatch):
    from qase.api_client_v1.api import authors_api

    page = SimpleNamespace(result=SimpleNamespace(entities=[
        SimpleNamespace(id=4, email="Owner@Example.com"),
    ]))
    monkeypatch.setattr(authors_api.AuthorsApi, "get_authors", lambda self, **kw: page)
    user_id, _ = resolve_default_user(SimpleNamespace(client=None), "owner@example.com")
    assert user_id == 4
    user_id, detail = resolve_default_user(SimpleNamespace(client=None), "nobody@example.com")
    assert user_id is None and "no user" in detail


def test_unmapped_author_uses_the_configured_default():
    mappings = MigrationMappings()
    mappings.default_user_id = 9
    mappings.users = {1: 2}
    assert mappings.get_user_id(1) == 2
    assert mappings.get_user_id(5) == 9


# --------------------------------------------------------------------------
# Resume
# --------------------------------------------------------------------------

def test_loaded_mapping_keys_are_integers(tmp_path):
    """JSON keys are strings; every 'already migrated' check compares integers."""
    path = tmp_path / "mappings.json"
    path.write_text(json.dumps({
        "suites": {"P": {"1": 10}},
        "cases": {"P": {"5": 50}},
        "runs": {"P": {"7": 70}},
        "shared_steps": {"P": {"abc123": "def456"}},
        "results_done": {"P": [7]},
    }))
    m = MigrationMappings()
    m.load_from_file(str(path))
    assert m.suites == {"P": {1: 10}}
    assert m.cases == {"P": {5: 50}}
    assert m.runs == {"P": {7: 70}}
    assert m.shared_steps == {"P": {"abc123": "def456"}}
    assert m.results_done == {"P": [7]}


def test_mappings_round_trip(tmp_path):
    m = MigrationMappings()
    m.cases = {"P": {1: 2}}
    m.results_done = {"P": [3]}
    path = str(tmp_path / "m.json")
    m.save_to_file(path)
    loaded = MigrationMappings()
    loaded.load_from_file(path)
    assert loaded.cases == {"P": {1: 2}} and loaded.results_done == {"P": [3]}


def test_parallel_fork_sees_previous_run_but_does_not_share_dicts():
    base = MigrationMappings()
    base.cases = {"P": {1: 2}}
    base.default_user_id = 5
    fork = fork_mappings_for_parallel_project(base)
    assert fork.cases == {"P": {1: 2}}
    assert fork.default_user_id == 5
    fork.cases["P"][3] = 4
    assert base.cases == {"P": {1: 2}}


def test_suites_already_migrated_are_not_created_again(monkeypatch):
    from migration.create import suites as suites_mod
    from migration.extract import suites as extract_mod

    all_suites = {1: {"id": 1, "title": "Root"}, 2: {"id": 2, "title": "Child"}}
    tree = {None: [1], 1: [2]}
    monkeypatch.setattr(extract_mod, "extract_suites", lambda svc, code: (all_suites, tree))
    created = []

    class FakeSuitesApi:
        def __init__(self, client):
            pass

        def create_suite(self, code, suite_create):
            created.append(suite_create.title)
            return SimpleNamespace(status=True, result=SimpleNamespace(id=100 + len(created)))

    monkeypatch.setattr(suites_mod, "SuitesApi", FakeSuitesApi)
    mappings = MigrationMappings()
    mappings.suites = {"P": {1: 11}}
    stats = MigrationStats()
    result = suites_mod.migrate_suites(None, SimpleNamespace(client=None), "P", "P", mappings, stats)
    assert created == ["Child"]
    assert result == {1: 11, 2: 101}
    assert stats.entities_reused["suites"] == 1


# --------------------------------------------------------------------------
# Retry policy against a local server
# --------------------------------------------------------------------------

class _Script(BaseHTTPRequestHandler):
    responses = []
    hits = 0

    def do_GET(self):
        type(self).hits += 1
        status, headers = type(self).responses.pop(0) if type(self).responses else (200, {})
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(qase_http._BackoffRetry, "get_backoff_time", lambda self: 0)
    httpd = HTTPServer(("127.0.0.1", 0), _Script)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _Script.hits = 0
    yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    httpd.shutdown()


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_transient_status_is_retried(server, status):
    _Script.responses = [(status, {}), (200, {})]
    assert qase_http.get(server, timeout=5).status_code == 200
    assert _Script.hits == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_deterministic_4xx_fails_fast(server, status):
    _Script.responses = [(status, {})]
    assert qase_http.get(server, timeout=5).status_code == status
    assert _Script.hits == 1


def test_retry_after_is_honoured(server, monkeypatch):
    slept = []
    monkeypatch.setattr(qase_http.time, "sleep", slept.append)
    _Script.responses = [(429, {"Retry-After": "2"}), (200, {})]
    assert qase_http.get(server, timeout=5).status_code == 200
    assert slept == [2.0]


def test_short_retry_after_never_undercuts_backoff(monkeypatch):
    slept = []
    monkeypatch.setattr(qase_http.time, "sleep", slept.append)
    retry = qase_http.build_retry()
    monkeypatch.setattr(type(retry), "get_backoff_time", lambda self: 12.0)
    monkeypatch.setattr(type(retry), "get_retry_after", lambda self, response: 1.0)
    retry.sleep(response=object())
    assert slept == [12.0]


# --------------------------------------------------------------------------
# Logging and the report
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (None, "info"), ("WARN", "warn"), ("warning", "warn"), ("verbose", "verbose"),
    ("debug", "debug"), ("nonsense", "info"),
])
def test_normalise_level(raw, expected):
    assert normalise_level(raw) == expected


def test_verbose_sits_between_info_and_debug():
    assert LEVELS["debug"] < VERBOSE < LEVELS["info"]


def test_warnings_become_report_lines_grouped_by_project(tmp_path):
    setup_run_logging("t", level="info", write_to_file=True, log_dir=str(tmp_path))
    log = logging.getLogger("migration.create.cases")
    set_current_project("ABC")
    log.warning("case 5 dropped a field")
    set_current_project(None)
    log.error("workspace-level failure")
    log.info("progress is not an issue")
    logging.getLogger("urllib3.connectionpool").warning("Retrying (Retry(total=5))")
    assert [(i["project"], i["level"], i["step"]) for i in REPORT.issues] == [
        ("ABC", "warn", "cases"),
        ("workspace", "error", "cases"),
    ]


def test_log_file_starts_with_the_version(tmp_path):
    paths = setup_run_logging("t2", write_to_file=True, log_dir=str(tmp_path), prefix="acme")
    assert os.path.basename(paths["full"]) == "acme_workspace_t2.log"
    with open(paths["full"], encoding="utf-8") as handle:
        assert handle.readline().startswith("# qase-workspace-migration v")


def test_no_log_files_when_disabled(tmp_path):
    paths = setup_run_logging("t3", write_to_file=False, log_dir=str(tmp_path / "logs"))
    assert paths["full"] is None
    assert not (tmp_path / "logs").exists()


def test_stats_files_include_the_issue_list(tmp_path):
    setup_run_logging("t4", write_to_file=False)
    logging.getLogger("x").warning("something skipped")
    stats = MigrationStats()
    stats.add_entity("cases", 3, 3)
    stats.add_reused("cases", 2)
    written = write_stats_files(stats, prefix="acme", stats_dir=str(tmp_path))
    data = json.loads(open(written[0], encoding="utf-8").read())
    assert data["entities"] == [{"entity": "cases", "source": 3, "migrated": 3, "reused_from_previous_run": 2}]
    assert data["issues"][0]["message"] == "something skipped"
    assert any(p.endswith("acme_stats.xlsx") for p in written)


def test_worker_stats_merge_reused_counts():
    main, worker = MigrationStats(), MigrationStats()
    worker.add_entity("runs", 4, 4)
    worker.add_reused("runs", 4)
    merge_migration_stats(main, worker)
    assert main.entities_reused == {"runs": 4}


# --------------------------------------------------------------------------
# Milestones and plans
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("2026-07-02T21:00:00-03:00", 1783036800),   # same instant as 2026-07-03 00:00 UTC
    ("2026-07-03T00:00:00Z", 1783036800),
    ("2026-07-03", 1783036800),
    (1783036800, 1783036800),
    (None, None),
    ("", None),
])
def test_milestone_due_date_is_a_unix_timestamp(raw, expected):
    from migration.create.milestones import due_date_timestamp

    assert due_date_timestamp(raw) == expected


def test_unreadable_due_date_is_dropped_not_fatal():
    from migration.create.milestones import due_date_timestamp

    assert due_date_timestamp("next tuesday") is None


def test_plan_cases_are_mapped_and_cached(monkeypatch):
    from qase.api_client_v1.api import plans_api
    from migration.create.runs import _plan_target_case_ids

    calls = []

    def fake_get_plan(self, code, id):
        calls.append(id)
        return SimpleNamespace(result=SimpleNamespace(cases=[
            SimpleNamespace(case_id=1), SimpleNamespace(case_id=2), SimpleNamespace(case_id=9),
        ]))

    monkeypatch.setattr(plans_api.PlansApi, "get_plan", fake_get_plan)
    cache = {}
    source = SimpleNamespace(client=None)
    assert _plan_target_case_ids(source, "P", 5, {1: 11, 2: 12}, cache) == {11, 12}
    assert _plan_target_case_ids(source, "P", 5, {1: 11, 2: 12}, cache) == {11, 12}
    assert calls == [5]


def test_example_config_defaults_to_keeping_run_scope():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config, _ = load_config(os.path.join(root, "config.example.json"))
    assert config["runs"]["link_to_plan"] is False


# --------------------------------------------------------------------------
# Users without SCIM: matching existing target users by email
#
# SCIM is an Enterprise feature. Creating users needs it; matching users that
# already exist in the target does not, because both sides are read through the
# public authors endpoint. These tests pin that down, since the difference
# decides whether migrated history carries real author names or one default user.
# --------------------------------------------------------------------------

def _user_service(scim_token=None):
    """A QaseService-shaped stub: only the SCIM attributes are consulted."""
    return SimpleNamespace(
        scim_token=scim_token,
        scim_client=None,
        client=SimpleNamespace(configuration=SimpleNamespace(host="https://api.qase.io/v1")),
    )


def _run_migrate_users(monkeypatch, config, source_users, target_users):
    from migration.create import users as users_module
    from migration.extract import users as extract_users_module

    # migrate_users imports extract_users inside the function, so patch it at
    # its source module rather than on the importer.
    monkeypatch.setattr(extract_users_module, "extract_users", lambda _svc: source_users)

    class _FakeAuthorsApi:
        def __init__(self, _client):
            pass

        def get_authors(self, limit=100, offset=0, type=None):
            entities = target_users if offset == 0 else []
            # extract_entities_from_response needs both status and result.
            return SimpleNamespace(status=True, result=SimpleNamespace(entities=entities))

    monkeypatch.setattr(users_module, "AuthorsApi", _FakeAuthorsApi)

    mappings = SimpleNamespace(users={}, user_uuid_mapping={}, user_email_mapping={})
    stats = SimpleNamespace(add_entity=lambda *a, **k: None)
    return users_module.migrate_users(
        _user_service(config.get("_source_scim")),
        _user_service(config.get("_target_scim")),
        mappings,
        stats,
        config,
    )


SOURCE_USERS = [
    {"id": 11, "email": "Ada@company.com", "name": "Ada", "is_active": True},
    {"id": 12, "email": "grace@company.com", "name": "Grace", "is_active": True},
    {"id": 13, "email": "gone@company.com", "name": "Gone", "is_active": False},
]
TARGET_USERS = [
    {"id": 91, "email": "ada@company.com", "name": "Ada", "is_active": True},
    {"id": 92, "email": "grace@company.com", "name": "Grace", "is_active": True},
]


def test_users_match_by_email_without_scim(monkeypatch):
    """No SCIM tokens: existing target users are still matched, case-insensitively."""
    mapping = _run_migrate_users(
        monkeypatch,
        {"users": {"migrate": True, "create": False, "default": 1}},
        SOURCE_USERS,
        TARGET_USERS,
    )
    assert mapping[11] == 91, "mixed-case source email should match the target user"
    assert mapping[12] == 92
    # A source user with no target match is pointed at users.default, so their
    # cases and runs still get a valid author rather than failing to create.
    assert mapping[13] == 1, "an unmatched source user falls back to users.default"


def test_user_creation_still_requires_scim(monkeypatch):
    """users.create is the part that genuinely needs SCIM, so it must still refuse."""
    with pytest.raises(ValueError, match="SCIM token is required"):
        _run_migrate_users(
            monkeypatch,
            {"users": {"migrate": True, "create": True, "default": 1}},
            SOURCE_USERS,
            TARGET_USERS,
        )


def test_migrate_users_disabled_returns_empty(monkeypatch):
    """users.migrate false stays a no-op, whatever else is set."""
    assert _run_migrate_users(
        monkeypatch,
        {"users": {"migrate": False, "create": True}},
        SOURCE_USERS,
        TARGET_USERS,
    ) == {}


# --------------------------------------------------------------------------
# Attachment references inside steps
#
# Qase returns a step's attachments as hash strings in some payloads and as
# objects in others. Using an object as a dict key raises "unhashable type:
# dict", which aborted the whole test case migration for a project mid-run.
# --------------------------------------------------------------------------

def test_source_attachment_hash_accepts_every_shape():
    from migration.create.cases import source_attachment_hash

    h = "a" * 32
    assert source_attachment_hash(h) == h, "plain hash string"
    assert source_attachment_hash({"hash": h}) == h, "object carrying a hash"
    assert source_attachment_hash(
        {"url": f"https://company.qase.io/attachment/{h}/screenshot.png"}
    ) == h, "object carrying only a url"
    assert source_attachment_hash(SimpleNamespace(hash=h)) == h, "SDK model object"
    assert source_attachment_hash({"file_name": "no-hash-here.png"}) is None
    assert source_attachment_hash(None) is None


def test_step_attachments_as_objects_do_not_crash():
    """The exact shape that raised TypeError: unhashable type: 'dict'."""
    from migration.create.cases import transform_case_data

    source_hash, target_hash = "b" * 32, "c" * 32
    case = {
        "id": 1,
        "title": "case with an attachment on a step",
        "steps": [{"action": "do the thing", "attachments": [{"hash": source_hash}]}],
    }
    mappings = SimpleNamespace(
        default_user_id=1,
        get_user_id=lambda _uid: 1,
        target_workspace_hash=None,
        custom_fields={},
    )

    case_data = transform_case_data(
        case, {}, {}, {}, {}, {}, {}, {source_hash: target_hash}, mappings
    )

    assert case_data is not None
    assert case_data["steps"][0]["attachments"] == [target_hash], (
        "a step attachment given as an object should map to the target hash"
    )


# --------------------------------------------------------------------------
# Environments: Qase turns "" into null and then rejects null
# --------------------------------------------------------------------------

def test_environment_payload_omits_empty_host(monkeypatch):
    """host="" is a 400 from the API; the field has to be left out entirely."""
    from migration.create import environments as env_module

    sent = []

    class _FakeEnvironmentsApi:
        def __init__(self, _client):
            pass

        def create_environment(self, code=None, environment_create=None):
            sent.append(environment_create.to_dict())
            return SimpleNamespace(status=True, result=SimpleNamespace(id=7))

    monkeypatch.setattr(env_module, "EnvironmentsApi", _FakeEnvironmentsApi)
    # imported inside the function, so patch it at its source module
    from migration.extract import environments as extract_env_module
    monkeypatch.setattr(
        extract_env_module, "extract_environments",
        lambda *a, **k: [
            {"id": 1, "title": "Staging", "slug": "staging", "host": ""},
            {"id": 2, "title": "Prod Cluster", "slug": None, "host": "https://prod"},
        ],
    )

    mappings = SimpleNamespace(environments={})
    stats = SimpleNamespace(add_entity=lambda *a, **k: None, add_reused=lambda *a, **k: None)
    env_module.migrate_environments(
        SimpleNamespace(client=None), SimpleNamespace(client=None),
        "SRC", "TGT", mappings, stats,
    )

    assert "host" not in sent[0], "an empty host must not be sent at all"
    assert sent[1]["host"] == "https://prod", "a real host must still be sent"
    assert sent[1]["slug"] == "prod-cluster", "a missing slug is derived from the title"


# --------------------------------------------------------------------------
# Timestamps: an offset must be converted, not discarded
# --------------------------------------------------------------------------

def test_format_datetime_converts_offsets_to_utc():
    """
    Qase reads a naive timestamp as UTC, so dropping "+01:00" moved every
    timestamp by an hour: a run started 07:43 in Oslo arrived as 08:43.
    """
    from migration.utils import format_datetime

    expected = "2026-02-09 07:43:25"
    assert format_datetime("2026-02-09T08:43:25+01:00") == expected
    assert format_datetime("2026-02-09T07:43:25+00:00") == expected
    assert format_datetime("2026-02-09T07:43:25Z") == expected
    assert format_datetime("2026-02-09T03:43:25-04:00") == expected
    # already naive: taken as written
    assert format_datetime("2026-02-09 07:43:25") == expected
    assert format_datetime(None) is None


# --------------------------------------------------------------------------
# Result statuses
# --------------------------------------------------------------------------

def test_statuses_the_api_accepts_are_not_downgraded():
    """retest and in_progress are valid; both used to be turned into blocked."""
    from migration.create.results import _map_v2_run_execution_status

    for status in ("passed", "failed", "blocked", "skipped", "invalid", "untested",
                   "retest", "in_progress"):
        assert _map_v2_run_execution_status(status) == status
    # the API rejects a space, so normalise it
    assert _map_v2_run_execution_status("in progress") == "in_progress"
    # A custom status is passed through, because the API accepts statuses the
    # target workspace defines. If the target does not have it, the send path
    # retries that one result with the fallback.
    assert _map_v2_run_execution_status("Passed with comment") == "passed_with_comment"
    from migration.create.results import status_fallback_for, set_status_overrides

    assert status_fallback_for("Passed with comment") == "skipped"
    set_status_overrides({"Passed with comment": "passed"})
    try:
        # with a mapping configured, that is where it goes, both on the way out
        # and in the fallback
        assert _map_v2_run_execution_status("Passed with comment") == "passed"
        assert status_fallback_for("passed-with-comment") == "passed"
    finally:
        set_status_overrides({})


def test_custom_status_map_from_config():
    """results.status_map decides where a custom source status lands."""
    from migration.create.results import set_status_overrides, _resolve_result_status_string

    set_status_overrides({"Passed with comment": "passed", "Needs retest": "retest"})
    try:
        assert _resolve_result_status_string({"status": "Passed with comment"}) == "passed"
        assert _resolve_result_status_string({"status": "needs retest"}) == "retest"
        # unmapped falls back, and the fallback is reported elsewhere
        assert _resolve_result_status_string({"status": "Something else"}) == "skipped"
        # a mapped name wins over the numeric status id
        assert _resolve_result_status_string(
            {"status": "Passed with comment", "status_id": 3}
        ) == "passed"
    finally:
        set_status_overrides({})


# --------------------------------------------------------------------------
# Runs: environment and tags were silently dropped
# --------------------------------------------------------------------------

def test_run_payload_carries_environment_and_tags(monkeypatch):
    from migration.create import runs as runs_module

    sent = []

    class _FakeRunsApi:
        def __init__(self, _client):
            pass

        def create_run(self, code=None, run_create=None):
            sent.append(run_create.to_dict())
            return SimpleNamespace(status=True, result=SimpleNamespace(id=55))

    monkeypatch.setattr(runs_module, "RunsApi", _FakeRunsApi)
    monkeypatch.setattr(runs_module, "extract_run_cases", lambda *a, **k: [])
    monkeypatch.setattr(runs_module, "fetch_run_detail_json", lambda *a, **k: {})
    # imported inside migrate_runs, so patch it at its source module
    from migration.extract import milestones as milestones_module
    monkeypatch.setattr(milestones_module, "extract_milestones", lambda *a, **k: [])

    source_runs = [{
        "id": 7,
        "title": "Systemtest Knowit",
        "environment": {"slug": "systemtest", "title": "Systemtest"},
        "tags": ["systemtest", {"title": "nightly"}],
    }]
    mappings = SimpleNamespace(
        runs={}, users={}, milestones={}, get_user_id=lambda _u: 1, trace=None,
        default_user_id=1,
    )
    stats = SimpleNamespace(
        add_entity=lambda *a, **k: None,
        add_error=lambda *a, **k: None,
        add_reused=lambda *a, **k: None,
    )

    runs_module.migrate_runs(
        SimpleNamespace(client=None), SimpleNamespace(client=None),
        "SRC", "TGT", {}, {}, {}, {}, {}, mappings, stats,
        source_runs_precached=source_runs,
        environment_mapping={"slug:systemtest": 9, "title:systemtest": 9},
    )

    assert sent, "a run should have been created"
    assert sent[0].get("environment_id") == 9, "the run's environment must be sent"
    assert sorted(sent[0].get("tags") or []) == ["nightly", "systemtest"], (
        "tags, including object-shaped ones, must be sent"
    )


def test_result_title_uses_the_case_title_not_a_placeholder():
    """
    The v1 results API returns no title for a result, so every migrated result
    was titled "Automated Test <id>" even when it was linked to the right case.
    The case title fetched during enrichment is now used instead.
    """
    from migration.create.results import _resolve_result_display_title

    # with the case title present, that is what the result is called
    assert _resolve_result_display_title(
        {"case_id": 608, "case_title": "Forvaltning FK_72_6"}, 608
    ) == "Forvaltning FK_72_6"

    # with no case at all, the placeholder is still the honest answer
    assert _resolve_result_display_title({"id": 9}, None) == "Automated Test 9"

    # a title on the result itself still wins
    assert _resolve_result_display_title(
        {"title": "from the result", "case_title": "from the case"}, 1
    ) == "from the result"


def test_step_attachments_do_not_become_stringified_objects():
    """
    Case step attachments arrive as objects as well as hashes. Stringifying an
    object produced hashes like "{'size': 455, 'mime': ...}", which then failed
    to download and were reported as missing attachments, while the real file
    was never collected.
    """
    from migration.extract.attachments import _ingest_attachment_like_items

    real = "a" * 32
    hashes, urls = set(), {}
    _ingest_attachment_like_items(
        [
            {"hash": real, "size": 455, "mime": "image/png"},
            real.replace("a", "b"),
            {"size": 123},  # nothing usable, must be ignored rather than stringified
        ],
        hashes,
        urls,
    )

    assert real in hashes
    assert "b" * 32 in hashes
    assert not any("size" in h or "{" in h for h in hashes), (
        f"an attachment object must never be used as a hash: {hashes}"
    )
    assert len(hashes) == 2


def test_unmatched_results_split_mapping_problems_from_deleted_cases(caplog):
    """
    A result naming a case the source still has is our bug. One naming a case
    the source no longer has is the documented unlinked-results case. Reporting
    both the same way cries wolf, which it did on a verification run.
    """
    from migration.create import results as R

    R.reset_unmatched_result_counters()
    R._UNMATCHED_WITH_CASE_ID.extend([5, 801])  # 5 still exists, 801 was deleted
    errors, warnings = [], []
    stats = SimpleNamespace(add_error=lambda entity, msg: errors.append(msg))

    with caplog.at_level(logging.WARNING, logger="migration.create.results"):
        R.report_unmatched_results("ECSE", stats, source_case_ids={5, 7, 9})

    joined = " ".join(r.getMessage() for r in caplog.records)
    assert errors, "a case that still exists must be reported as an error"
    assert "mapping problem" in errors[0]
    assert "5" in errors[0] and "801" not in errors[0]
    assert "no longer exist in the source" in joined
    assert "unlinked-results" in joined
