<#
.SYNOPSIS
  Registers (or removes) the Windows Task Scheduler tasks for the Tableau subscription automation.

.DESCRIPTION
  Running it CREATES the tasks (existing ones are replaced; a task that was Disabled stays Disabled).
  Use -WhatIf to preview without changing anything, -Unregister to remove them.
  Tasks live in the folder \TableauAutomation:

    t2_mapping_sync  daily 07:50 (StartWhenAvailable)  limit 30 min
    extract_takeover daily 08:20                       limit 10 min   (keeps taken-over Tableau extract refresh tasks deleted; see -ExtractExtraArgs)
    t11_pause        08:30, then every 15 min          limit 10 min   last start 16:30
    t4_refresh       08:32, then every 15 min          limit 40 min   last start 16:32
    t6_send          08:34, then every 15 min          limit 55 min   last start 16:34 (t6 own claim budget is 25 min)
    watchdog         08:45, then every 15 min          limit 5 min    last start 17:15 (mails when a run is stuck or a script stopped)
    daily_summary    daily 17:30                       limit 10 min

  All tasks use MultipleInstances = IgnoreNew. The repetition does NOT stop a running instance at the end
  of the window (StopAtDurationEnd = false): a run started at 16:34 may finish; only the time limit stops it.

  DEFAULT IS DRY-RUN ONLY: the registered command lines carry no --execute (t2 carries --dry-run,
  because t2 writes by default). -Live registers the real-run command lines (t2 without --dry-run,
  the others with --execute). -Live REQUIRES a t11 allow-list in -T11ExtraArgs and asks for confirmation.
  extract_takeover stays --dry-run even with -Live unless -ExtractExtraArgs is given (deleting Tableau extract refresh tasks is its own go-ahead).
  Real mail still needs MAIL_TRANSPORT=smtp in .env.

  No password is stored in this file. For a normal account the script asks for the password with
  Get-Credential when the tasks are created (Task Scheduler stores it). For a group managed service account
  (name ends with $) no password is needed.

.PARAMETER ServiceAccount
  Account the tasks run as, e.g. DOMAIN\svc_tableau or DOMAIN\gmsa_tableau$. Required (not needed with -WhatIf or -Unregister).
.PARAMETER RepoRoot
  Install folder = working directory of every task. The scripts read RepoRoot\.env and write RepoRoot\out\ (logs,
  lastrun markers, KILL file); neither location can be moved. Default: two levels above this script (the repo's
  current location). Pass it explicitly.
.PARAMETER PythonExe
  Full path of python.exe. Default order: <RepoRoot>\.venv\Scripts\python.exe, then a machine-wide python on PATH,
  then (with a warning) a per-user install. An explicit -PythonExe is always used as given.
.PARAMETER Live
  Register real-run command lines (adds --execute / drops --dry-run). Default is dry-run only.
.PARAMETER T11ExtraArgs
  Required with -Live. Only these are accepted: --workbooks <guid[,guid]>, --subscription-ids <id[,id]>,
  --owner-user-id <id> (REQUIRED with -Live, repeat per owner). At least one of --workbooks / --subscription-ids is needed.
  Example: '--workbooks b98b96f8-a0cb-42ed-afde-5ec1f8a46f9b --owner-user-id <user id>'
.PARAMETER ExtractExtraArgs
  Optional, only used with -Live. Turns the extract_takeover task into a real run: --datasource-ids <luid[,luid]> (required) and
  --owner-user-id <id> (required, repeat per owner). Without it the task is registered with --dry-run even under -Live.
  Example: '--datasource-ids 8bb1fcad-ed2a-4c14-8b9b-7b70f4f7be1f --owner-user-id <user id>'
.PARAMETER Force
  Skips only the -Live confirmation prompt. It never skips the allow-list requirement.
.PARAMETER Unregister
  Remove all tasks in \TableauAutomation (honours -WhatIf).

.EXAMPLE
  .\register_tasks.ps1 -RepoRoot D:\Apps\TableauAutomation -ServiceAccount 'DOMAIN\svc_tableau' -WhatIf
.EXAMPLE
  .\register_tasks.ps1 -RepoRoot D:\Apps\TableauAutomation -ServiceAccount 'DOMAIN\svc_tableau'
.EXAMPLE
  .\register_tasks.ps1 -RepoRoot D:\Apps\TableauAutomation -ServiceAccount 'DOMAIN\svc_tableau' -Live -T11ExtraArgs '--workbooks <guid>'
.EXAMPLE
  .\register_tasks.ps1 -Unregister
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$ServiceAccount,
    [string]$RepoRoot = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),
    [string]$PythonExe,
    [switch]$Live,
    [string]$T11ExtraArgs = '',
    [string]$ExtractExtraArgs = '',
    [switch]$Force,
    [switch]$Unregister
)

