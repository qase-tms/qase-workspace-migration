# Qase workspace to Qase workspace

Copies test management data from one Qase workspace into another: projects, suites, cases, plans, runs, results, attachments and the entities they depend on, with the links between them. It runs on your machine, reads the source workspace, and writes to the target through the public Qase API.

Current version: see [VERSION](VERSION). Every release is listed in [CHANGELOG.md](CHANGELOG.md).

---

## 1. What this migrates

- **Source and target:** any two Qase workspaces. Qase Cloud (`qase.io`) and dedicated clusters (for example `company.qase.io`) are both supported, in either direction. A common case is Qase Cloud to a dedicated cluster.
- **Scope:** everything the public API can read from one workspace and write to another. The [coverage table](#2-coverage-table) lists what comes across, and [known limitations](#3-known-limitations) lists what does not.
- **Not supported:** anything the public API does not expose, such as roles, dashboards, saved queries, requirements and integration settings.

The source workspace is only read, never modified.

## 2. Coverage table

| Source entity | Qase entity in the target | Status | Notes |
|---|---|---|---|
| Projects | Projects | ✅ | Same project code. A target project that already holds data is skipped unless you resume or allow it, see [section 10](#10-re-run-and-resume-behaviour) |
| Users | Users (matched by email) | ✅ Optional | `users.migrate`. Missing users can be created through SCIM with `users.create` |
| Groups | Groups | ✅ Optional | `groups.create`, only when `users.migrate` is true |
| Custom fields | Custom fields | ✅ | Workspace-global. An existing field of the same title and type is reused |
| Shared parameters | Shared parameters | ✅ | |
| Attachments | Attachments | ✅ | References inside text are rewritten to the target's attachments |
| Milestones | Milestones | ✅ | Hierarchy and due dates |
| Configurations | Configuration groups and configurations | ✅ | |
| Environments | Environments | ✅ | |
| Shared steps | Shared steps | ✅ | Project-level only |
| Suites | Suites | ✅ | Hierarchy preserved |
| Test cases | Test cases | ✅ | Ids preserved with `cases.preserve_ids` |
| Test plans | Test plans | ✅ | Runs are linked to them only if you choose to, see [test plans and test runs](#test-plans-and-test-runs) |
| Test runs | Test runs | ✅ | Case scope, dates, milestone, configurations, completion, and optionally the plan link |
| Test results | Test results | ✅ | Status, comments, steps, attachments, author, timing |
| Defects | Defects | ⚠️ Partial | Created and resolved; links to runs and results may be missing |

## 3. Known limitations

These are limits of the public API or deliberate trade-offs. Read them before planning a move.

**Not migrated at all**

| Item | Why |
|---|---|
| Roles | Role definitions and assignments are not exposed by the public API |
| Workspace-level shared steps | Only project-level shared steps are migrated |
| Dashboards, saved queries | Not exposed by the public API |
| Requirements and their links | Not migrated |
| Traceability reports | Not migrated |
| Cases in review | Review workflow states are not migrated as a complete path |
| Aiden Test Converter tests and Aiden authorization settings | Not migrated |
| Project settings | Only what the migrated entities carry |
| Integrations (for example Jira) | Not migrated. Plan a separate step to re-point integrations to the new workspace |

**Migrated with a gap**

| Item | What to expect |
|---|---|
| **Test plan to run link** | You choose between keeping each run's plan link and keeping its exact case list. You cannot have both. See [test plans and test runs](#test-plans-and-test-runs) |
| **Defects** | Created and resolved, but linking to runs and results is not guaranteed through the public API. Verify in the target |
| **Unlinked results** | Results whose case was deleted in the source, or that came from an automated reporter, cannot be tied to a case. They appear as "Automated Test N" entries without suite structure. If the target project creates cases from automated results, each becomes a repository case, so **the target can show more cases than the source**: in our own testing, a source project with 76 cases and 239 deleted-but-still-referenced cases produced 519 cases in the target |
| **Parameterized results** | The API references the case only, not the parameter variation. All results land on a single variation |
| **Attachments over 32 MB** | Cannot be uploaded through the API. Skipped and named in the migration report; the case or result keeps its text but loses the reference |
| **Custom field of a different type** | If the target already has a field with the same title but a different type or entity, it is not reused, because a field's type cannot be changed. The field is reported, cases migrate, and only that field's values are missing. See [custom fields in a reused workspace](#custom-fields-in-a-reused-workspace) |
| **Rejected case batches** | Cases are created in batches of 20, and the API rejects a whole batch if one case is invalid. The report names the affected source case ids |
| **Estimated time on cases** | The bulk case API has no estimate field |
| **Milestone creation date** | The API does not accept one; the migration date is used |
| **Authors** | An author with no matching user in the target is attributed to `users.default` |

### Test plans and test runs

**This is a choice you make before migrating, with `runs.link_to_plan`.** It only affects runs that were started from a test plan in the source; every other run is migrated the same way either way.

The Qase API cannot recreate a plan-linked run exactly. When a run is created from a plan, Qase fills it with **all of the plan's current cases**, adding them to whatever case list the migration sends. The link also cannot be added to a run afterwards. So for each plan-linked run you keep one of two things:

| | `link_to_plan: false` (default) | `link_to_plan: true` |
|---|---|---|
| **Run shows its plan** | No | Yes |
| **Cases in the run** | Exactly the source run's cases | The source run's cases **plus** any plan cases it did not include |
| **Results** | All migrated | All migrated |
| **Run totals and pass rate** | Match the source | Inflated when the plan has cases the run did not cover. Those cases show as **untested** |
| **If someone adds cases to the plan later** | Migrated runs do not change | Qase adds them to every linked run, including old ones |

An example. The source plan "Release regression" has 40 cases, and last spring's run from it covered only 25 of them, all passed:

- With `false`, the target run has 25 cases, 25 passed, 100%, and no plan shown.
- With `true`, the target run shows the plan, but it has 40 cases: 25 passed and 15 untested.

When a run covered exactly its plan's cases, which is common, both choices give the same run and only `true` keeps the link.

**How to choose.** Keep the default if your reports, dashboards or audits rely on historical run totals and pass rates. Choose `true` if you navigate your history by test plan and can accept that some old runs show extra untested cases.

**See the effect before deciding.** With `link_to_plan: true`, a dry run (`python start.py --dry-run`) lists every run that would gain cases from its plan, and how many. For example:

```
WEB: run 44 would be linked to plan 1 and also get 15 plan case(s) it did not include, shown as untested (total 40 instead of 25)
WEB: 27 run(s) would be linked to their test plan; 1 of them would gain cases from the plan
```

If no run would gain cases, `true` costs nothing and keeps every link. Once runs have been migrated, the choice cannot be changed for them: delete the target project and migrate again.

### Custom fields in a reused workspace

Custom fields are workspace-global and a field's type cannot be changed after creation. If the target already has a field with the same title as a source field but a different type or entity, for example one left by an earlier trial run, the migration does not reuse it. Delete that field in the target (`Settings > Custom fields`) and run again. The report gives the field id.

## 4. Prerequisites

**Python 3.11 or newer.** Check with `python3 --version`.

**Source workspace access**

1. In the source workspace, create an API token at **Workspace > API tokens**.
2. Copy the token when it is shown.
3. Put it in `config.json` as `source.api_token`, or set the environment variable `QASE_SOURCE_API_TOKEN`.

The token's user must be able to see every project you are migrating. Read access is enough.

**Target workspace access**

1. In the target workspace, create an API token at **Workspace > API tokens**.
2. Put it in `config.json` as `target.api_token`, or set `QASE_TARGET_API_TOKEN`.

The token's user must be able to create projects and custom fields in the target workspace.

**SCIM tokens, only when migrating users** (`users.migrate: true`)

1. In each workspace, create a SCIM token at **Workspace > SCIM**.
2. Put them in `source.scim_token` and `target.scim_token`, or set `QASE_SOURCE_SCIM_TOKEN` and `QASE_TARGET_SCIM_TOKEN`.

The API token cannot manage users; that needs the SCIM token.

> **Seats.** With `users.create: true`, every source user with no match in the target is created there, and **each one consumes a seat**. `users.only_active: true`, the default, keeps deactivated source users out. Preflight reports how many source users are active and inactive before you run.

**Nothing needs to be created in the target beforehand.** Projects, custom fields and everything else are created by the migration.

## 5. Install

```bash
git clone https://github.com/qase-tms/qase-workspace-migration.git
cd qase-workspace-migration
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

On Windows, activate with `.venv\Scripts\activate`.

## 6. Configure

```bash
cp config.example.json config.json
```

Then edit `config.json`. Every key is listed below. `config.json` is gitignored; never commit it.

**`source` and `target`**, one block per workspace

| Key | Required | Default | Purpose |
|---|---|---|---|
| `api_token` | Yes | | API token for that workspace. `QASE_SOURCE_API_TOKEN` / `QASE_TARGET_API_TOKEN` override it |
| `host` | No | `qase.io` | `qase.io` for Qase Cloud, or your cluster's hostname such as `company.qase.io`. Qase Cloud is served at `api.qase.io`; a dedicated cluster at `api-<host>` |
| `ssl` | No | `true` | Use HTTPS. Leave it on |
| `scim_token` | When `users.migrate` is true | | SCIM token for that workspace |
| `scim_host` | No | derived | Override only if SCIM is not at `app.qase.io` (Cloud) or `app-<host>` (dedicated cluster) |

**`projects`**, which projects to migrate

| Key | Required | Default | Purpose |
|---|---|---|---|
| `import_all` | One of these two | `false` | Migrate every project in the source |
| `import` | One of these two | `[]` | Project codes to migrate, for example `["WEB", "API"]`. A code that does not exist in the source stops the run before anything is written |
| `exclude` | No | `[]` | Codes to leave out. Wins over both of the above |

**`users`**

| Key | Required | Default | Purpose |
|---|---|---|---|
| `default` | No | `1` | Target user for anything whose author has no match. **An email address**, such as `"qa.lead@company.com"`, or a numeric user id. An email must belong to a user in the target, or preflight fails. `1` is the workspace owner |
| `migrate` | No | `false` | Match source users to target users by email, so authors carry over. Needs both SCIM tokens |
| `create` | No | `false` | Create missing users in the target through SCIM. **Consumes seats** |
| `only_active` | No | `true` | When creating users, skip those deactivated in the source |
| `skip_creation_confirm` | No | `false` | When `create` is true, the run lists the users it will create and asks you to type `yes`. Set this to `true` for an unattended run. Without a terminal and without this, no users are created |

**`groups`**

| Key | Required | Default | Purpose |
|---|---|---|---|
| `create` | No | `false` | Create source groups in the target and add their members. Only when `users.migrate` is true |

**`cases`**

| Key | Required | Default | Purpose |
|---|---|---|---|
| `preserve_ids` | No | `false` | Keep source case ids in the target where possible, so `WEB-123` stays `WEB-123` |

**`runs`**

| Key | Required | Default | Purpose |
|---|---|---|---|
| `link_to_plan` | No | `false` | For runs started from a test plan: `false` keeps each run's exact cases and drops the plan link; `true` keeps the link, and the run also gets any plan cases it did not cover, shown as untested. **Read [test plans and test runs](#test-plans-and-test-runs) before changing it** |

**`migration`**

| Key | Required | Default | Purpose |
|---|---|---|---|
| `allow_existing_target` | No | `false` | Write into a target project that already holds data. Off by default, so a second run cannot duplicate into a project. A resumed run does not need it |

**`options`**, workspace-migration specific

| Key | Required | Default | Purpose |
|---|---|---|---|
| `mappings_file` | No | `mappings.json` | Where source to target ids are recorded. Needed to resume |
| `resume` | No | `false` | Same as `--resume` |
| `skip_attachments` | No | `false` | Same as `--skip-attachments`. Much faster for a trial, but the result is not a complete migration |
| `parallel_project_migration` | No | `true` | Migrate several projects at once |
| `max_parallel_projects` | No | `4` | How many at once |
| `show_project_progress` | No | `true` | One progress bar per project in a terminal. Set `false` for plain log output |
| `migration_trace_file` | No | `migration_trace.jsonl` | Step-by-step trace for diagnosis. `false` or `""` turns it off |
| `migration_trace_full_payloads` | No | `false` | Include full request payloads in the trace. Large |

**`logging`**

| Key | Required | Default | Purpose |
|---|---|---|---|
| `level` | No | `info` | `error`, `warn`, `info`, `verbose` (adds request URLs and status codes) or `debug` (adds request details). Warnings and errors always reach the console at the default level |
| `write_to_file` | No | `true` | Also write the log, an errors-only log and the run report to `dir` |
| `dir` | No | `./logs` | Where log files go |

**`prefix`** (optional, default `""`): a label for this migration, used in file names such as `logs/acme_workspace_<time>.log` and `stats/acme_stats.json`.

**Tokens from the environment.** `QASE_SOURCE_API_TOKEN`, `QASE_TARGET_API_TOKEN`, `QASE_SOURCE_SCIM_TOKEN` and `QASE_TARGET_SCIM_TOKEN` take precedence over the file, so `config.json` can hold no secrets at all.

**Upgrading from before 1.0.0?** `options.only_projects`, `options.skip_projects`, `options.preserve_ids` and `users.inactive` are still read, with a warning naming the new key. See the [changelog](CHANGELOG.md).

## 7. Validate

```bash
python preflight.py
```

or `python preflight.py path/to/config.json`. Preflight writes nothing. It checks the config, both tokens, that every project in `projects.import` exists in the source, that `users.default` is a real target user, that the SCIM tokens work when needed, and whether any target project already holds data.

A green run looks like this:

```
- Config -
  ✅ Config file config.json: parses OK
  ✅ source.api_token
  ✅ target.api_token

- Source workspace -
  ✅ Source auth (GET /v1/project): 20 project(s)
  ✅ projects.import: 1 project(s): ['TTE']

- Target workspace -
  ✅ Target auth (GET /v1/project): 0 project(s)
  ✅ users.default: qa.lead@company.com is user id 1

✅ Preflight passed, ready to run: python start.py
```

It exits with `0` when everything passes and `1` otherwise. Fix every ❌ before running.

To see exactly what a run would create, without writing anything:

```bash
python start.py --dry-run
```

```
============================================================
DRY RUN: WHAT A REAL RUN WOULD DO
============================================================
custom_fields                 : 8/8 would be created
projects                      : 1/1 would be created
suites                        : 29/29 would be created
environments                  : 1/1 would be created
cases                         : 76/76 would be created
runs                          : 45/45 would be created
results                       : 1230/1230 would be created
```

`QASE_DRY_RUN=1` does the same.

## 8. Run

```bash
python start.py
```

or `python start.py path/to/config.json`. Options: `--dry-run`, `--resume`, `--skip-attachments`.

The run exits with `0` when everything came across and `1` when anything failed, so a scheduler can tell them apart.

**How long it takes.** Attachments dominate, because each file is downloaded from the source and uploaded to the target. Measured on Qase Cloud:

| Volume | Duration |
|---|---|
| 1 project, 21 cases, no runs | about 20 seconds |
| 1 project, 76 cases, 45 runs, 1,230 results, 2,622 attachments | about 10 minutes, half of it attachments |
| Resuming that same project when it is already complete | about 1 minute |

Qase limits API requests per token. A large workspace takes hours, and the run slows down on its own when it is rate limited. That is expected and not an error.

## 9. What good output looks like

Progress, per step and per project:

```
QASE WORKSPACE MIGRATION v1.0.0
Source: qase.io
Target: company.qase.io
users.default: qa.lead@company.com is user id 1 (used for authors with no match in the target)
STEP 1: Migrating Projects
Found 1 project(s) to migrate
STEP 2: Migrating Users (Workspace Level)
STEP 3: Migrating Custom Fields (Workspace Level)
STEP 4: Migrating Shared Parameters (Workspace Level)
STEP 5: Migrating Attachments (Workspace Level)
Migrating project: TTE -> TTE
```

In a terminal, each project shows a progress bar for cases, runs and results. At the end, the counts, source against target:

```
============================================================
MIGRATION SUMMARY
============================================================
projects                      : 1/1 migrated
users                         : 8/8 migrated
custom_fields                 : 8/8 migrated
shared_parameters             : 3/3 migrated
attachments                   : 2622/2622 migrated
environments                  : 1/1 migrated
suites                        : 29/29 migrated
cases                         : 76/76 migrated
runs                          : 45/45 migrated
results                       : 1230/1230 migrated

Total Errors: 0
============================================================
```

Then the **migration report**: every warning and error from the run, grouped by project, each naming the step and the reason. A clean run says so:

```
------ Migration report: no skipped or degraded items ------
```

A run with problems lists them:

```
------ Migration report: 2 skipped/degraded/failed item(s) ------

  [TTE] 1 item(s)
    ! [custom_fields] Custom field 'Priority' already exists in the target workspace (id 12) but differs by type. It was left unmapped ...

  [workspace] 1 item(s)
    x [scim_client] Failed to get users: 401 - ... "Bearer token is invalid"
```

`x` is an error, `!` a warning. Warnings scrolling past during the run mean something was skipped or defaulted, not that the run failed. The report is the list to check.

Last, the files the run wrote:

```
============================================================
FILES FOR THIS RUN
============================================================
  report      : logs/report_20260925_134400.txt   <-- send this one first
  full log    : logs/workspace_20260925_134400.log
  errors only : logs/errors_20260925_134400.log
  statistics  : stats/stats.json
  statistics  : stats/stats.xlsx
============================================================
```

- `logs/report_<time>.txt`: settings (never tokens), counts, a **SHORTFALLS** section for any entity that did not fully migrate, and every warning and error.
- `logs/<prefix>_workspace_<time>.log`: the full log. Its first line is the version.
- `logs/errors_<time>.log`: warnings and errors only.
- `stats/<prefix>_stats.json` and `.xlsx`: the counts and the full issue list.
- `mappings.json`: source to target ids, used by `--resume`.

The logs, statistics, mappings and trace contain project, case and user data from both workspaces. Treat them as customer data and delete them when the migration is signed off.

## 10. Re-run and resume behaviour

**A plain second run does not duplicate.** A target project that already holds data is skipped with an error, and preflight flags it beforehand. To write into it anyway, set `migration.allow_existing_target: true`, and expect duplicates: below project level, a plain run creates everything again.

**Resume continues a run without duplicating.** If a run was interrupted, or stopped with errors:

```bash
python start.py --resume
```

It reads `mappings.json` and reuses everything a previous run recorded there: suites, cases, milestones, configurations, environments, shared steps, plans, runs, results, defects, custom fields, shared parameters and attachments. Only what is missing is created, and the summary says how much was reused:

```
cases                         : 76/76 migrated (76 already migrated by a previous run)
runs                          : 45/45 migrated (45 already migrated by a previous run)
```

What to know about resume:

- **Keep the same `mappings.json`.** It is how the migration knows what already exists. Without it, resume has nothing to reuse.
- **Results are resumed per run.** A run whose results were fully sent is skipped. If the source could not be read for a run, that run is reported and retried next time. If the migration is killed halfway through sending one run's results, that run's results can be sent twice.
- **Attachments are recorded when their step ends.** An interrupted attachment step starts again, but files already in the target are recognised and not uploaded twice.
- **Parallel runs record per project.** With `parallel_project_migration`, a project's progress is written to `mappings.json` when that project finishes, or when the run is stopped with Ctrl+C or fails. A process killed outright loses the projects still in progress; set `parallel_project_migration: false` for step-by-step saves.
- Deleting a migrated entity in the target and resuming does not recreate it, because the mappings still point at it. Delete the project in the target and run again without `--resume` instead.

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Config file not found` | No `config.json` in the current directory | `cp config.example.json config.json`, or pass the path: `python start.py path/to/config.json` |
| `401` from the source or target | Wrong or revoked API token | Create a new token (section 4). Preflight checks both |
| `projects.import lists [...] which do not exist` | Typo, or a project the token's user cannot see | Use the codes preflight lists as available |
| `Target project X already contains data, so it was skipped` | A previous run already migrated it, or the project existed | `--resume` if this migration created it; otherwise delete it in the target, or set `migration.allow_existing_target: true` |
| `users.default: no user with email ... exists` | The email is not a user in the **target** | Use a target user's email, or a numeric id |
| `Failed to get users: 401 ... Bearer token is invalid` | SCIM token wrong, expired, or for the other workspace | Generate a new SCIM token in that workspace. Users are then matched through the API instead, which works for existing users but cannot create them |
| Many more cases in the target than in the source | Unlinked results created cases in the target, see [limitations](#3-known-limitations) | Expected for results of deleted cases. If you do not want them as repository cases, check the target project's settings for creating cases from automated results before migrating |
| `Custom field ... already exists with a different type` | A field left by an earlier run, or an unrelated field with the same name | Delete it in the target and run again |
| `N of 20 cases were not created` | The API rejected one case, and with it the batch | The message names the source case ids. Check those cases in the source |
| The run is slow or pauses | Qase rate limiting, or many attachments | Expected. The run backs off and continues. Use `--skip-attachments` for a quick trial |
| The run waits at `Type yes (exactly)` | `users.create` is on and there are users to create | Type `yes`, or set `users.skip_creation_confirm: true` for an unattended run |

## 12. Getting help

Email **migrations@qase.io**.

Include:

- The version, which is the first line of the log file and appears in the run report
- `logs/report_<time>.txt` from the run
- Your `config.json` **with every token removed**
- What you expected to see in the target, and what you saw

The logs contain project, case and user data, so send them only over a channel you are comfortable with. Delete `logs/`, `stats/`, `mappings.json`, any trace file and `config.json` once the migration is signed off.

For a suspected security issue, do not email this address. See [SECURITY.md](SECURITY.md).

Every release is listed in [CHANGELOG.md](CHANGELOG.md).
