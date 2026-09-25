# Changelog

Notable changes to this migration, newest first. Versions follow
[semantic versioning](https://semver.org) read from the customer's side: a
**major** bump means your `config.json` needs changing, a **minor** means new
capability that needs no action from you, and a **patch** means fixes and
documentation.

## 1.0.0 (2026-09-25)

First release as an official Qase product. The migration itself predates this
version; 1.0.0 marks the point at which it became supported software with a
documented configuration, a validation step and a support address.

### Added
- `python preflight.py` validates your configuration and both workspaces before
  a migration touches anything: tokens, project codes, `users.default`, SCIM
  tokens, and target projects that already hold data.
- `--dry-run` (or `QASE_DRY_RUN=1`) reads the source and reports what would be
  created, writing nothing to the target.
- An end-of-run **migration report** listing every warning and error, grouped by
  project, plus `stats/<prefix>_stats.json` and `.xlsx` with the same list.
- Five logging levels under `logging.level` (`error`, `warn`, `info`,
  `verbose`, `debug`). Warnings and errors always reach the console.
- `users.default` accepts the email address of a target user, not only a
  numeric id.
- `migration.allow_existing_target`: a target project that already holds data
  is skipped unless you are resuming or set this to `true`, so a second run
  cannot silently duplicate into it.
- `runs.link_to_plan` lets you choose, for runs started from a test plan,
  between keeping the plan link and keeping the run's exact case list. The
  Qase API cannot keep both. The default keeps the case list, as before. See
  "Test plans and test runs" in the README before changing it.
- Tokens may be supplied by environment variable instead of `config.json`.
- The version is the first line of every log file and appears in the run report.

### Fixed
- **Resuming no longer duplicates data.** `--resume` re-created suites, cases,
  milestones, runs, results, shared steps, configuration groups and defects that
  a previous run had already migrated, while reporting them as newly created.
  Everything recorded in the mappings file is now reused.
- A run whose results could not be read from the source is reported as an
  error and retried by `--resume`, instead of being treated as a run with no
  results.
- `users.default` was ignored for cases, runs and defects, whose unmapped
  authors were always attributed to user 1.
- On a dedicated cluster, SCIM is now called at `app-<host>`, matching the API
  at `api-<host>`.
- Transient failures (network errors, timeouts, 408, 429, 5xx) are retried on
  every request, honouring `Retry-After`. Several requests previously failed on
  the first rate-limit response.
- The run exits with code `1` when anything failed, so a script or scheduler
  can tell a clean run from a partial one.

### Changed
- The entry point is `python start.py [config.json]`, replacing
  `python migrate_workspace.py`. Settings come from `config.json`; the
  `--source-token`, `--target-token`, host, SSL, `--mappings-file`,
  `--preserve-ids`, `--only-projects` and `--skip-projects` flags are removed.
  `--resume` and `--skip-attachments` remain.
- `example_config.json` is now `config.example.json`.
- Renamed keys. The old spellings are still read, with a warning naming the
  new one:

  | Old | New |
  |---|---|
  | `options.only_projects` | `projects.import` (empty list means `projects.import_all: true`) |
  | `options.skip_projects` | `projects.exclude` |
  | `options.preserve_ids` | `cases.preserve_ids` |
  | `users.inactive: false` | `users.only_active: true` (opposite meaning) |

- Log files are `logs/<prefix>_workspace_<timestamp>.log`.
- Dependencies trimmed to what the code imports.
