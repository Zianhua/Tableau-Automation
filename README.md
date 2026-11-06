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
- [docs/RUNBOOK.md](docs/RUNBOOK.md): how to operate it (schedules, alerts, kill switch, restore)
- [docs/LIMITATIONS.md](docs/LIMITATIONS.md): known limits and assumptions
- [docs/DB_CHANGES.md](docs/DB_CHANGES.md): every database change, in the order to apply it, with permissions and the dev-only test changes (for the DBA)
- [docs/DEPLOYMENT_PLAN.md](docs/DEPLOYMENT_PLAN.md): step-by-step deployment to the server, staged rollout, monitoring, what is not tested yet
- [docs/DEPLOYMENT_UAT.md](docs/DEPLOYMENT_UAT.md): the UAT-only walk-through on the host (<UAT_DWH_SERVER>, tableau-uat)
- [docs/PROD_PREFLIGHT.md](docs/PROD_PREFLIGHT.md): read-only production checks before go-live

Suggested reading order for deployment: PROD_PREFLIGHT, DB_CHANGES, DEPLOYMENT_PLAN, then RUNBOOK.

## Local configuration

Environment-specific values are not hard-coded. The Python and PowerShell scripts read them from `.env` (copy `.env.example`); a guard whose key is unset refuses to run (fail closed). Keys:

| Key | Used for |
|---|---|
| `DEV_DWH_SERVER`, `DEV_DWH_DATABASE` | dev DWH guard of the mapping sync and refresh scripts, and the concurrency test; they refuse any other server or database |
| `UAT_TABLEAU_HOST` | `SEND_ALLOW_ALL_UAT` only takes effect when the host of `TABLEAU_SERVER_URL` equals this exactly |
| `OWNER_EMAIL` | default alert recipient when `ALERT_TO` is unset |
| `DEFAULT_SENDER_EMAIL` | display From address when `SEND_FROM` is unset |

The SQL files (`scripts/sql/*.sql`) cannot read `.env`. Their server guards, and the docs, use placeholders such as `<DEV_DWH_SERVER>`, `<UAT_DWH_SERVER>`, `tableau-uat.example.com` and `you@example.com`. Replace them with the values for your environment before running a script (the SQL guards stop the script on any other server).
