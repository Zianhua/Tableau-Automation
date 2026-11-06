<#
.SYNOPSIS
  One-shot UAT install on the Tableau host: venv, packages, folder rights, Event Log source, dry-run check, DRY-RUN scheduled tasks.
  Run from an ELEVATED PowerShell:  .\scripts\schedule\deploy_uat.ps1   (tasks run as the logged-in account unless -ServiceAccount is given)
  Safe to re-run. Nothing is written to the warehouse or Tableau (dry-run only); tasks are registered in DRY-RUN mode.
#>
param([string]$ServiceAccount = "$env:USERDOMAIN\$env:USERNAME")
$ErrorActionPreference = 'Stop'
$app = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location $app

function Step($t) { Write-Host "`n=== $t" -ForegroundColor Cyan }

Step '1/6 checks'
Write-Host "Tasks will run as: $ServiceAccount"
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole('Administrators')) { throw 'Run this in an elevated (Run as administrator) PowerShell.' }
if (-not (Test-Path .env)) { throw ".env is missing in $app" }
$cfg = @{}
Get-Content .env | Where-Object { $_ -match '^\s*([A-Za-z0-9_]+)\s*=\s*([^#]*?)\s*(#.*)?$' } | ForEach-Object { $cfg[$Matches[1]] = $Matches[2] }
foreach ($k in 'CONN_SERVER','CONN_DATABASE','DEV_DWH_SERVER','DEV_DWH_DATABASE','TABLEAU_SERVER_URL','TABLEAU_PAT_NAME','TABLEAU_PAT_SECRET') { if (-not $cfg[$k]) { throw ".env is missing $k" } }
if ($cfg['CONN_SERVER'] -ne $cfg['DEV_DWH_SERVER']) { throw ".env CONN_SERVER ($($cfg['CONN_SERVER'])) must equal DEV_DWH_SERVER ($($cfg['DEV_DWH_SERVER']))" }
if ($cfg['MAIL_TRANSPORT'] -ne 'local') { throw '.env MAIL_TRANSPORT must be local for the first install' }
$dwh = $cfg['CONN_SERVER']; $tab = [uri]$cfg['TABLEAU_SERVER_URL']
Write-Host "Warehouse: $dwh / $($cfg['CONN_DATABASE'])   Tableau: $($tab.AbsoluteUri)"
if ((Get-TimeZone).Id -ne 'Singapore Standard Time') { Write-Warning "Time zone is $((Get-TimeZone).Id), tasks use host local time: expected Singapore Standard Time" }
if (-not (Get-OdbcDriver -Name 'ODBC Driver 17 for SQL Server' -ErrorAction SilentlyContinue)) { throw 'ODBC Driver 17 for SQL Server is not installed' }
foreach ($t in @(@($dwh,1433), @($tab.Host,$tab.Port))) {
    $r = Test-NetConnection $t[0] -Port $t[1] -WarningAction SilentlyContinue
    if (-not $r.TcpTestSucceeded) { throw "Cannot reach $($t[0]):$($t[1])" }
}
$sys = (Get-Command python -ErrorAction Stop).Source
& $sys --version

Step '2/6 virtual environment and packages'
if (-not (Test-Path .venv\Scripts\python.exe)) { & $sys -m venv .venv }
$py = Join-Path $app '.venv\Scripts\python.exe'
& $py -m pip install --upgrade pip
& $py -m pip install tableauserverclient==0.42 pyodbc==5.3.0 python-dotenv==1.2.4 sqlglot==30.13.0 Pillow==13.0.0
& $py -c "import tableauserverclient, pyodbc, dotenv, sqlglot, PIL; print('imports ok')"

Step '3/6 folder rights'
New-Item -ItemType Directory -Force out | Out-Null
icacls $app /grant "${ServiceAccount}:(OI)(CI)RX" | Out-Null
icacls "$app\out" /grant "${ServiceAccount}:(OI)(CI)M" | Out-Null
icacls "$app\.env" /inheritance:r /grant:r "${ServiceAccount}:R" "BUILTIN\Administrators:F" | Out-Null
Write-Host 'done'

Step '4/6 Event Log source'
eventcreate /L APPLICATION /T INFORMATION /ID 1 /SO TableauAutomation /D "registered" 2>&1 | Out-Host

Step '5/6 dry-run check of the mapping sync (reads only)'
& $py scripts\t2_mapping_sync.py --dry-run
if ($LASTEXITCODE -ne 0) { Write-Warning "t2 dry run exited $LASTEXITCODE (see lines above). If it is a database login error, your own login may lack rights the service account has: re-test as the service account later." }

Step '6/6 scheduled tasks (DRY-RUN mode, asks for the account password)'
& .\scripts\schedule\register_tasks.ps1 -RepoRoot $app -ServiceAccount $ServiceAccount
Get-ScheduledTask -TaskPath '\TableauAutomation\' | Get-ScheduledTaskInfo | Format-Table TaskName, LastRunTime, NextRunTime

Write-Host "`nInstalled in DRY-RUN mode. Logs: $app\out. Kill switch: New-Item $app\out\KILL -ItemType File" -ForegroundColor Green
