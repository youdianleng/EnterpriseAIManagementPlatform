# Leave requests in every state the screen has to render.
#
# Filed and decided through the API as the three demo accounts, so the balances, the ledger
# history, the calendar overlay and the engine's decisions all come from the product's own
# modules. The manager is the reporting line set for this fixture (the seed leaves the demo
# accounts as their own department's manager, and the engine refuses a request nobody can
# approve).
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
    $text = $_.ErrorDetails.Message
    if (-not $text) { $text = $_.Exception.Message }
    return [pscustomobject]@{ __error = $text }
  }
}

$empleado = Login 'empleado' 'Demo!Passw0rd2026'
$devlead = Login 'devlead' 'Demo!Passw0rd2026'
$rrhh = Login 'rrhh' 'Demo!Passw0rd2026'

function File($type, $start, $end) {
  $draft = Call $empleado 'POST' '/api/v1/leave/requests' @{
    leave_type = $type; start_date = $start; end_date = $end
  }
  if ($draft.__error) { Write-Output "  draft $start refused: $($draft.__error)"; return $null }
  $filed = Call $empleado 'POST' "/api/v1/leave/requests/$($draft.id)/submit"
  if ($filed.__error) { Write-Output "  submit $start refused: $($filed.__error)"; return $null }
  Write-Output "  filed $start..$end ($type): $($filed.business_days_count) working day(s), state $($filed.state)"
  return $filed
}

Write-Output '--- approved: five working days in October ---'
$approved = File 'annual' '2026-10-05' '2026-10-09'
if ($approved) {
  $first = Call $devlead 'POST' "/api/v1/leave/requests/$($approved.id)/decide" @{ decision = 'approve'; comment = 'Sin incidencias en el equipo esa semana.' }
  if ($first.__error) { Write-Output "  level 1 refused: $($first.__error)" }
  $second = Call $rrhh 'POST' "/api/v1/leave/requests/$($approved.id)/decide" @{ decision = 'approve'; comment = 'Registrado en el calendario.' }
  if ($second.__error) { Write-Output "  level 2 refused: $($second.__error)" }
  Write-Output "  approved: state $($second.state)"
}

Write-Output '--- in flight: three days in November ---'
$null = File 'annual' '2026-11-02' '2026-11-04'

Write-Output '--- rejected: two days in October, with the reason an approver must give ---'
$rejected = File 'personal' '2026-10-19' '2026-10-20'
if ($rejected) {
  $decision = Call $devlead 'POST' "/api/v1/leave/requests/$($rejected.id)/decide" @{ decision = 'reject'; comment = 'Esa semana estamos de cierre: pidela para la siguiente.' }
  if ($decision.__error) { Write-Output "  reject refused: $($decision.__error)" }
  Write-Output "  rejected: state $($decision.state)"
}

Write-Output '--- a draft left unfiled, so the list has one to send ---'
$draft = Call $empleado 'POST' '/api/v1/leave/requests' @{
  leave_type = 'annual'; start_date = '2026-12-21'; end_date = '2026-12-23'
}
if ($draft.__error) { Write-Output "  draft refused: $($draft.__error)" } else { Write-Output "  draft: state $($draft.state)" }

Write-Output '--- the balances that came out of it ---'
$balances = Call $empleado 'GET' '/api/v1/leave/balances?year=2026'
foreach ($balance in $balances.items) {
  Write-Output ("  {0}: entitled {1} used {2} pending {3} remaining {4} (history {5})" -f `
    $balance.leave_type, $balance.entitled_days, $balance.used_days, $balance.pending_days, `
    $balance.remaining_days, $balance.history.Count)
}