$ErrorActionPreference = 'Stop'
$TaskPath = '\TableauAutomation\'
$mode = if ($Live) { 'LIVE' } else { 'DRY-RUN' }

$definitions = @(
    @{ Name = 't2_mapping_sync'; Script = 'scripts\t2_mapping_sync.py'; At = '07:50'; Repeat = $false; LimitMin = 30; DryArgs = '--dry-run'; LiveArgs = '' },
    @{ Name = 'extract_takeover'; Script = 'scripts\extract_takeover.py'; At = '08:20'; Repeat = $false; LimitMin = 10; DryArgs = '--dry-run'; LiveArgs = '--execute' },
    @{ Name = 't11_pause';       Script = 'scripts\t11_pause.py';       At = '08:30'; Repeat = $true;  LimitMin = 10; DryArgs = '--dry-run'; LiveArgs = '--execute' },
    @{ Name = 't4_refresh';      Script = 'scripts\t4_refresh.py';      At = '08:32'; Repeat = $true;  LimitMin = 40; DryArgs = '--dry-run'; LiveArgs = '--execute' },
    @{ Name = 't6_send';         Script = 'scripts\t6_send.py';         At = '08:34'; Repeat = $true;  LimitMin = 55; DryArgs = '--dry-run'; LiveArgs = '--execute' },
    @{ Name = 'watchdog';        Script = 'scripts\watchdog.py';        At = '08:45'; Repeat = $true;  LimitMin = 5;  DryArgs = '';          LiveArgs = '--execute'; DurationMin = 511 },
    @{ Name = 'daily_summary';   Script = 'scripts\daily_summary.py';   At = '17:30'; Repeat = $false; LimitMin = 10; DryArgs = '';          LiveArgs = '--execute' }
)

# ---- -Unregister --------------------------------------------------------------------------------
if ($Unregister) {
    foreach ($d in $definitions) {
        $existing = Get-ScheduledTask -TaskPath $TaskPath -TaskName $d.Name -ErrorAction SilentlyContinue
        if (-not $existing) { Write-Host "not registered: $TaskPath$($d.Name)"; continue }
        if ($PSCmdlet.ShouldProcess("$TaskPath$($d.Name)", 'Unregister-ScheduledTask')) {
            Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $d.Name -Confirm:$false
            Write-Host "unregistered $TaskPath$($d.Name)"
        }
    }
    return
}

# ---- t11 argument whitelist ---------------------------------------------------------------------
function Test-T11ExtraArgs([string]$Text) {
    $tokens = @($Text -split '\s+' | Where-Object { $_ })
    $hasAllowList = $false
    $i = 0
    while ($i -lt $tokens.Count) {
        $flag = $tokens[$i]
        if ($i + 1 -ge $tokens.Count -or $tokens[$i + 1].StartsWith('--')) { throw "-T11ExtraArgs: '$flag' needs a value." }
        $value = $tokens[$i + 1]
        switch ($flag) {
            '--workbooks' {
                if ($value -notmatch '^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}(,[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})*$') {
                    throw "-T11ExtraArgs: --workbooks needs workbook GUID(s), comma separated, got '$value'."
                }
                $hasAllowList = $true
            }
            '--subscription-ids' {
                if ($value -notmatch '^[A-Za-z0-9-]+(,[A-Za-z0-9-]+)*$') { throw "-T11ExtraArgs: bad --subscription-ids '$value'." }
                $hasAllowList = $true
            }
            '--owner-user-id' {
                if ($value -notmatch '^[A-Za-z0-9-]+$') { throw "-T11ExtraArgs: bad --owner-user-id '$value'." }
            }
            default { throw "-T11ExtraArgs: '$flag' is not allowed here (only --workbooks, --subscription-ids, --owner-user-id)." }
        }
        $i += 2
    }
    return $hasAllowList
}

