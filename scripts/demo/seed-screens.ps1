# Everything the three new screens need to render non-empty, for one demo account.
#
# `web/scripts/visual-check.mjs` signs in as `devlead` (the account
# `api/tests/tools/seed_timesheet_demo.py` documents for it), so the attendance and leave
# fixtures have to exist for that account too — otherwise the clock, attendance and leave
# checks run against an empty record.
#
# Run:  $env:EAM_FIXTURE_USER='devlead'; & .\scripts\demo\seed-screens.ps1
#
# **This sets a known password and writes demo data.** The seed proper issues one-time
# passwords precisely so they are never stored in the clear; an automated visual run
# cannot read a password that was printed once and lost, so these fixtures deliberately
# take the other path. They are for the local `docker compose` stack only, and the
# password below belongs to the four demo accounts nothing else depends on.
$ErrorActionPreference = 'Stop'
$API = 'http://localhost:8000'
$WHO = if ($env:EAM_FIXTURE_USER) { $env:EAM_FIXTURE_USER } else { 'empleado' }
$PASSWORD = 'Demo!Passw0rd2026'
# The account that approves this one's corrections and leave: level 1 is the direct manager,
# level 2 is HR. The seed leaves the demo accounts as their own department's manager, and the
# engine refuses a request whose approver is its requester.
$APPROVER = if ($env:EAM_FIXTURE_APPROVER) { $env:EAM_FIXTURE_APPROVER } else { 'empleado' }

function Login([string]$u, [string]$p) {
  $null = Invoke-WebRequest -Uri "$API/api/v1/auth/login" -Method Post `
    -Body (@{ username = $u; password = $p } | ConvertTo-Json) `
    -ContentType 'application/json' -UseBasicParsing -SessionVariable s -TimeoutSec 20
  return $s
}

function Call($session, [string]$method, [string]$path, $body) {
  $parameters = @{
    Uri = "$API$path"; Method = $method; UseBasicParsing = $true
    WebSession = $session; TimeoutSec = 30
  }
  if ($body) { $parameters.Body = ($body | ConvertTo-Json -Depth 6); $parameters.ContentType = 'application/json' }
  try {
    return (Invoke-WebRequest @parameters).Content | ConvertFrom-Json
  } catch {
    $text = $_.ErrorDetails.Message
    if (-not $text) { $text = $_.Exception.Message }
    return [pscustomobject]@{ __error = $text }
  }
}

$employeeId = (docker compose exec -T postgres psql -U eam -d eam -t -A -c "select employee_id from users where username='$WHO';" 2>&1 | Out-String).Trim()
$approverId = (docker compose exec -T postgres psql -U eam -d eam -t -A -c "select employee_id from users where username='$APPROVER';" 2>&1 | Out-String).Trim()
if ($employeeId -notmatch '^[0-9a-f-]{36}$') { throw "could not resolve the employee for $WHO" }
Write-Output "seeding $WHO ($employeeId), approved by $APPROVER ($approverId)"

docker compose exec -T postgres psql -U eam -d eam -q -c @"
update employee_assignments set manager_employee_id='$approverId', updated_at=now()
 where employee_id='$employeeId' and is_primary and end_date is null;
"@ | Out-String

$me = Login $WHO $PASSWORD
$manager = Login $APPROVER $PASSWORD
$hr = Login 'rrhh' $PASSWORD

# --- the clock and the month -------------------------------------------------------------
#
# The employee's own events are cleared first: `AttendanceService._day_of_punch` decides a
# clock-out's day from the newest punch *by instant*, so a backfill is only accepted while
# each day's clock-in is the newest event in the stream.
docker compose exec -T postgres psql -U eam -d eam -q -c @"
delete from attendance_anomalies where employee_id='$employeeId';
delete from attendance_corrections where employee_id='$employeeId';
delete from attendance_daily where employee_id='$employeeId';
delete from attendance_events where employee_id='$employeeId';
"@ | Out-String

$fullDays = @(7, 8, 9, 10, 11, 14, 16, 17, 18, 21, 22, 23, 24, 25)
foreach ($day in $fullDays) {
  $date = '2026-09-{0:d2}' -f $day
  $null = Call $me 'POST' '/api/v1/attendance/clock' @{ kind = 'clock_in'; at = "${date}T08:00:00+02:00" }
  $null = Call $me 'POST' '/api/v1/attendance/clock' @{ kind = 'clock_out'; at = "${date}T16:05:00+02:00" }
}
# One day left open (so the correction chain has something to be about), and a second one that
# stays flagged (so the month view has an anomaly mark to draw).
$null = Call $me 'POST' '/api/v1/attendance/clock' @{ kind = 'clock_in'; at = '2026-09-15T08:05:00+02:00' }
$null = Call $me 'POST' '/api/v1/attendance/clock' @{ kind = 'clock_in'; at = '2026-09-03T08:10:00+02:00' }
$null = Call $me 'POST' '/api/v1/attendance/clock' @{ kind = 'clock_in'; at = '2026-09-28T08:02:00+02:00' }
$null = Call $me 'POST' '/api/v1/attendance/clock' @{ kind = 'clock_out'; at = '2026-09-28T16:35:00+02:00' }
Write-Output 'attendance: a month of full days, one open, one flagged, today closed'

