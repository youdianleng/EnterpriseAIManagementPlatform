# A real two-level correction chain on the day that was left without a clock-out.
#
# Two documents about the same punch, each approved by the manager and then by HR, so the
# interface has an original and two corrections to show as an evolution.
$ErrorActionPreference = 'Stop'
$API = 'http://localhost:8000'

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
    # `ErrorDetails.Message` is the reliable place for a 4xx body in Windows PowerShell:
    # reading the response stream here returns an exhausted one, which is how an earlier
    # run of this script mistook a 422 for a success.
    $text = $_.ErrorDetails.Message
    if (-not $text) { $text = $_.Exception.Message }
    return [pscustomobject]@{ __error = $text }
  }
}

$empleado = Login 'empleado' 'Demo!Passw0rd2026'
$devlead = Login 'devlead' 'Demo!Passw0rd2026'
$rrhh = Login 'rrhh' 'Demo!Passw0rd2026'

$requests = @(
  @{ time = '2026-09-15T16:10:00+02:00'; reason = 'Olvide fichar la salida al terminar la jornada.' },
  @{ time = '2026-09-15T16:30:00+02:00'; reason = 'La hora correcta era las 16:30: estuve cerrando una incidencia.' }
)

foreach ($request in $requests) {
  $draft = Call $empleado 'POST' '/api/v1/attendance/corrections' @{
    business_date = '2026-09-15'
    kind = 'clock_out'
    corrected_at = $request.time
    reason = $request.reason
  }
  if ($draft.__error) { Write-Output "draft refused: $($draft.__error)"; continue }

  $filed = Call $empleado 'POST' "/api/v1/attendance/corrections/$($draft.id)/submit"
  if ($filed.__error) { Write-Output "submit refused: $($filed.__error)"; continue }

  $first = Call $devlead 'POST' "/api/v1/attendance/corrections/$($draft.id)/decide" @{ decision = 'approve'; comment = 'Confirmado con el parte de trabajo.' }
  if ($first.__error) { Write-Output "manager refused: $($first.__error)"; continue }

  $second = Call $rrhh 'POST' "/api/v1/attendance/corrections/$($draft.id)/decide" @{ decision = 'approve'; comment = 'Registrado.' }
  if ($second.__error) { Write-Output "hr refused: $($second.__error)"; continue }
  Write-Output "chain link: $($draft.id) state=$($second.state) applied_event=$($second.applied_event_id)"
}

$record = Call $empleado 'GET' '/api/v1/attendance/punches?business_date=2026-09-15'
Write-Output "day status=$($record.day.status) worked=$($record.day.worked_minutes)"
foreach ($chain in $record.punches) {
  Write-Output ("punch {0} effective={1} corrected={2} madeUp={3}" -f $chain.punch.event_type, $chain.effective_at, $chain.is_corrected, $chain.is_made_up)
  foreach ($correction in $chain.corrections) {
    Write-Output ("  correction at {0} of {1}: {2}" -f $correction.occurred_at, $correction.correction_of_event_id, $correction.reason)
  }
}
Write-Output ("anomalies: {0}" -f (($record.anomalies | ForEach-Object { "$($_.type)(resolved=$($null -ne $_.resolved_by_event_id))" }) -join ', '))

# One document left in flight, so the requests list has a state other than "applied" to
# show — "waiting for approval" is what a reader opens this screen looking for.
$pending = Call $empleado 'POST' '/api/v1/attendance/corrections' @{
  business_date = '2026-09-22'
  kind = 'clock_in'
  corrected_at = '2026-09-22T07:55:00+02:00'
  reason = 'Entre a las 07:55 y fiche a las 08:00: la entrada quedo cinco minutos tarde.'
}
if ($pending.__error) { Write-Output "pending draft refused: $($pending.__error)" }
else {
  $filedPending = Call $empleado 'POST' "/api/v1/attendance/corrections/$($pending.id)/submit"
  Write-Output "in flight: state=$($filedPending.state)"
}
