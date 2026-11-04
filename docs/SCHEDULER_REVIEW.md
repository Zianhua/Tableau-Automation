# Scheduler review (register_tasks.ps1, RUNBOOK section 15, SCHEDULER_HANDOFF.md)

Reviewer: fresh review, 2026-10-23. Only done: Parser::ParseFile (0 errors), the script with `-WhatIf` (default, `-Live -T11ExtraArgs`, `-Unregister -WhatIf`) on Windows PowerShell 5.1.20348, and building (not registering) a trigger object in memory. No task registered, no t* script run, `.env` not read.

## Verdict
Safe by default: the default run cannot add `--execute` or send mail. One real defect (H1) will kill the last runs of each day mid-flight, including a possible mid-send t6. Fix H1 before the first registration, even in dry-run.

## Findings

### H1 (High) Repetition stops running instances at the end of the window
- Location: register_tasks.ps1 lines 122-126 (`$trigger.Repetition = $rep.Repetition`).
- Evidence: building the trigger exactly as the script does gives `Interval PT15M, Duration PT8H1M, StopAtDurationEnd : True`.
- Scenario: the window ends 8h01m after each task's own start. t11 ends 16:31, t4 (starts 08:32) ends 16:33, t6 (starts 08:34) ends 16:35. Task Scheduler then terminates any instance still running. The last slots are 16:30 (t11), 16:32 (t4), 16:34 (t6); the t6 slot is killed after one minute, mid-export or mid-send. Any t6 or t4 run that started earlier and is still going at 16:35 is also killed (TerminateProcess, no cleanup), which defeats the 55-min limit and the "budget 40 min" design. A killed t6 after a claim and before the mail leaves a claimed-but-unsent slot (and, if the mail went out but the log write did not, a possible re-send risk; check how t6 treats stale claims).
- Fix: after copying Repetition add `$trigger.Repetition.StopAtDurationEnd = $false`. Then verify in the task XML/GUI that "Stop all running tasks at end of repetition duration" is unticked. Also decide whether the 16:32 / 16:34 slots are intended (docs say "until 16:30"); if t4/t6 must not start after 16:30, use 7h59m... duration per task, or accept and document the real last-slot times.

### M1 (Medium) `-Live` does not enforce the t11 allow-list / owner fence at registration
- Location: lines 81, 101-102; RUNBOOK 307; handoff note.
- Scenario: `register_tasks.ps1 -ServiceAccount X -Live` (forgotten `-T11ExtraArgs`) registers t11 with `--execute` and no allow-list, plus t4/t6/daily_summary `--execute`. t11 is fail-closed on its own (empty allow-list takes over nothing; it also reads `TAKEOVER_WORKBOOK_IDS` from `.env`; workbook names are refused for `--execute`; Gmail test mode needs `--owner-user-id` or `TAKEOVER_OWNER_USER_IDS`, else fatal each run), so no mass pause results. But the fence is not "required" by the registration script as the project scripts demand; an allow-list sitting in `.env` can be broader than the operator thinks, and a fatal t11 every 15 min means 32 alert-attempts a day. The mail side is still gated by `MAIL_TRANSPORT`, `SEND_ALLOWED_RECIPIENTS`, and t6 only sending taken-over, paused subscriptions.
- Fix: in `-Live`, throw unless `$T11ExtraArgs` matches `--workbooks\s+<guid>` or `--subscription-ids` (or an explicit `-AllowEnvAllowList` switch); warn/throw if the Gmail-test indicator is unknown and no `--owner-user-id` given; print a final "LIVE: t11 allow-list = ..." line; require a typed confirmation (`$PSCmdlet.ShouldContinue`) for `-Live`. Do the validation before any Register call.

### M2 (Medium) Re-running silently re-enables tasks an operator disabled; and silently flips mode
- Location: `Register-ScheduledTask ... -Force` (lines 137, 140).
- Scenario: the on-call disables the tasks as the kill step (RUNBOOK 15 recommends `Disable-ScheduledTask`), someone later re-runs the register script to fix an argument: all five come back enabled. A re-run without `-Live` silently downgrades a live host to dry-run (fail-safe but the dead-man check then reports "did not run"; nothing warns).
- Fix: before replacing, read the existing task: keep `State -eq 'Disabled'` (call `Disable-ScheduledTask` afterwards, or add a `-Enable` switch) and print "replacing existing task (mode X -> Y)". Prefer the file `out\KILL` in the runbook as the primary kill (already honoured, see below).

### M3 (Medium) Overlap / lock behaviour is safe but starves t11/t4 while t6 runs long
- Location: offsets 2 min, limits 10/30/55, IgnoreNew.
- Analysis: overlap of the same script is prevented (IgnoreNew; 15 min < limits means the next slot is dropped, not queued, and Task Scheduler does not queue a second instance). Cross-script, the shared PAT lock (applock) is the real guard, so a 2-minute offset is not a correctness mechanism: if t11 takes more than 2 minutes, t4 and t6 skip with `pat_busy` and exit 0, which preserves "pause before send" (t6 refuses unpaused subscriptions). Cost: while a long t6 run (up to 40 min budget, 55 limit) holds the lock, the next 2-3 t11 and t4 slots are skipped as pat_busy, so re-pause and refresh are delayed; after 4 skips an alert is mailed, which a normal long send day will trigger. Also with the 55 min limit, a t6 started 08:34 can hold slots until 09:29 and the next real start is 09:34.
- Fix: not blocking. Document that long t6 days will produce `pat_busy` alerts, or raise `PAT_BUSY_ALERT_AFTER`; consider t6 releasing the PAT lock after the export phase if that is feasible.