# --- the nightly pass, before anything is corrected --------------------------------------
#
# Order matters: the scan writes the anomaly a correction is meant to *resolve*, so it has to
# run first. Scanning afterwards finds the day already `ok` and writes nothing, which would
# leave the day panel with no "resolved" line to show.
Write-Output '--- scanning the flagged days, so the month carries anomalies ---'
foreach ($date in @('2026-09-03', '2026-09-15')) {
  $scan = docker compose exec -T api python -m app.jobs.scan_attendance_anomalies $date 2>&1 | Select-Object -Last 1
  Write-Output "  $scan"
}

# --- the correction chain ----------------------------------------------------------------
$requests = @(
  @{ time = '2026-09-15T16:10:00+02:00'; reason = 'Olvide fichar la salida al terminar la jornada.' },
  @{ time = '2026-09-15T16:30:00+02:00'; reason = 'La hora correcta era las 16:30: estuve cerrando una incidencia.' }
)
foreach ($request in $requests) {
  $draft = Call $me 'POST' '/api/v1/attendance/corrections' @{
    business_date = '2026-09-15'; kind = 'clock_out'
    corrected_at = $request.time; reason = $request.reason
  }
  if ($draft.__error) { Write-Output "  correction refused: $($draft.__error)"; continue }
  $null = Call $me 'POST' "/api/v1/attendance/corrections/$($draft.id)/submit"
  $null = Call $manager 'POST' "/api/v1/attendance/corrections/$($draft.id)/decide" @{ decision = 'approve'; comment = 'Confirmado con el parte de trabajo.' }
  $applied = Call $hr 'POST' "/api/v1/attendance/corrections/$($draft.id)/decide" @{ decision = 'approve'; comment = 'Registrado.' }
  Write-Output "  chain: $($draft.id) -> $($applied.state)"
}
# One left in flight, for the "waiting for approval" state in the list.
$pending = Call $me 'POST' '/api/v1/attendance/corrections' @{
  business_date = '2026-09-22'; kind = 'clock_in'
  corrected_at = '2026-09-22T07:55:00+02:00'
  reason = 'Entre a las 07:55 y fiche a las 08:00: la entrada quedo cinco minutos tarde.'
}
if (-not $pending.__error) { $null = Call $me 'POST' "/api/v1/attendance/corrections/$($pending.id)/submit" }

# --- leave ------------------------------------------------------------------------------
function File($type, $start, $end) {
  $draft = Call $me 'POST' '/api/v1/leave/requests' @{ leave_type = $type; start_date = $start; end_date = $end }
  if ($draft.__error) { Write-Output "  draft $start refused: $($draft.__error)"; return $null }
  $filed = Call $me 'POST' "/api/v1/leave/requests/$($draft.id)/submit"
  if ($filed.__error) { Write-Output "  submit $start refused: $($filed.__error)"; return $null }
  return [pscustomobject]@{ id = $draft.id; days = $filed.business_days_count }
}

$approved = File 'annual' '2026-10-05' '2026-10-09'
if ($approved) {
  $null = Call $manager 'POST' "/api/v1/leave/requests/$($approved.id)/decide" @{ decision = 'approve'; comment = 'Sin incidencias en el equipo esa semana.' }
  $done = Call $hr 'POST' "/api/v1/leave/requests/$($approved.id)/decide" @{ decision = 'approve'; comment = 'Registrado en el calendario.' }
  Write-Output "leave: approved 05-09/10 ($($approved.days) working days) -> $($done.state)"
}
$null = File 'annual' '2026-11-02' '2026-11-04'
$rejected = File 'personal' '2026-10-19' '2026-10-20'
if ($rejected) {
  $no = Call $manager 'POST' "/api/v1/leave/requests/$($rejected.id)/decide" @{ decision = 'reject'; comment = 'Esa semana estamos de cierre: pidela para la siguiente.' }
  Write-Output "leave: rejected 19-20/10 -> $($no.state)"
}
$draft = Call $me 'POST' '/api/v1/leave/requests' @{ leave_type = 'annual'; start_date = '2026-12-21'; end_date = '2026-12-23' }
Write-Output "leave: draft 21-23/12 -> $($draft.state)"

$balances = Call $me 'GET' '/api/v1/leave/balances?year=2026'
foreach ($balance in $balances.items) {
  Write-Output ("  {0}: entitled {1} used {2} pending {3} remaining {4} (history {5})" -f `
    $balance.leave_type, $balance.entitled_days, $balance.used_days, $balance.pending_days,
    $balance.remaining_days, $balance.history.Count)
}

# --- the assistant's draft forms (ticket 40) ---------------------------------------------
#
# The drafts the Q&A screen draws come from the *product*: `seed_agent_draft.py` runs the three
# draft tools and records the rows the way the graph's node does, in a conversation each. It
# needs the company week and the project `api/tests/tools/seed_timesheet_demo.py` writes (the
# leave draft is priced in working days and the time-entry draft names a task this account may
# book), and it says so itself if they are missing — so this runs it last, after everything
# above, and a stack whose timesheet fixture has never been seeded reports that clearly instead
# of writing a draft the validation would refuse.
Write-Output '--- the assistant''s drafts (run seed_timesheet_demo.py first if this fails) ---'
docker compose exec -T api python /app/tests/tools/seed_agent_draft.py | Select-Object -Last 6
