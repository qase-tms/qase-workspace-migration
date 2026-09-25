# Security Policy

## Reporting a vulnerability

**Do not open a public issue or discussion, and do not email a vulnerability report to the general migrations address.**

Report privately through GitHub's private vulnerability reporting on this repository: **Security > Report a vulnerability**. This creates a private advisory visible only to the maintainers.

Please include the affected version or commit, what an attacker could achieve, and the steps to reproduce.

We will acknowledge the report and keep you updated until it is resolved.

## What this tool handles

This script reads from one Qase workspace and writes into another. Understanding what it touches will help you judge whether something is a vulnerability.

**Credentials.** Read from `config.json` or from the environment:

| Credential | Needs | Used for |
|---|---|---|
| Source Qase API token | Read | Everything read out of the source workspace |
| Target Qase API token | Read and write | Everything written into the target workspace |
| Source SCIM token | Read, optional | Listing source users and groups, when `users.migrate` is true |
| Target SCIM token | Read and write, optional | Matching users, and creating users and groups when enabled |

They are held in memory for the duration of the run. **No credential value is written to a log file, the run report or the statistics, at any level.**

**Each token is only sent to its own workspace.** Source tokens go only to the source host and target tokens only to the target host.

**The source workspace is read-only.** Every call against the source is a read. The migration has no code path that writes, updates or deletes anything there, so a failed or repeated run cannot damage your source data. A dry run (`--dry-run`) is read-only against the target too.

**Customer data on disk.** These hold it after a run:

- `logs/`: run logs and the run report, which can contain project, case and user data and API error bodies
- `stats/`: per-run counts and the list of skipped or failed items
- `mappings.json`: source to target id mappings, used for `--resume`
- `migration_trace.jsonl`: a step-by-step trace, when enabled, which can include payloads

All are gitignored. None is needed once a migration is signed off.

## Handling your own credentials

- **Prefer the environment over the file.** `QASE_SOURCE_API_TOKEN`, `QASE_TARGET_API_TOKEN`, `QASE_SOURCE_SCIM_TOKEN` and `QASE_TARGET_SCIM_TOKEN` override `config.json` and take precedence, so a config file you paste into a support ticket carries no secrets.
- Use tokens belonging to a user who can see only the projects being migrated, where possible.
- Revoke every token used for a migration once it is finished.
- Never commit `config.json`. It is gitignored, but a file added under a different name will not be.
- **Delete `config.json`, `logs/`, `stats/`, `mappings.json` and any trace file when the migration is signed off.**

## If a credential is exposed

Revoke it first, then clean up. A token removed from a file but not revoked is still live, and a commit deleted from a public repository stays readable by hash.