### M4 (Medium) `.env`, python and account problems are warnings or not checked (fail-open-ish at registration, fail-closed at run)
- Location: lines 106-108, 61-68.
- Scenario: missing `.env` is only `Write-Warning`; only `t6_send.py` existence is checked (not the other four scripts or the `out` folder); the service account's read access to `.env`/python/`out` is not tested; the python fallback picks the first `python` on PATH of the *registering* user (which may be a per-user install the service account cannot run, as here: `...\AppData\Local\Programs\Python\Python312`). Runtime outcome is a failing task (no mail, no send), not a mass-send, and the scripts' own kill switch is read from `<RepoRoot>\out\KILL` by `ops.ROOT`, which equals RepoRoot because the tasks run the scripts from RepoRoot, so KILL is honoured. But a task that cannot start the interpreter never writes a log, marker or alert; only the dead-man check sees it.
- Fix: throw (not warn) if `.env` is missing when not `-WhatIf`; check all five script files and create `out\` if missing; refuse a Python path under a `\Users\` profile when the account is not the current user (or at least warn); optionally run `python -c "import tableauserverclient, pyodbc, dotenv"` is not worth it, but `python --version` is.

### L1 (Low) Time zone assumption
- The tasks use host local time (here Singapore, no DST; StartBoundary is emitted with a `Z`, i.e. UTC-anchored). The scripts reason in Singapore time (`sg_now`). A host not in SGT shifts every task relative to the 08:30-16:30 business window and the t2-before-cycle ordering. Fix: assert `[TimeZoneInfo]::Local.Id -eq 'Singapore Standard Time'` or warn.

### L2 (Low) Minor script points
- `$password` is a plain string (Register-ScheduledTask requires it); it is never written to disk, logged, or placed in the description/arguments, and is nulled at the end. Acceptable. It is prompted even if a later `ShouldProcess` answer is No; harmless. Getting it before any validation of the task definitions (line 112) means a typo in the account is only found at the first Register; consider validating `Get-ScheduledTask`-free with `[Security.Principal.NTAccount]::Translate`.
- `-Unregister` before argument validation is fine; it touches only the five named tasks in `\TableauAutomation\` and leaves any unrelated task and the folder alone (verified: `-Unregister -WhatIf` lists only the five names and changes nothing). Register with `-Force` replaces only same-name tasks.
- `$trigger` is a Daily trigger whose `StartBoundary` is today's date; fine when registered after 08:30 (no catch-up; no `-StartWhenAvailable`, so a missed 07:50 t2 is not made up. State this in the runbook; the 26 h `MAPPING_MAX_AGE_H` covers one miss).
- `T11ExtraArgs` is free text appended to the command line; it is not a shell, but nothing stops `--restore ...` or `--force-notready`. Whitelist the flags.
- `RunLevel Limited` with `-LogonType Password` for the gMSA is the documented form; untested (as the handoff says).
- t2 `--dry-run` also writes nothing; daily_summary without `--execute` is dry-run by script default (verified in `daily_summary.py`: `--execute` sends). OK.
- Docs: RUNBOOK 15 and the script header say "until 16:30" for all; with H1 fixed the real last starts are 16:30/16:32/16:34. State it.

## Checked and fine
- (a) Default run: no `--execute` anywhere; t2/t11/t4/t6 get `--dry-run`, daily_summary gets no flag. Verified from the `-WhatIf` output. Real mail also needs `MAIL_TRANSPORT=smtp`, `SEND_ALLOWED_RECIPIENTS`.
- `-Live` t2 drops `--dry-run` (t2 has no `--execute`), others get `--execute`; `-T11ExtraArgs` is applied only to t11 and only in `-Live`.
- (b) Kill switch: the scripts check `out\KILL` / `TABLEAU_AUTOMATION_DISABLED` first (`ops.check_kill_switch`, plus `killed_midrun` in t4/t6), file is under RepoRoot. Works in both modes.
- (c) No password in file, arguments, description, or output.
- (d) Repetition: 15 min for 8h01m from the task's start does include the 16:30 slot for t11 (08:30 + 8h = 16:30 < 16:31); the "does it really fire" question is fine, the stop-at-end flag (H1) is the problem.
- (e) No function named `H`; parses without errors on 5.1; no `&&`, `??`, ternary or PS7-only syntax; `$PSScriptRoot` default works when invoked as `& script.ps1` (a `powershell -File` call from this review environment hit an empty `$PSScriptRoot`; dot-sourced or `-Command` use could too, so pass `-RepoRoot` explicitly in the runbook examples).