# ---- extract_takeover argument whitelist --------------------------------------------------------
function Test-ExtractExtraArgs([string]$Text) {
    $tokens = @($Text -split '\s+' | Where-Object { $_ })
    $hasIds = $false; $hasOwner = $false
    $i = 0
    while ($i -lt $tokens.Count) {
        $flag = $tokens[$i]
        if ($i + 1 -ge $tokens.Count -or $tokens[$i + 1].StartsWith('--')) { throw "-ExtractExtraArgs: '$flag' needs a value." }
        $value = $tokens[$i + 1]
        switch ($flag) {
            '--datasource-ids' {
                if ($value -notmatch '^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}(,[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})*$') {
                    throw "-ExtractExtraArgs: --datasource-ids needs data source GUID(s), comma separated, got '$value'."
                }
                $hasIds = $true
            }
            '--owner-user-id' {
                if ($value -notmatch '^[A-Za-z0-9-]+$') { throw "-ExtractExtraArgs: bad --owner-user-id '$value'." }
                $hasOwner = $true
            }
            default { throw "-ExtractExtraArgs: '$flag' is not allowed here (only --datasource-ids, --owner-user-id)." }
        }
        $i += 2
    }
    if (-not ($hasIds -and $hasOwner)) { throw '-ExtractExtraArgs needs both --datasource-ids and --owner-user-id (the owner fence is not optional).' }
}
if ($ExtractExtraArgs) { Test-ExtractExtraArgs $ExtractExtraArgs }

$allowListOk = $false
if ($T11ExtraArgs) { $allowListOk = Test-T11ExtraArgs $T11ExtraArgs }
if ($Live -and -not $allowListOk) {
    throw '-Live needs a t11 allow-list: -T11ExtraArgs ''--workbooks <workbook guid>'' (or --subscription-ids <ids>). Not optional, -Force does not skip it.'
}

if ($Live -and $T11ExtraArgs -notmatch '(^|\s)--owner-user-id\s') {
    throw '-Live needs the owner fence too: add --owner-user-id <Tableau user id> to -T11ExtraArgs (repeat the flag per owner). t11 refuses --execute with an allow-list and no owner fence.'
}

function Get-TaskArgumentLine($Definition) {
    $argText = if ($Live) { $Definition.LiveArgs } else { $Definition.DryArgs }
    if ($Live -and $Definition.Name -eq 't11_pause' -and $T11ExtraArgs) { $argText = "$argText $T11ExtraArgs" }
    if ($Definition.Name -eq 'extract_takeover') {
        $argText = if ($Live -and $ExtractExtraArgs) { "--execute $ExtractExtraArgs" } else { '--dry-run' }
    }
    return ('-u "{0}" {1}' -f (Join-Path $RepoRoot $Definition.Script), $argText).Trim()
}

# ---- python: explicit > repo venv > machine-wide > per-user (warn) ------------------------------
function Test-UserProfilePath([string]$Path) { return ($Path -match '\\Users\\' -or $Path -match '\\AppData\\') }

if (-not $PythonExe) {
    $venv = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venv) { $PythonExe = $venv }
    else {
        $all = @(Get-Command python.exe -All -ErrorAction SilentlyContinue | ForEach-Object { $_.Source })
        $machine = $all | Where-Object { -not (Test-UserProfilePath $_) } | Select-Object -First 1
        if ($machine) { $PythonExe = $machine }
        elseif ($all.Count -gt 0) { $PythonExe = $all[0] }
    }
}
if ($PythonExe -and (Test-UserProfilePath $PythonExe)) {
    Write-Warning "Python '$PythonExe' is inside a user profile. The service account probably cannot run it. Create <RepoRoot>\.venv from a machine-wide python or pass -PythonExe."
}
if ([TimeZoneInfo]::Local.Id -ne 'Singapore Standard Time') {
    Write-Warning "Host time zone is '$([TimeZoneInfo]::Local.Id)', not Singapore Standard Time. The tasks use host local time, the scripts reason in Singapore time."
}

# ---- plan ---------------------------------------------------------------------------------------
Write-Host "Mode: $mode   Repo: $RepoRoot   Python: $PythonExe   Folder: $TaskPath"
foreach ($d in $definitions) {
    $durMin = if ($d.DurationMin) { $d.DurationMin } else { 481 }
    $last = ([datetime]::Today.Add([timespan]::Parse($d.At))).AddMinutes($durMin - 1).ToString('HH:mm')
    $when = if ($d.Repeat) { "$($d.At) every 15 min, last start $last" } else { "daily $($d.At)" }
    Write-Host ("  {0,-16} {1,-38} limit {2,2} min  {3} {4}" -f $d.Name, $when, $d.LimitMin, $PythonExe, (Get-TaskArgumentLine $d))
}
if ($Live) { Write-Host "LIVE: t11 allow-list = $T11ExtraArgs" }

if (-not $ServiceAccount -and -not $WhatIfPreference) { throw '-ServiceAccount is required (DOMAIN\account, or DOMAIN\gmsa$).' }
if (-not $ServiceAccount) { $ServiceAccount = '<ServiceAccount>' }

