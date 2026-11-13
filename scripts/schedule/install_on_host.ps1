<#
.SYNOPSIS
  Puts the Tableau subscription automation on a Tableau host NEXT TO the legacy extract job, without touching the legacy job.

.DESCRIPTION
  Run it from the folder you downloaded from GitLab (the folder that holds scripts\, docs\, .env.example), in an elevated
  PowerShell on the Tableau host.

    .\scripts\schedule\install_on_host.ps1 -CheckOnly
        READ-ONLY. Finds the legacy extract job through its scheduled tasks (run_extract_refresh.bat /
        tableau_extract_automation.exe / run_server_cleanup.bat), shows its folder, and checks Python, the ODBC driver, the time
        zone and whether this solution is already installed. Changes nothing.

    .\scripts\schedule\install_on_host.ps1 [-LegacyRoot <INSTALL_DIR>]
        Copies this solution into a NEW subfolder <LegacyRoot>\TableauSubscriptionAutomation (name: -TargetName).
        The legacy root is found automatically; give -LegacyRoot when the check finds none or more than one.
        Before copying it fingerprints every legacy file and exports the legacy scheduled tasks; afterwards it proves both are
        unchanged. It never writes outside the new subfolder, never copies .env, test suites or local reference material, and
        does not register scheduled tasks (that is deploy_uat.ps1, after you created .env).

    .\scripts\schedule\install_on_host.ps1 -Update
        Same, into an existing install: copies the new code over it and KEEPS .env, out\ and .venv.

  Why a separate subfolder and not the legacy folder itself: the legacy job runs from <root>\executables_tableau_extract with its
  own .env and logs to <root>\log. Mixing files there could overwrite its .env or its exe. A sibling folder shares nothing.
#>
[CmdletBinding()]
param(
    [string]$LegacyRoot,
    [string]$TargetName = 'TableauSubscriptionAutomation',
    [switch]$CheckOnly,
    [switch]$Update
)
$ErrorActionPreference = 'Stop'

function Step($t) { Write-Host "`n=== $t" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "  OK    $t" -ForegroundColor Green }
function Warn($t) { Write-Host "  WARN  $t" -ForegroundColor Yellow }
function Bad($t)  { Write-Host "  FAIL  $t" -ForegroundColor Red }

$source = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$LegacyPattern = 'run_extract_refresh|tableau_extract_automation|run_server_cleanup|executables_tableau_extract'

# ---------------------------------------------------------------- 1. source
Step '1. Source (the folder this script was started from)'
Write-Host "  $source"
foreach ($f in 'scripts\t4_refresh.py', 'scripts\t6_send.py', 'scripts\schedule\deploy_uat.ps1', '.env.example') {
    if (-not (Test-Path (Join-Path $source $f))) { throw "This does not look like the solution folder: $f is missing in $source" }
}
Ok 'solution files present'
$exe = Join-Path $source 'bin\tableau_automation.exe'
$packaged = Test-Path $exe
if ($packaged) {
    $ErrorActionPreference = 'Continue'
    $sc = & $exe selfcheck 2>&1; $scExit = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($scExit -eq 0) { Ok 'packaged program bin\tableau_automation.exe runs on this host (no Python needed)' }
    else { Bad "bin\tableau_automation.exe does not run on this host (antivirus? missing runtime?): $($sc -join ' | ')" }
} else { Warn 'no bin\tableau_automation.exe in the download: the install will need Python 3.12 and PyPI' }
$elevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole('Administrators')
if (-not $elevated) { Warn 'this PowerShell is NOT elevated: scheduled tasks of other accounts may be hidden below. Use Run as administrator.' }

