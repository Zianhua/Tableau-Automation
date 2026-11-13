<#
.SYNOPSIS
  Builds bin\tableau_automation.exe (the packaged solution: the host needs no Python). Run on a build machine, never on the host.

.DESCRIPTION
  Creates build\venv from Python 3.12, installs the same pinned packages as deploy_uat.ps1 plus PyInstaller, and builds a
  one-folder program into bin\ (the exe and its DLLs side by side). One-folder, not one-file: the scripts find the install
  folder two levels above their module file, so bin\ must sit directly in the install folder; a one-file exe would unpack to a
  temp folder and lose .env and out\. Ends with "tableau_automation.exe selfcheck". Rebuild after every code change and commit bin\.

  .\scripts\build_exe.ps1
#>
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $repo

$commands = 't2_mapping_sync', 'extract_takeover', 't11_pause', 't4_refresh', 't6_send', 'watchdog', 'daily_summary', 'review_list'

if (-not (Test-Path build\venv\Scripts\python.exe)) { & py -3.12 -m venv build\venv; if ($LASTEXITCODE) { throw 'Python 3.12 (py -3.12) is needed to build' } }
$py = Join-Path $repo 'build\venv\Scripts\python.exe'
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet tableauserverclient==0.42 pyodbc==5.3.0 python-dotenv==1.2.4 sqlglot==30.13.0 Pillow==13.0.0 pyinstaller==6.16.0
if ($LASTEXITCODE) { throw 'pip install failed' }

$hidden = $commands | ForEach-Object { '--hidden-import', $_ }   # started by name through runpy: PyInstaller cannot see them
$ErrorActionPreference = 'Continue'   # PyInstaller logs to stderr; Windows PowerShell would stop on the first line. Exit code checked below.
& $py -m PyInstaller scripts\tableau_automation.py --noconfirm --clean --onedir --console --name tableau_automation `
    --contents-directory . --paths scripts @hidden --collect-all tableauserverclient --collect-submodules sqlglot `
    --distpath build\dist --workpath build\work --specpath build
if ($LASTEXITCODE) { throw 'PyInstaller failed' }
$ErrorActionPreference = 'Stop'

& $py scripts\tableau_automation.py sourcehash | Set-Content build\dist\tableau_automation\SOURCE_HASH.txt -Encoding ascii   # selfcheck refuses a bin\ built from other code
& robocopy build\dist\tableau_automation bin /MIR /NFL /NDL /NJH /NJS /NP /R:3 /W:2 | Out-Null   # a copy: antivirus may still hold the new files
if ($LASTEXITCODE -ge 8) { throw "copy to bin\ failed (robocopy $LASTEXITCODE)" }
& .\bin\tableau_automation.exe selfcheck
if ($LASTEXITCODE) { throw 'selfcheck failed' }
Write-Host "`nBuilt $repo\bin\tableau_automation.exe" -ForegroundColor Green
