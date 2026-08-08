[CmdletBinding()]
param(
    [string]$RemoteUser = "boyce",
    [string]$RemoteHost = "123.57.106.70",
    [string]$SshKeyPath = "C:\Users\DENG\.ssh\codex_ecs_ed25519",
    [string]$PublishScriptPath = "",
    [string]$ConsoleAutomationsUrl = "",
    [string]$PriceSmokeEndpoint = "/tms/get_price",
    [string]$PricePayloadPath = "",
    [string]$PostSmokeEndpoint = "/tms/get_scan",
    [string]$PostPayloadPath = "",
    [string]$BrowserSmokeEndpoint = "/tms/fetch_dispatch",
    [string]$BrowserPayloadPath = "",
    [string]$LegacyHttpServicePath = "/root/http_service",
    [string]$LegacyHttpServiceUnit = "http-service.service",
    [string[]]$LegacyN8NServiceUnits = @("n8n.service"),
    [string[]]$LegacyN8NDataPaths = @("/root/.n8n", "/root/n8n", "/root/n8n_data"),
    [switch]$SkipPublish,
    [switch]$SkipInteractiveLogin,
    [switch]$SkipSmoke,
    [switch]$SkipLegacyShutdown,
    [switch]$SkipDelete
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $PSCommandPath
if ([string]::IsNullOrWhiteSpace($PublishScriptPath)) {
    $PublishScriptPath = Join-Path $ScriptDir "publish_to_ecs.ps1"
}
if ([string]::IsNullOrWhiteSpace($ConsoleAutomationsUrl)) {
    $ConsoleAutomationsUrl = "http://$RemoteHost`:8765/automations"
}

$remoteSpec = "$RemoteUser@$RemoteHost"
$sshArgs = @(
    "-i", $SshKeyPath,
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=no"
)

function Assert-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Missing required command: $Name"
    }
}

function Assert-PathExists([string]$PathValue) {
    if (-not (Test-Path -LiteralPath $PathValue)) {
        throw "Path not found: $PathValue"
    }
}

function Invoke-RemotePython([string]$PythonCode) {
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($PythonCode))
    $remoteCommand = "python3 - <<'PY'
import base64
import sys

code = base64.b64decode('$encoded').decode('utf-8')
globals_dict = {'__name__': '__main__'}
exec(compile(code, '<codex-cutover>', 'exec'), globals_dict, globals_dict)
PY"
    $output = & ssh @sshArgs $remoteSpec $remoteCommand
    if ($LASTEXITCODE -ne 0) {
        throw "Remote python command failed."
    }
    return ($output -join "`n").Trim()
}

function Invoke-RemoteBash([string]$ScriptText) {
    $pythonCode = @"
import subprocess
import sys

script = """$ScriptText"""
proc = subprocess.run(["bash", "-lc", script], capture_output=True, text=True)
sys.stdout.write(proc.stdout)
sys.stderr.write(proc.stderr)
raise SystemExit(proc.returncode)
"@
    return Invoke-RemotePython $pythonCode
}

function Invoke-RemoteHealthCheck([string]$Url) {
    $pythonCode = @"
import urllib.request

with urllib.request.urlopen("$Url", timeout=30) as response:
    response.read()
"@
    [void](Invoke-RemotePython $pythonCode)
}

function Get-RemoteJson([string]$Url) {
    $pythonCode = @"
import urllib.request

with urllib.request.urlopen("$Url", timeout=30) as response:
    print(response.read().decode("utf-8"))
"@
    $raw = Invoke-RemotePython $pythonCode
    return $raw | ConvertFrom-Json -Depth 50
}

function Read-JsonFile([string]$PathValue) {
    Assert-PathExists $PathValue
    $raw = Get-Content -LiteralPath $PathValue -Raw -Encoding UTF8
    return $raw | ConvertFrom-Json -AsHashtable -Depth 50
}

function Invoke-RemotePostJson([string]$PathValue, [hashtable]$Payload) {
    $json = $Payload | ConvertTo-Json -Depth 50 -Compress
    $bodyB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
    $pythonCode = @"
import base64
import urllib.request

body = base64.b64decode("$bodyB64")
request = urllib.request.Request(
    "http://127.0.0.1:9000$PathValue",
    data=body,
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=120) as response:
    print(response.read().decode("utf-8"))
"@
    $raw = Invoke-RemotePython $pythonCode
    return $raw | ConvertFrom-Json -Depth 50
}

function Assert-SmokeSuccess([string]$Name, [object]$Payload) {
    if ($null -eq $Payload) {
        throw "$Name smoke returned no payload."
    }
    $ok = $true
    if ($Payload.PSObject.Properties.Name -contains "ok") {
        $ok = [bool]$Payload.ok
    }
    if (-not $ok) {
        $message = ""
        foreach ($candidate in @("message", "error", "detail")) {
            if ($Payload.PSObject.Properties.Name -contains $candidate -and $Payload.$candidate) {
                $message = [string]$Payload.$candidate
                break
            }
        }
        throw "$Name smoke failed: $message"
    }
}

function Test-RemotePath([string]$PathValue) {
    $pythonCode = @"
from pathlib import Path

print("yes" if Path(r"$PathValue").exists() else "no")
"@
    return (Invoke-RemotePython $pythonCode) -eq "yes"
}