# ---------------------------------------------------------------- 2. legacy job
Step '2. Legacy extract job (read-only)'
$legacyTasks = @(Get-ScheduledTask | Where-Object {
    ($_.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments) $($_.WorkingDirectory)" }) -join ' ' -match $LegacyPattern })
$roots = @()
if (-not $legacyTasks) { Warn 'no scheduled task found that runs the legacy job' }
foreach ($t in $legacyTasks) {
    $a = $t.Actions | Select-Object -First 1
    $info = $t | Get-ScheduledTaskInfo
    Write-Host ("  task  {0}{1}  state={2}  runs as={3}  last={4}  next={5}" -f $t.TaskPath, $t.TaskName, $t.State, $t.Principal.UserId, $info.LastRunTime, $info.NextRunTime)
    Write-Host ("        runs: {0} {1}" -f $a.Execute, $a.Arguments)
    $cmd = [Environment]::ExpandEnvironmentVariables(($a.Execute -replace '"', ''))
    if ($cmd -match '\.bat$|\.cmd$') {
        if (Test-Path $cmd) {
            $bat = Get-Content $cmd
            foreach ($line in $bat) { if ($line -match '^\s*set\s+"?(EXE_PATH|WORKING_DIR|LOG_DIR|LOG_FILE)=([^"]+)"?') { Write-Host "        $($Matches[1]) = $($Matches[2])" } }
            $batDir = Split-Path $cmd -Parent
            $roots += $(if ((Split-Path $batDir -Leaf) -eq 'batch_files') { Split-Path $batDir -Parent } else { $batDir })
            $exe = ($bat | Where-Object { $_ -match '^\s*set\s+"?EXE_PATH=([^"]+)"?' } | ForEach-Object { $Matches[1] } | Select-Object -First 1)
            if ($exe) { $roots += Split-Path (Split-Path $exe.Trim() -Parent) -Parent }
        } else { Warn "batch file not found: $cmd" }
    } elseif ($cmd -match '\.exe$') {
        $roots += Split-Path (Split-Path $cmd -Parent) -Parent
    }
}
if (-not $roots) {   # fallback: look for the legacy exe folder on the fixed drives, two levels deep
    foreach ($d in (Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Free -ne $null -and $_.Root -match '^[A-Z]:\\$' })) {
        Get-ChildItem $d.Root -Directory -Depth 2 -Filter 'executables_tableau_extract' -ErrorAction SilentlyContinue |
            ForEach-Object { $roots += $_.Parent.FullName }
    }
}
if ($LegacyRoot) { $roots = @($LegacyRoot) }   # given by hand: use exactly that folder
$roots = @($roots | Where-Object { $_ } | ForEach-Object { $_.TrimEnd('\') } | Sort-Object -Unique)
foreach ($r in $roots) {
    Write-Host "  legacy root candidate: $r"
    Get-ChildItem $r -Force -ErrorAction SilentlyContinue | ForEach-Object { Write-Host ("        {0,-5} {1}" -f $(if ($_.PSIsContainer) { 'dir' } else { 'file' }), $_.Name) }
    if (Test-Path (Join-Path $r '.git')) { Warn "$r is a git checkout: do not 'git pull' the whole repository there by hand, this script copies only the new folder" }
    if (Test-Path (Join-Path $r 'executables_tableau_extract\.env')) { Ok 'legacy .env is in executables_tableau_extract (it is never read or copied by this script)' }
}
if ($roots.Count -eq 0) { Warn 'no legacy folder found: give -LegacyRoot <folder> (for example <INSTALL_DIR>)' }
if ($roots.Count -gt 1) { Warn 'more than one legacy folder found: give -LegacyRoot <folder>' }

# ---------------------------------------------------------------- 3. host
Step '3. Host'
if ($packaged) { Ok 'Python and PyPI not needed (packaged program)' }
else {
    $py = Get-Command python -ErrorAction SilentlyContinue
    if ($py -and $py.Source -notmatch 'WindowsApps') {
        $v = & $py.Source --version 2>&1
        if ($py.Source -match '\\Users\\|\\AppData\\') { Warn "python is in a user profile ($($py.Source)): the service account probably cannot run it; install Python 3.12 for all users" }
        else { Ok "python: $v ($($py.Source))" }
    } else { Bad 'Python is not installed and the download has no bin\tableau_automation.exe: download the folder again from GitLab (it includes bin\)' }
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; $null = Invoke-WebRequest 'https://pypi.org/simple/pip/' -UseBasicParsing -TimeoutSec 10 -Method Head; Ok 'PyPI reachable (packages can be installed)' }
    catch { Warn 'PyPI not reachable: deploy_uat.ps1 cannot install the packages; arrange a proxy or offline wheels' }
}
$drivers = @(Get-OdbcDriver -Platform '64-bit' -ErrorAction SilentlyContinue | Where-Object { $_.Name -in 'ODBC Driver 18 for SQL Server', 'ODBC Driver 17 for SQL Server' } | ForEach-Object Name)
if ($drivers) { Ok "ODBC driver: $($drivers -join ', ')  (put one of them in .env CONN_DRIVER, e.g. {$($drivers[0])})" }
else { Bad 'neither ODBC Driver 18 nor 17 for SQL Server is installed' }
if ((Get-TimeZone).Id -eq 'Singapore Standard Time') { Ok 'time zone Singapore' } else { Warn "time zone is $((Get-TimeZone).Id); the tasks use host time, the scripts Singapore time" }
$ours = @(Get-ScheduledTask -TaskPath '\TableauAutomation\' -ErrorAction SilentlyContinue)
if ($ours) { Warn "this solution's tasks already exist: $($ours.TaskName -join ', ')" } else { Ok 'no \TableauAutomation\ tasks yet' }

if ($LegacyRoot) { $root = $LegacyRoot.TrimEnd('\') } elseif ($roots.Count -eq 1) { $root = $roots[0] } else { $root = $null }
if ($root) {
    $target = Join-Path $root $TargetName
    Write-Host "`n  Install folder would be: $target  (a NEW subfolder next to the legacy folders)" -ForegroundColor Cyan
    if (Test-Path $target) { Warn "$target already exists: use -Update to refresh the code (keeps .env, out\, .venv)" }
}
if ($CheckOnly) { Write-Host "`nCheck only: nothing was changed." -ForegroundColor Green; return }

# ---------------------------------------------------------------- 4. copy
if (-not $elevated) { throw 'Run this in an elevated (Run as administrator) PowerShell.' }
if ($Update -and @(Get-ScheduledTask -TaskPath '\TableauAutomation\' -ErrorAction SilentlyContinue | Where-Object State -eq 'Running')) {
    throw 'A \TableauAutomation\ task is running (its program files are in use): run -Update again when it has finished, best before 07:50 or after 17:40'
}
if (-not $root) { throw 'Legacy folder not determined: run with -LegacyRoot <folder> (see the candidates above).' }
if (-not (Test-Path $root)) { throw "Legacy root $root does not exist" }
if ($TargetName -match '[\\/:]' -or $TargetName -in @('executables_tableau_extract', 'batch_files', 'log', 'windows_scheduler_prod', 'windows_scheduler_UAT', 'source_reference')) {
    throw "TargetName '$TargetName' is not allowed: it must be a new, plain folder name"
}
$target = Join-Path $root $TargetName
if ($source.TrimEnd('\') -ieq $target.TrimEnd('\')) { throw 'The source IS the install folder: start the script from the downloaded copy, not from the install' }
if ((Test-Path $target) -and -not $Update) { throw "$target already exists. Use -Update to copy the new code over it (keeps .env, out\, .venv)." }
if (-not (Test-Path $target) -and $Update) { throw "-Update given but $target does not exist" }

Step '4. Fingerprint the legacy files and tasks (before)'
$LegacyDirs = @('executables_tableau_extract', 'batch_files', 'source_reference', 'windows_scheduler_prod', 'windows_scheduler_UAT') |
    ForEach-Object { Join-Path $root $_ } | Where-Object { Test-Path $_ }
function Get-LegacyState {
    # the legacy folders and the files directly in the root; NOT the log folder (the legacy job writes to it) and not other projects
    $files = @(Get-ChildItem $root -File -Force -ErrorAction SilentlyContinue) +
             @($LegacyDirs | ForEach-Object { Get-ChildItem $_ -Recurse -File -Force -ErrorAction SilentlyContinue })
    $h = @{}
    foreach ($f in $files) { $h[$f.FullName] = (Get-FileHash $f.FullName -Algorithm SHA256).Hash }
    $tasks = @{}
    foreach ($t in $legacyTasks) { $tasks["$($t.TaskPath)$($t.TaskName)"] = (Export-ScheduledTask -TaskPath $t.TaskPath -TaskName $t.TaskName) }
    return @{ files = $h; tasks = $tasks }
}
$before = Get-LegacyState
Write-Host "  $($before.files.Count) legacy file(s) fingerprinted in: $(($LegacyDirs + "$root (top-level files)") -join ', ')"
Write-Host "  (the log folder is skipped: the legacy job writes to it); $($before.tasks.Count) legacy task(s) exported"

Step "5. Copy the solution into $target"
$xd = @('.git', 'out', '.venv', '__pycache__', 'Reference', 'testdata', 'UAT_package', 'build')
$xf = @('.env', '.env.*.backup', '.env.txt', '*_checks.py', 'check_fixtures.py', 'testguard.py', 'publish_map.local.json', '*.log')
New-Item -ItemType Directory -Force $target | Out-Null
& robocopy $source $target /E /XD @xd /XF @xf /NFL /NDL /NJH /NJS /NP /R:1 /W:1 | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed with exit code $LASTEXITCODE" }
if ($packaged) {   # bin\ is replaced exactly (a file left from an older build must not stay next to the new program)
    & robocopy (Join-Path $source 'bin') (Join-Path $target 'bin') /MIR /NFL /NDL /NJH /NJS /NP /R:1 /W:1 | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy of bin\ failed with exit code $LASTEXITCODE" }
}
Copy-Item (Join-Path $source '.env.example') (Join-Path $target '.env.example') -Force   # /XF '.env.*' patterns must not drop it
New-Item -ItemType Directory -Force (Join-Path $target 'out\legacy_snapshot') | Out-Null
foreach ($k in $before.tasks.Keys) { $before.tasks[$k] | Out-File (Join-Path $target ("out\legacy_snapshot\" + ($k.Trim('\') -replace '\\', '_') + '.xml')) -Encoding utf8 }
Ok "copied; legacy task definitions saved in $target\out\legacy_snapshot"
if (Test-Path (Join-Path $target '.env')) { Ok '.env kept as it was' } else { Warn '.env does not exist yet: create it from .env.example (docs\DEPLOYMENT_PROD.md section 2)' }
$tests = @(Get-ChildItem (Join-Path $target 'scripts') -Filter '*_checks.py' -ErrorAction SilentlyContinue)
if ($tests) { Warn "test suites found in the install (copied earlier?): $($tests.Name -join ', '); delete them, they must not be on this host" }

Step '6. Prove the legacy job is untouched (after)'
$after = Get-LegacyState
$changed = @($before.files.Keys | Where-Object { -not $after.files.ContainsKey($_) -or $after.files[$_] -ne $before.files[$_] })
$added = @($after.files.Keys | Where-Object { -not $before.files.ContainsKey($_) })
$taskDiff = @($before.tasks.Keys | Where-Object { $after.tasks[$_] -ne $before.tasks[$_] })
if ($changed -or $added -or $taskDiff) {
    Bad 'legacy files or tasks differ from before (another process may have written them; check):'
    $changed + $added + $taskDiff | ForEach-Object { Write-Host "        $_" }
    exit 1
}
Ok "all $($after.files.Count) legacy files and $($after.tasks.Count) legacy task(s) unchanged"
foreach ($t in $legacyTasks) { $s = (Get-ScheduledTask -TaskPath $t.TaskPath -TaskName $t.TaskName).State; if ($s -eq 'Disabled') { Warn "$($t.TaskName) is Disabled (it was before too)" } else { Ok "$($t.TaskName): $s" } }

Write-Host @"

Next (docs\DEPLOYMENT_PROD.md):
  1. cd "$target"
  2. Create .env from .env.example (new Tableau token, NOT the legacy one; MAIL_TRANSPORT=local).
  3. .\scripts\schedule\deploy_uat.ps1 -ServiceAccount '<service account>'   (program check, DRY-RUN tasks)
"@ -ForegroundColor Green
exit 0
