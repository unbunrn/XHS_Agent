param([switch]$OfflineCheck)
$ErrorActionPreference='Stop'
$taskRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskCompose=Join-Path $taskRoot 'config\xhs-compose.yml'
$taskOut=Join-Path $taskRoot 'var\xhs-container'
$taskExpectedHash='99A4D57C3B043276F603910748CAB5A3AF28794A76C6960ED8890E5C8F59D7F4'
$taskToken='';$taskPreviousAuth=[Environment]::GetEnvironmentVariable('AUTH_TOKEN','Process')
function Set-XhsContainerStage([string]$Stage,[string]$Detail='') {
 New-Item -ItemType Directory -Path $taskOut -Force | Out-Null
 [ordered]@{stage=$Stage;runner='powershell-native';at=[DateTimeOffset]::Now.ToString('o');detail=$Detail;published=$false} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $taskOut 'start-status.json') -Encoding utf8
 Write-Host $Stage
}
try {
 if((Get-FileHash -LiteralPath $taskCompose -Algorithm SHA256).Hash -ne $taskExpectedHash){throw 'Container configuration changed; review before starting.'}
 if($OfflineCheck){Write-Output 'Native launcher offline check passed; no Docker or credentials accessed.';return}
 $taskDocker=(Get-Command docker.exe -CommandType Application -ErrorAction Stop).Source
 $taskExpectedDocker=Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin\docker.exe'
 if([IO.Path]::GetFullPath($taskDocker) -ne [IO.Path]::GetFullPath($taskExpectedDocker)){throw 'Unexpected Docker executable; not running it.'}
 Set-XhsContainerStage 'checking_local_docker'
 $taskContextJson=& $taskDocker --context desktop-linux context inspect desktop-linux --format '{{json .Endpoints.docker.Host}}'
 if($LASTEXITCODE -ne 0){throw 'Cannot inspect Docker desktop-linux context.'}
 $taskContext=$taskContextJson | ConvertFrom-Json
 if($taskContext -notin @('npipe:////./pipe/dockerDesktopLinuxEngine','npipe:////./pipe/docker_engine')){throw 'Docker context is not the approved local Windows engine.'}
 & $taskDocker --context desktop-linux version
 if($LASTEXITCODE -ne 0){throw 'Docker engine access failed from native PowerShell.'}
 $taskExisting=& $taskDocker --context desktop-linux container ls -a --filter 'name=^/bot-xhs-bridge$' --format '{{.ID}}'
 if($LASTEXITCODE -ne 0){throw 'Cannot inspect existing containers.'}
 if($taskExisting) {
  $taskLabelsRaw=& $taskDocker --context desktop-linux inspect --format '{{json .Config.Labels}}' bot-xhs-bridge
  if($LASTEXITCODE -ne 0){throw 'Cannot verify existing container ownership.'}
  $taskLabels=$taskLabelsRaw | ConvertFrom-Json
  if($taskLabels.'com.docker.compose.project' -ne 'bot-xhs'){throw 'Container name belongs to another project; refusing replacement.'}
 } else {
  $taskProbe=New-Object Net.Sockets.TcpClient
  try { $taskPending=$taskProbe.BeginConnect('127.0.0.1',18060,$null,$null); if($taskPending.AsyncWaitHandle.WaitOne(500) -and $taskProbe.Connected){throw 'Port 18060 is occupied; no process will be stopped.'} } finally {$taskProbe.Dispose()}
 }
 $taskPython=Join-Path $taskRoot '.venv\Scripts\python.exe'
 $taskReadToken="import sys; from pathlib import Path; r=Path(sys.argv[1]); sys.path.insert(0,str(r)); from bot.config import load_config; print(load_config(r/'config/bot.toml',root=r,require_file=True).secrets.xiaohongshu_token or '')"
 $taskTokenRaw=& $taskPython -c $taskReadToken $taskRoot
 if($LASTEXITCODE -ne 0){throw 'Cannot load local bridge credential.'}
 $taskToken=([string]$taskTokenRaw).Trim()
 if(-not $taskToken){throw 'Local bridge credential is missing.'}
 [Environment]::SetEnvironmentVariable('AUTH_TOKEN',$taskToken,'Process')
 New-Item -ItemType Directory -Path (Join-Path $taskOut 'data'),(Join-Path $taskRoot 'var\drafts') -Force | Out-Null
 $taskComposeArgs=@('--context','desktop-linux','compose','--file',$taskCompose,'--project-name','bot-xhs')
 & $taskDocker @taskComposeArgs config --quiet
 if($LASTEXITCODE -ne 0){throw 'Container configuration validation failed.'}
 Set-XhsContainerStage 'downloading_approved_image'
 & $taskDocker @taskComposeArgs pull bridge
 if($LASTEXITCODE -ne 0){throw 'Approved image download failed; no publication attempted.'}
 Set-XhsContainerStage 'starting_project_bridge'
 & $taskDocker @taskComposeArgs up --detach bridge
 if($LASTEXITCODE -ne 0){throw 'Project container failed to start.'}
 $taskReady=$false;$taskDeadline=[DateTime]::UtcNow.AddSeconds(90)
 while([DateTime]::UtcNow -lt $taskDeadline) {
  try {$taskHealth=Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:18060/health' -TimeoutSec 3;if($taskHealth.StatusCode -eq 200 -and ($taskHealth.Content | ConvertFrom-Json).success){$taskReady=$true;break}}catch{}
  Start-Sleep -Seconds 2
 }
 if(-not $taskReady){throw 'Container started, but bridge health is not ready.'}
 $taskUnauth=0
 try{$taskResponse=Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:18060/api/v1/login/status' -TimeoutSec 5;$taskUnauth=[int]$taskResponse.StatusCode}catch{if($_.Exception.Response){$taskUnauth=[int]$_.Exception.Response.StatusCode}else{throw 'Authentication check did not receive an HTTP response.'}}
 if($taskUnauth -ne 401){throw 'Bridge access control check failed.'}
 Set-XhsContainerStage 'ready_for_qr' 'HTTP 200 health; HTTP 401 without token.'
 Write-Host 'Ready. Return to the chat for your Xiaohongshu login QR code.'
} catch {
 $taskMessage=$_.Exception.Message
 if($taskToken){$taskMessage=$taskMessage.Replace($taskToken,'[REDACTED]')}
 Set-XhsContainerStage 'failed' $taskMessage
 Write-Host $taskMessage -ForegroundColor Red
} finally {
 [Environment]::SetEnvironmentVariable('AUTH_TOKEN',$taskPreviousAuth,'Process')
 $taskToken=$null;$taskTokenRaw=$null
}


