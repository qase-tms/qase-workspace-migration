# Changelog

Notable changes to this migration, newest first. Versions follow
[semantic versioning](https://semver.org) read from the customer's side: a
**major** bump means your `config.json` needs changing, a **minor** means new
capability that needs no action from you, and a **patch** means fixes and
documentation.

## 1.3.1 (2026-10-16)

### Fixed
- **Attachments that fail to download are no longer dropped in silence.** A
  download that came back empty was skipped without a single line in the log, and
  the failure behind it was only written at debug level. That is how a run could
  report "119 of 386 attachments missing" with nothing to explain it. Each one
  now logs a warning, and the step ends with one line stating the shortfall and
  what it means for the cases and results that referenced those files.
- **Results that lose their test case are now reported.** Where a result names a
  source test case that was not found among the migrated ones, Qase creates a
  stand-in case ("Automated Test 123") and the result is detached from the real
  case. The run now reports this as an error, with the source case ids involved,
  rather than leaving you to spot it in the UI. Results that carry no test case
  at all are reported separately as a warning, since that is the documented
  unlinked-results behaviour and means something quite different.

## 1.3.0 (2026-10-16)

### Fixed
- **Custom result statuses are now preserved where the target workspace has
  them.** 1.2.0 reported them instead of changing them silently, but still sent
  every custom status as `skipped`, so recreating a status in the target
  workspace made no difference. The status is now sent as it is, which works when
  the target defines the same one under Workspace settings > Fields > Result
  status. If the target does not have it, that single result is retried with the
  status from `results.status_map`, or `skipped` if you have not set one, so an
  unknown status costs one changed result rather than a rejected batch.
- `results.status_map` entries now match loosely, so `Passed with comment`,
  `passed-with-comment` and `passed_with_comment` are the same entry. Previously
  a mapping written with spaces never matched the status as the API names it,
  which meant a configured mapping was ignored.

## 1.2.1 (2026-10-16)

### Documentation
- Three limits of the Qase API are now written down in the README, having only
  been discovered when a customer hit them: a **defect's creation date** cannot be
  set, so migrated defects carry the migration date; **defect IDs** cannot be set,
  so they are renumbered in the target; and an **assignee** cannot be set
  anywhere, on defects, on the member shown per case inside a run, or on a plan's
  cases. Authors are migrated where the API allows it.

## 1.2.0 (2026-10-16)

### Fixed
- **A project whose test cases did not all migrate no longer has its runs and
  results migrated against them.** Previously the run continued, and every result
  whose case was missing was given a stand-in case ("Automated Test 123"): one
  project ended up with 5,238 stand-ins against 399 real test cases, with every
  run pointing at those instead of the real ones. The migration now stops that
  project, says how many cases are missing, and tells you to re-run it.
- **Custom fields that exist in the target but are not enabled for the project
  being migrated are now enabled automatically.** Using one that is not enabled
  made the API reject the whole batch of 20 test cases with
  `The field name is not valid`, so cases went missing in blocks. If the field
  cannot be updated, the run says which one and which projects to enable it for.
- **Test runs keep their environment.** The environment was never sent, so every
  migrated run showed none. Environments whose host is empty also no longer fail
  to migrate.
- **Test runs keep their tags.** They were never sent.
- **Timestamps no longer shift.** A timezone offset was being discarded rather
  than converted, so a run started 07:43 in Oslo arrived as 08:43.
- **Retest and In progress results keep their status.** Both were being turned
  into Blocked, although Qase accepts them.
- **Test plans that cannot be migrated are reported.** A plan whose cases are
  missing was skipped with only a line in the log file; it is now an error in the
  run report, naming the plan.
- Rate limiting: the results step now waits exactly as long as the API asks
  instead of guessing.

### Added
- `results.status_map` maps a custom source status onto one Qase accepts, for
  example `{"Passed with comment": "passed"}`. Qase has a fixed set of result
  statuses, so a custom one has to land on one of them. Anything unmapped is
  migrated as `skipped` and reported by name, rather than changed silently.
- `options.api_calls_per_minute` sets the pacing of the attachment step, now
  defaulting to 600 per minute rather than 1000. Qase allows 1000 per minute on
  Enterprise and 600 on other plans, and pacing above your real limit only turns
  throughput into rate-limit retries.

## 1.1.1 (2026-10-15)

### Fixed
- **Test cases no longer fail for a whole project when a step has an
  attachment.** Qase returns a step's attachments as objects in some payloads
  and as plain hashes in others. The migration assumed hashes, and an object
  raised `TypeError: unhashable type: 'dict'`, which aborted the entire test
  case step for that project and left it with zero cases. All attachment
  references, on cases and on steps, now accept either shape. If you hit this,
  re-run the affected projects into an empty target.
- **Environments with no host no longer fail with
  `The host must be a string`.** An empty host was sent as `""`, which Qase
  converts to null and then rejects. It is now left out of the request, and an
  environment with no slug gets one derived from its title.

## 1.1.0 (2026-10-09)

### Added
- **Authorship now carries over without SCIM.** With `users.migrate: true` and
  `users.create: false`, source users are matched to the users already in your
  target workspace **by email address**, so migrated cases, runs and results keep
  the right author. SCIM is an Enterprise feature and is now only needed to
  *create* users. If you are not on Enterprise, create the people you need in the
  target workspace first with the same email addresses; anyone unmatched falls
  back to `users.default` as before. Previously `users.migrate: true` without
  SCIM tokens stopped the run with an error.
- Preflight reports how many of your users it can match, for example
  `User matching by email: 4 of 8 source users exist in the target`, and names
  the shortfall before you run rather than after.

### Fixed
- `users.only_active: false` recreates users who are deactivated in the source as
  deactivated in the target, so they take no seat. The documentation and
  preflight previously implied they consumed a seat like active users, which
  could have made this setting look more expensive than it is.

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
