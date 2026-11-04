# Two sessions claim the same subscription/day at once. Dev DWH only. Cleans up its own TEST-conc rows.
# Session A inserts a Claimed row inside an open transaction; session B tries the same claim and blocks on the
# unique index; A commits; B must fail with error 2601. Expected output: PASS.
# Dev DWH server and database come from DEV_DWH_SERVER / DEV_DWH_DATABASE (environment, else the repo .env). Unset = stop.
foreach ($line in (Get-Content (Join-Path $PSScriptRoot "..\..\.env") -ErrorAction SilentlyContinue)) {
    if ($line -match "^(DEV_DWH_SERVER|DEV_DWH_DATABASE)=(.*)$" -and -not (Get-Item "env:$($Matches[1])" -ErrorAction SilentlyContinue)) { Set-Item "env:$($Matches[1])" $Matches[2].Trim() }
}
if (-not $env:DEV_DWH_SERVER -or -not $env:DEV_DWH_DATABASE) { throw "DEV_DWH_SERVER / DEV_DWH_DATABASE not set" }
$cs = "Server=$($env:DEV_DWH_SERVER);Database=$($env:DEV_DWH_DATABASE);Integrated Security=True;TrustServerCertificate=True"
$a = New-Object System.Data.SqlClient.SqlConnection $cs; $a.Open()
$b = New-Object System.Data.SqlClient.SqlConnection $cs; $b.Open()
function Run-Sql($conn, $sql, $tx = $null) { $c = $conn.CreateCommand(); $c.CommandText = $sql; if ($tx) { $c.Transaction = $tx }; [void]$c.ExecuteNonQuery() }
if ($a.Database -ne $env:DEV_DWH_DATABASE) { throw 'wrong database' }
$claim = "INSERT Integration.Tableau_SubscriptionSendLog (Subscription_id, Status) VALUES ('TEST-conc','Claimed')"
try {
    Run-Sql $a "INSERT Integration.Tableau_Subscription (Subscription_id, Workbook_id, TargetType, TargetId, Format) VALUES ('TEST-conc','TEST-wb','Workbook','TEST-wb','PDF')"
    $tx = $a.BeginTransaction()
    Run-Sql $a $claim $tx                                   # A holds the claim, uncommitted
    $cb = $b.CreateCommand(); $cb.CommandText = $claim
    $task = $cb.ExecuteNonQueryAsync()                      # B starts while A is open
    Start-Sleep -Seconds 2
    $blocked = -not $task.IsCompleted                       # B should be waiting on A's lock
    $tx.Commit()
    $err = $null
    try { [void]$task.Wait(15000) } catch { $err = $_.Exception; while ($err -and $err -isnot [System.Data.SqlClient.SqlException]) { $err = $err.InnerException } }
    $num = if ($err -is [System.Data.SqlClient.SqlException]) { $err.Number } else { 0 }
    $rows = $a.CreateCommand(); $rows.CommandText = "SELECT COUNT(*) FROM Integration.Tableau_SubscriptionSendLog WHERE Subscription_id='TEST-conc'"
    $n = $rows.ExecuteScalar()
    $pass = $blocked -and $num -eq 2601 -and $n -eq 1
    "{0}  concurrent claim: B blocked={1}, B error={2}, rows={3}" -f $(if ($pass) { 'PASS' } else { 'FAIL' }), $blocked, $num, $n
} finally {
    Run-Sql $a "DELETE FROM Integration.Tableau_SubscriptionSendLog WHERE Subscription_id='TEST-conc'; DELETE FROM Integration.Tableau_Subscription WHERE Subscription_id='TEST-conc'"
    $a.Close(); $b.Close()
}