function Remove-RemotePathIfExists([string]$PathValue) {
    if (-not (Test-RemotePath $PathValue)) {
        return
    }
    if ($PathValue -in @("/", "/root", "/home", "/home/boyce")) {
        throw "Refuse to delete unsafe path: $PathValue"
    }
    Invoke-RemoteBash("sudo rm -rf -- '$PathValue'")
}

function Test-RemoteService([string]$UnitName) {
    $pythonCode = @"
import subprocess

proc = subprocess.run(
    ["systemctl", "list-unit-files", "--type=service", "--all", "--no-legend"],
    capture_output=True,
    text=True,
    check=False,
)
units = {line.split()[0] for line in proc.stdout.splitlines() if line.strip()}
print("yes" if "$UnitName" in units else "no")
"@
    return (Invoke-RemotePython $pythonCode) -eq "yes"
}

function Stop-And-DisableService([string]$UnitName) {
    if (-not (Test-RemoteService $UnitName)) {
        return
    }
    Invoke-RemoteBash("sudo systemctl stop '$UnitName'; sudo systemctl disable '$UnitName'")
}

function Get-RemotePodmanContainers([string]$Pattern) {
    $pythonCode = @"
import re
import shutil
import subprocess

if shutil.which("podman") is None:
    raise SystemExit(0)
proc = subprocess.run(
    ["sudo", "-n", "podman", "ps", "-a", "--format", "{{.Names}}"],
    capture_output=True,
    text=True,
    check=False,
)
pattern = re.compile(r"$Pattern")
for line in proc.stdout.splitlines():
    name = line.strip()
    if name and pattern.search(name):
        print(name)
"@
    $raw = Invoke-RemotePython $pythonCode
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return @()
    }
    return @($raw -split "`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
}

Assert-Command "ssh"
Assert-Command "powershell"
Assert-PathExists $SshKeyPath
Assert-PathExists $PublishScriptPath

if (-not $SkipPublish) {
    & powershell -ExecutionPolicy Bypass -File $PublishScriptPath -Target all
    if ($LASTEXITCODE -ne 0) {
        throw "publish_to_ecs.ps1 failed."
    }
}

Invoke-RemoteHealthCheck "http://127.0.0.1:9000/health"
Invoke-RemoteHealthCheck "http://127.0.0.1:8765/"
Invoke-RemoteHealthCheck "http://127.0.0.1:9000/admin/tms/session/status"

if (-not $SkipInteractiveLogin) {
    Start-Process $ConsoleAutomationsUrl
    Write-Host "Open /automations and complete the TMS login module flow:"
    Write-Host "1. Click 发送验证码"
    Write-Host "2. Enter the SMS code"
    Write-Host "3. Click 提交验证码"
    $null = Read-Host -Prompt "Press Enter after the page shows 已登录"
}

$sessionStatus = Get-RemoteJson "http://127.0.0.1:9000/admin/tms/session/status"
if (-not $sessionStatus.ok -or -not $sessionStatus.authenticated) {
    throw "TMS session is not authenticated after cutover login smoke."
}

if (-not $SkipSmoke) {
    if ([string]::IsNullOrWhiteSpace($PricePayloadPath)) {
        throw "Price smoke requires -PricePayloadPath."
    }
    if ([string]::IsNullOrWhiteSpace($PostPayloadPath)) {
        throw "POST smoke requires -PostPayloadPath."
    }
    if ([string]::IsNullOrWhiteSpace($BrowserPayloadPath)) {
        throw "Browser smoke requires -BrowserPayloadPath."
    }

    $priceResult = Invoke-RemotePostJson $PriceSmokeEndpoint (Read-JsonFile $PricePayloadPath)
    Assert-SmokeSuccess "price" $priceResult

    $postResult = Invoke-RemotePostJson $PostSmokeEndpoint (Read-JsonFile $PostPayloadPath)
    Assert-SmokeSuccess "post" $postResult

    $browserResult = Invoke-RemotePostJson $BrowserSmokeEndpoint (Read-JsonFile $BrowserPayloadPath)
    Assert-SmokeSuccess "browser" $browserResult
}

if (-not $SkipLegacyShutdown) {
    Stop-And-DisableService $LegacyHttpServiceUnit
    foreach ($unitName in $LegacyN8NServiceUnits) {
        if ([string]::IsNullOrWhiteSpace($unitName)) {
            continue
        }
        Stop-And-DisableService $unitName
    }
    foreach ($containerName in Get-RemotePodmanContainers "(^|[-_])n8n($|[-_])") {
        Invoke-RemoteBash("sudo podman rm -f '$containerName'")
    }
}

if (-not $SkipDelete) {
    Remove-RemotePathIfExists $LegacyHttpServicePath
    foreach ($pathValue in $LegacyN8NDataPaths) {
        if ([string]::IsNullOrWhiteSpace($pathValue)) {
            continue
        }
        Remove-RemotePathIfExists $pathValue
    }
}

Invoke-RemoteHealthCheck "http://127.0.0.1:9000/health"
Invoke-RemoteHealthCheck "http://127.0.0.1:8765/"
Invoke-RemoteHealthCheck "http://127.0.0.1:9000/admin/tms/session/status"

Write-Host "Legacy TMS cutover finished."
