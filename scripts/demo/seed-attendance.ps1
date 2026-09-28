# A month of eight-hour days for the demo employee, built the way the clock service
# expects to be driven.
#
# `AttendanceService._day_of_punch` decides a clock-out's day from `latest_punch` — the
# newest punch *by instant* — so a backfill is only accepted while each day's clock-in is
# the newest event in the stream. Anything with a later instant (a punch made today, say)
# closes that door, which is what an earlier run of this fixture ran into: every clock-out
# was refused with `ERR_ATT_002`, and the month read as fourteen missing clock-outs.
#
# So: clear this employee's own events (all of them created by this fixture, minutes ago),
# then write day by day in ascending order, then today.
$ErrorActionPreference = 'Stop'
$API = 'http://localhost:8000'
$EMPLOYEE = '63107c20-e703-44cf-b068-439170764433'

function Login([string]$u, [string]$p) {
  $null = Invoke-WebRequest -Uri "$API/api/v1/auth/login" -Method Post `
    -Body (@{ username = $u; password = $p } | ConvertTo-Json) `
    -ContentType 'application/json' -UseBasicParsing -SessionVariable s -TimeoutSec 20
  return $s
}

function Post($session, [string]$path, $body) {
  try {
    return (Invoke-WebRequest -Uri "$API$path" -Method Post -Body ($body | ConvertTo-Json -Depth 6) `
        -ContentType 'application/json' -UseBasicParsing -WebSession $session -TimeoutSec 30).Content |
      ConvertFrom-Json
  } catch {
    $text = $_.ErrorDetails.Message
    if (-not $text) { $text = $_.Exception.Message }
    return [pscustomobject]@{ __error = $text }
  }
}

Write-Output '--- clearing the fixtures this script wrote minutes ago ---'
docker compose exec -T postgres psql -U eam -d eam -q -c @"
delete from attendance_anomalies where employee_id='$EMPLOYEE';
delete from attendance_daily where employee_id='$EMPLOYEE';
delete from attendance_events where employee_id='$EMPLOYEE';
"@ | Out-String

$empleado = Login 'empleado' 'Demo!Passw0rd2026'

Write-Output '--- a month of full days, ascending ---'
$fullDays = @(7, 8, 9, 10, 11, 14, 16, 17, 18, 21, 22, 23, 24, 25)
foreach ($day in $fullDays) {
  $date = '2026-09-{0:d2}' -f $day
  $in = Post $empleado '/api/v1/attendance/clock' @{ kind = 'clock_in'; at = "${date}T08:00:00+02:00" }
  if ($in.__error) { Write-Output "  $date in FAILED: $($in.__error)"; continue }
  $out = Post $empleado '/api/v1/attendance/clock' @{ kind = 'clock_out'; at = "${date}T16:05:00+02:00" }
  if ($out.__error) { Write-Output "  $date out FAILED: $($out.__error)" }
}

Write-Output '--- one day left open, for the correction chain to be about ---'
$open = Post $empleado '/api/v1/attendance/clock' @{ kind = 'clock_in'; at = '2026-09-15T08:05:00+02:00' }
if ($open.__error) { Write-Output "  15th FAILED: $($open.__error)" }

Write-Output '--- today ---'
$todayIn = Post $empleado '/api/v1/attendance/clock' @{ kind = 'clock_in'; at = '2026-09-28T08:02:00+02:00' }
$todayOut = Post $empleado '/api/v1/attendance/clock' @{ kind = 'clock_out'; at = '2026-09-28T16:35:00+02:00' }
Write-Output "today: $($todayIn.event_type) / $($todayOut.event_type) $($todayIn.__error)$($todayOut.__error)"
