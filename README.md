# TableauSubscriptionAutomation

Script-driven refresh and delivery of Tableau reports that use several extracts, where native "when data refreshes" subscriptions do not work.

- [CLAUDE.md](CLAUDE.md): goal, session roles and rules
- [STATUS.md](STATUS.md): current state (as of 2026-11-03) and handoff log
- [BACKLOG.md](BACKLOG.md): tasks
- [DECISIONS.md](DECISIONS.md): decisions and open questions
- [docs/PLAN.md](docs/PLAN.md): target flow, data model, phases
- [Flow diagram (FigJam)](https://www.figma.com/board/dMD25oI1U2Kor8sqCPggIf): green already built, blue new
- [docs/NEXT_SESSION.md](docs/NEXT_SESSION.md): prompt to start the next PM session
- [docs/REVIEW_SYSTEM_OWNER_20261029.md](docs/REVIEW_SYSTEM_OWNER_20261029.md): system-owner review and its open points
- [docs/UAT_TEST_PLAN.md](docs/UAT_TEST_PLAN.md) and [docs/UAT_TEST_RESULTS_20261027.md](docs/UAT_TEST_RESULTS_20261027.md): UAT test cases and results
- [docs/FEASIBILITY_CROSSTAB_EXPORT.md](docs/FEASIBILITY_CROSSTAB_EXPORT.md): crosstab/Excel export study (parked)
- [docs/edge_cases.md](docs/edge_cases.md): known edge cases, not in scope
- [docs/RUNBOOK.md](docs/RUNBOOK.md): how to operate it (schedules, alerts, kill switch, restore, who refreshes each extract)
- [docs/LIMITATIONS.md](docs/LIMITATIONS.md): known limits and assumptions
- [docs/DB_CHANGES.md](docs/DB_CHANGES.md): every database change, in the order to apply it, with permissions and the dev-only test changes (for the DBA)
- [docs/DEPLOYMENT_PLAN.md](docs/DEPLOYMENT_PLAN.md): step-by-step deployment to the server, staged rollout, monitoring, what is not tested yet
- [docs/DEPLOYMENT_UAT.md](docs/DEPLOYMENT_UAT.md): the UAT-only walk-through on the host (<UAT_DWH_SERVER>, tableau-uat)
- [docs/DEPLOYMENT_PROD.md](docs/DEPLOYMENT_PROD.md): production install, go-live and rollback
- [scripts/sql/prod_precheck.sql](scripts/sql/prod_precheck.sql): read-only check of every object, column, grant and seed row on a warehouse
- [docs/PROD_PREFLIGHT.md](docs/PROD_PREFLIGHT.md), [docs/DB_CHANGES.md](docs/DB_CHANGES.md), [docs/DEPLOYMENT_PLAN.md](docs/DEPLOYMENT_PLAN.md): history of the design and the dev rollout (superseded for deployment by the files above)

**Deploying:** `scripts/sql/prod_precheck.sql` on the target warehouse, then [DEPLOYMENT_PROD.md](docs/DEPLOYMENT_PROD.md) (or [DEPLOYMENT_UAT.md](docs/DEPLOYMENT_UAT.md)), then [RUNBOOK.md](docs/RUNBOOK.md). The deployable set is the scripts without the `*_checks.py` test suites (which only ever run against the dev warehouse).

**Packaged program (`bin\`):** `scripts\build_exe.ps1` packages the scripts with PyInstaller into `bin\tableau_automation.exe` (one folder, the Python runtime and the pinned packages included), so the host needs no Python. `tableau_automation.exe <script name> <arguments>` runs that script unchanged; the scheduled tasks use it when `bin\` exists. Rebuild and commit `bin\` with every code change: `selfcheck` (run by the install and task scripts) refuses a `bin\` built from other code. `bin\` is not in the public copy.

## Local configuration

Environment-specific values are not hard-coded. The Python and PowerShell scripts read them from `.env` (copy `.env.example`); a guard whose key is unset refuses to run (fail closed). Keys:

| Key | Used for |
|---|---|
| `DEV_DWH_SERVER`, `DEV_DWH_DATABASE` | warehouse guard of every script (historical name): set them to the warehouse of the host (production on production); the scripts refuse any other server or database |
| `SEND_TO_ALL_HOST` | `SEND_TO_ALL_SUBSCRIBERS` only takes effect when the host of `TABLEAU_SERVER_URL` equals this exactly |
| `OWNER_EMAIL` | default alert recipient when `ALERT_TO` is unset |
| `DEFAULT_SENDER_EMAIL` | display From address when `SEND_FROM` is unset |

The SQL files (`scripts/sql/*.sql`) cannot read `.env`. Their server guards, and the docs, use placeholders such as `<DEV_DWH_SERVER>`, `<UAT_DWH_SERVER>`, `tableau-uat.example.com` and `you@example.com`. Replace them with the values for your environment before running a script (the SQL guards stop the script on any other server).