# ---- preflight: fail registration, do not warn --------------------------------------------------
$problems = @()
if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot '.env'))) { $problems += "missing $(Join-Path $RepoRoot '.env')" }
foreach ($d in $definitions) {
    $p = Join-Path $RepoRoot $d.Script
    if (-not (Test-Path -LiteralPath $p)) { $problems += "missing $p" }
}
if (-not $PythonExe -or -not (Test-Path -LiteralPath $PythonExe)) { $problems += "python.exe not found: '$PythonExe' (pass -PythonExe)" }
else {
    try { $v = & $PythonExe --version 2>&1; if ($LASTEXITCODE -ne 0) { $problems += "python cannot be run: $v" } }
    catch { $problems += "python cannot be run: $($_.Exception.Message)" }
}
if ($problems.Count -gt 0) {
    if ($WhatIfPreference) { $problems | ForEach-Object { Write-Warning "preflight (would fail registration): $_" } }
    else { throw ("Preflight failed:`n  " + ($problems -join "`n  ")) }
}

# ---- confirmation for -Live ---------------------------------------------------------------------
if ($Live -and -not $WhatIfPreference -and -not $Force) {
    if (-not $PSCmdlet.ShouldContinue("Register LIVE tasks (--execute) as $ServiceAccount with t11 allow-list '$T11ExtraArgs'?", 'LIVE tasks')) {
        Write-Host 'Cancelled.'; return
    }
}

$isGmsa = $ServiceAccount.EndsWith('$')
$password = $null
if (-not $isGmsa -and -not $WhatIfPreference) {
    $cred = Get-Credential -UserName $ServiceAccount -Message 'Password of the service account (stored by Task Scheduler, not by this script)'
    $password = $cred.GetNetworkCredential().Password
}
if (-not $WhatIfPreference) { New-Item -ItemType Directory -Force -Path (Join-Path $RepoRoot 'out') | Out-Null }

# ---- register -----------------------------------------------------------------------------------
foreach ($d in $definitions) {
    $action = New-ScheduledTaskAction -Execute $PythonExe -Argument (Get-TaskArgumentLine $d) -WorkingDirectory $RepoRoot

    $start = [datetime]::Today.Add([timespan]::Parse($d.At))
    $trigger = New-ScheduledTaskTrigger -Daily -At $start
    if ($d.Repeat) {
        # Duration 8 h 1 min so the slot exactly 8 h after the first start is included. Real last starts: 16:30 / 16:32 / 16:34.
        $rep = New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Minutes $(if ($d.DurationMin) { $d.DurationMin } else { 481 }))
        $trigger.Repetition = $rep.Repetition
        $trigger.Repetition.StopAtDurationEnd = $false   # never kill a run that is still going when the window closes
    }

    $settingArgs = @{
        MultipleInstances          = 'IgnoreNew'
        ExecutionTimeLimit         = (New-TimeSpan -Minutes $d.LimitMin)
        AllowStartIfOnBatteries    = $true
        DontStopIfGoingOnBatteries = $true
    }
    if ($d.Name -eq 't2_mapping_sync') { $settingArgs['StartWhenAvailable'] = $true }   # a missed 07:50 runs when the host is back
    $settings = New-ScheduledTaskSettingsSet @settingArgs
    $desc = "Tableau subscription automation, mode=$mode, Kill switch: create out\KILL (see docs/RUNBOOK.md)."
    $task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Description $desc

    # keep an operator's Disabled state; show the mode change
    $existing = Get-ScheduledTask -TaskPath $TaskPath -TaskName $d.Name -ErrorAction SilentlyContinue
    $wasDisabled = $false
    if ($existing) {
        $oldMode = if ($existing.Description -match 'mode=([A-Z-]+)') { $Matches[1] } else { 'unknown' }
        $wasDisabled = ($existing.State -eq 'Disabled')
        Write-Host "replacing existing $TaskPath$($d.Name): mode $oldMode -> $mode$(if ($wasDisabled) { ' (stays Disabled)' })"
    }

    if ($PSCmdlet.ShouldProcess("$TaskPath$($d.Name)", "Register-ScheduledTask as $ServiceAccount ($mode)")) {
        if ($isGmsa) {
            $principal = New-ScheduledTaskPrincipal -UserId $ServiceAccount -LogonType Password -RunLevel Limited
            Register-ScheduledTask -TaskPath $TaskPath -TaskName $d.Name -InputObject $task -Principal $principal -Force | Out-Null
        }
        else {
            Register-ScheduledTask -TaskPath $TaskPath -TaskName $d.Name -InputObject $task `
                -User $ServiceAccount -Password $password -RunLevel Limited -Force | Out-Null
        }
        if ($wasDisabled) { Disable-ScheduledTask -TaskPath $TaskPath -TaskName $d.Name | Out-Null }
        Write-Host "registered $TaskPath$($d.Name) ($mode)"
    }
}
$password = $null
