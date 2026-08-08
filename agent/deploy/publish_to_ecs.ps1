[CmdletBinding()]
param(
    [ValidateSet("auto", "all", "agent", "console")]
    [string]$Target = "auto",
    [string]$RemoteUser = "boyce",
    [string]$RemoteHost = "123.57.106.70",
    [string]$SshKeyPath = "C:\Users\DENG\.ssh\codex_ecs_ed25519",
    [string]$AgentRoot = "",
    [string]$ConsoleRoot = "",
    [string]$RemoteAgentDir = "/home/boyce/agent",
    [string]$RemoteConsoleDir = "/home/boyce/console",
    [switch]$SkipRestart,
    [switch]$SkipHealthCheck,
    [switch]$SkipFeishuCheck
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $PSCommandPath
$DefaultAgentRoot = Split-Path -Parent $ScriptDir
$DefaultProjectsRoot = Split-Path -Parent $DefaultAgentRoot

if ([string]::IsNullOrWhiteSpace($AgentRoot)) {
    $AgentRoot = $DefaultAgentRoot
}
if ([string]::IsNullOrWhiteSpace($ConsoleRoot)) {
    $ConsoleRoot = Join-Path $DefaultProjectsRoot "console"
}

$StateDir = Join-Path $ScriptDir "state"
$StateFile = Join-Path $StateDir "publish_state.json"
$remoteSpec = "$RemoteUser@$RemoteHost"
$sshArgs = @(
    "-i", $SshKeyPath,
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=no"
)
$scpArgs = @(
    "-i", $SshKeyPath,
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=no"
)

$AgentScope = @{
    Name = "agent"
    LocalRoot = $AgentRoot
    RemoteRoot = $RemoteAgentDir
    Service = "agent.service"
    HealthCommand = "curl -fsS http://127.0.0.1:9000/health"
    Items = @(
        @{ Type = "file"; Local = "AGENTS.md"; Remote = "AGENTS.md" },
        @{ Type = "file"; Local = "CLAUDE.md"; Remote = "CLAUDE.md" },
        @{ Type = "file"; Local = "README.md"; Remote = "README.md" },
        @{ Type = "file"; Local = "main.py"; Remote = "main.py" },
        @{ Type = "file"; Local = "requirements.txt"; Remote = "requirements.txt" },
        @{ Type = "file"; Local = "agent.service"; Remote = "agent.service" },
        @{ Type = "file"; Local = "project_overview.md"; Remote = "project_overview.md" },
        @{ Type = "file"; Local = "deploy/publish_to_ecs.ps1"; Remote = "deploy/publish_to_ecs.ps1" },
        @{ Type = "file"; Local = "deploy/publish_to_ecs.md"; Remote = "deploy/publish_to_ecs.md" },
        @{ Type = "dir"; Local = "deploy/nginx"; Remote = "deploy/nginx" },
        @{ Type = "dir"; Local = "agent"; Remote = "agent" },
        @{ Type = "dir"; Local = "docs"; Remote = "docs" },
        @{ Type = "dir"; Local = "feishu"; Remote = "feishu" },
        @{ Type = "dir"; Local = "knowledge"; Remote = "knowledge" },
        @{ Type = "dir"; Local = "prompts"; Remote = "prompts" },
        @{ Type = "dir"; Local = "tms_docs"; Remote = "tms_docs" },
        @{ Type = "dir"; Local = "tools"; Remote = "tools" },
        @{ Type = "dir"; Local = "price_scripts"; Remote = "price_scripts" },
        @{ Type = "dir"; Local = "finance_reconciliation"; Remote = "finance_reconciliation" },
        @{ Type = "dir"; Local = "../shared"; Remote = "../shared" }
    )
}

$ConsoleScope = @{
    Name = "console"
    LocalRoot = $ConsoleRoot
    RemoteRoot = $RemoteConsoleDir
    Service = "console.service"
    HealthCommand = "curl -fsS http://127.0.0.1:8765/ > /dev/null && echo console_ok"
    Items = @(
        @{ Type = "file"; Local = "AGENTS.md"; Remote = "AGENTS.md" },
        @{ Type = "file"; Local = "CLAUDE.md"; Remote = "CLAUDE.md" },
        @{ Type = "file"; Local = "README.md"; Remote = "README.md" },
        @{ Type = "file"; Local = "app.py"; Remote = "app.py" },
        @{ Type = "file"; Local = "check_syntax.py"; Remote = "check_syntax.py" },
        @{ Type = "file"; Local = "config.py"; Remote = "config.py" },
        @{ Type = "file"; Local = "console.service"; Remote = "console.service" },
        @{ Type = "file"; Local = "database.py"; Remote = "database.py" },
        @{ Type = "file"; Local = "finance_service.py"; Remote = "finance_service.py" },
        @{ Type = "file"; Local = "line_haul_contacts.py"; Remote = "line_haul_contacts.py" },
        @{ Type = "file"; Local = "ocr_providers.py"; Remote = "ocr_providers.py" },
        @{ Type = "file"; Local = "preprocessing.py"; Remote = "preprocessing.py" },
        @{ Type = "file"; Local = "requirements.txt"; Remote = "requirements.txt" },
        @{ Type = "file"; Local = "start_backend.sh"; Remote = "start_backend.sh" },
        @{ Type = "file"; Local = "stop_backend.sh"; Remote = "stop_backend.sh" },
        @{ Type = "file"; Local = "task_queue.py"; Remote = "task_queue.py" },
        @{ Type = "file"; Local = "template_store.py"; Remote = "template_store.py" },
        @{ Type = "file"; Local = "known_issues.md"; Remote = "known_issues.md" },
        @{ Type = "dir"; Local = "config"; Remote = "config" },
        @{ Type = "dir"; Local = "static"; Remote = "static" },
        @{ Type = "dir"; Local = "templates"; Remote = "templates" },
        @{ Type = "dir"; Local = "../shared"; Remote = "../shared" }
    )
}

$ScopesByName = @{
    agent = $AgentScope
    console = $ConsoleScope
}

$ExcludedDirNames = @("__pycache__", ".pytest_cache", "logs", "runtime", "state", "temp", "tmp")
$ExcludedFileNames = @(".env", "config.json", "cookies.json", "feishu_ws.lock", "pending_actions.json", "session_meta.json", "storage_state.json")
$ExcludedFileNamePatterns = @("credentials*", "secrets*")
$ExcludedFileExtensions = @(".log", ".pyc")

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

function Join-RemotePath([string]$BasePath, [string]$RelativePath) {
    return ($BasePath.TrimEnd("/") + "/" + ($RelativePath -replace "\\", "/").TrimStart("/"))
}

function Get-RemoteParentPath([string]$RemotePath) {
    $normalized = $RemotePath -replace "\\", "/"
    $lastSlash = $normalized.LastIndexOf("/")
    if ($lastSlash -lt 1) {
        return $normalized
    }
    return $normalized.Substring(0, $lastSlash)
}

function Invoke-Remote([string]$CommandText) {
    Write-Host "REMOTE> $CommandText"
    & ssh @sshArgs $remoteSpec $CommandText
    if ($LASTEXITCODE -ne 0) {
        throw "Remote command failed: $CommandText"
    }
}

function Copy-FileToRemote([string]$SourcePath, [string]$RemotePath) {
    Assert-PathExists $SourcePath
    Write-Host "FILE  $SourcePath -> $RemotePath"
    & scp @scpArgs $SourcePath "${remoteSpec}:${RemotePath}"
    if ($LASTEXITCODE -ne 0) {
        throw "scp file failed: $SourcePath"
    }
}

function Copy-DirToRemote([string]$SourcePath, [string]$RemoteParentDir) {
    Assert-PathExists $SourcePath
    $sourceItem = Get-Item -LiteralPath $SourcePath
    $stageRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("agent_publish_" + [System.Guid]::NewGuid().ToString("N"))
    $stageDir = Join-Path $stageRoot $sourceItem.Name
    try {
        New-Item -ItemType Directory -Path $stageDir -Force | Out-Null
        foreach ($file in Get-PublishFiles $SourcePath $SourcePath) {
            $relativePath = Get-RelativePath $SourcePath $file.FullName
            $targetPath = Join-Path $stageDir $relativePath
            $targetParent = Split-Path -Parent $targetPath
            if (-not (Test-Path -LiteralPath $targetParent)) {
                New-Item -ItemType Directory -Path $targetParent -Force | Out-Null
            }
            Copy-Item -LiteralPath $file.FullName -Destination $targetPath -Force
        }

        Write-Host "DIR   $SourcePath -> $RemoteParentDir"
        & scp @scpArgs -r $stageDir "${remoteSpec}:${RemoteParentDir}"
        if ($LASTEXITCODE -ne 0) {
            throw "scp dir failed: $SourcePath"
        }
    }
    finally {
        if (Test-Path -LiteralPath $stageRoot) {
            Remove-Item -LiteralPath $stageRoot -Recurse -Force
        }
    }
}

function Get-RelativePath([string]$BasePath, [string]$TargetPath) {
    $baseResolved = (Resolve-Path -LiteralPath $BasePath).ProviderPath.TrimEnd("\")
    $targetResolved = (Resolve-Path -LiteralPath $TargetPath).ProviderPath
    $baseUri = [Uri]($baseResolved + "\")
    $targetUri = [Uri]$targetResolved
    return [Uri]::UnescapeDataString($baseUri.MakeRelativeUri($targetUri).ToString()).Replace("/", "\")
}

function Get-StringHash([string]$InputText) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($InputText)
        return (($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString("x2") }) -join "")
    }
    finally {
        $sha.Dispose()
    }
}

function Test-PublishExcluded([string]$BasePath, [string]$PathValue) {
    $relativePath = Get-RelativePath $BasePath $PathValue
    $parts = @($relativePath -split "[\\/]+" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    foreach ($part in $parts) {
        if ($ExcludedDirNames -contains $part) {
            return $true
        }
    }

    $name = [System.IO.Path]::GetFileName($PathValue)
    if ($ExcludedFileNames -contains $name) {
        return $true
    }
    foreach ($pattern in $ExcludedFileNamePatterns) {
        if ($name -like $pattern) {
            return $true
        }
    }

    $extension = [System.IO.Path]::GetExtension($PathValue)
    return ($ExcludedFileExtensions -contains $extension)
}

function Get-PublishFiles([string]$BasePath, [string]$PathValue) {
    $files = @(Get-ChildItem -LiteralPath $PathValue -Recurse -File | Sort-Object FullName)
    return @($files | Where-Object { -not (Test-PublishExcluded $BasePath $_.FullName) })
}

function Get-ScopeFingerprint([hashtable]$Scope) {
    $entries = New-Object System.Collections.Generic.List[string]
    foreach ($item in $Scope.Items) {
        $localPath = Join-Path $Scope.LocalRoot $item.Local
        Assert-PathExists $localPath
        $files = @()
        if ($item.Type -eq "file") {
            $files = @(Get-Item -LiteralPath $localPath)
        }
        elseif ($item.Type -eq "dir") {
            $files = @(Get-PublishFiles $Scope.LocalRoot $localPath)
        }
        else {
            throw "Unsupported item type: $($item.Type)"
        }

        foreach ($file in $files) {
            $relativePath = Get-RelativePath $Scope.LocalRoot $file.FullName
            $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            $entries.Add("$relativePath=$hash")
        }
    }

    return Get-StringHash(([string]::Join("`n", ($entries | Sort-Object))))
}

function Load-State() {
    if (-not (Test-Path -LiteralPath $StateFile)) {
        return @{}
    }
    $raw = Get-Content -Raw -Encoding utf8 -LiteralPath $StateFile
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return @{}
    }

    $parsed = $raw | ConvertFrom-Json
    $state = @{}
    foreach ($property in $parsed.PSObject.Properties) {
        $state[$property.Name] = $property.Value
    }
    return $state
}

function Save-State([hashtable]$State) {
    if (-not (Test-Path -LiteralPath $StateDir)) {
        New-Item -ItemType Directory -Path $StateDir | Out-Null
    }
    $State | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $StateFile -Encoding utf8
}

function Resolve-Targets([hashtable]$State, [hashtable]$Fingerprints) {
    switch ($Target) {
        "all" { return @("agent", "console") }
        "agent" { return @("agent") }
        "console" { return @("console") }
        "auto" {
            $targets = New-Object System.Collections.Generic.List[string]
            foreach ($name in @("agent", "console")) {
                $hashKey = "${name}Hash"
                $changed = ($State[$hashKey] -ne $Fingerprints[$name])
                $status = if ($changed) { "changed" } else { "unchanged" }
                Write-Host ("AUTO  {0,-7} {1}" -f $name, $status)
                if ($changed) {
                    $targets.Add($name)
                }
            }
            return @($targets)
        }
        default {
            throw "Unsupported target: $Target"
        }
    }
}

function Publish-Scope([hashtable]$Scope) {
    Invoke-Remote("mkdir -p '$($Scope.RemoteRoot)'")
    foreach ($item in $Scope.Items) {
        $localPath = Join-Path $Scope.LocalRoot $item.Local
        $remotePath = Join-RemotePath $Scope.RemoteRoot $item.Remote
        $remoteParent = Get-RemoteParentPath $remotePath
        Invoke-Remote("mkdir -p '$remoteParent'")
        if ($item.Type -eq "file") {
            Copy-FileToRemote $localPath $remotePath
        }
        elseif ($item.Type -eq "dir") {
            Copy-DirToRemote $localPath $remoteParent
        }
        else {
            throw "Unsupported item type: $($item.Type)"
        }
    }
}

function Restart-Services([string[]]$TargetNames) {
    foreach ($name in $TargetNames) {
        $scope = $ScopesByName[$name]
        Invoke-Remote("sudo systemctl restart $($scope.Service) && systemctl is-active $($scope.Service)")
    }
}

function Check-Health([string[]]$TargetNames) {
    foreach ($name in $TargetNames) {
        $scope = $ScopesByName[$name]
        $retryCommand = 'bash -lc ''for attempt in 1 2 3 4 5 6 7 8 9 10; do ' + $scope.HealthCommand + ' && exit 0; sleep 2; done; exit 1'''
        Invoke-Remote($retryCommand)
    }
}

function Wait-AgentFeishuReady() {
    if ($SkipFeishuCheck) {
        Write-Host "Skip feishu_ws readiness check."
        return
    }

    $retryCommand = 'for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do last=$(curl -fsS http://127.0.0.1:9000/health) || exit 1; echo "$last"; printf "%s" "$last" | grep -Eq ''feishu_ws[^A-Za-z0-9]+connected'' && exit 0; sleep 2; done; echo "feishu_ws did not become connected within retry window" >&2; exit 1'
    Invoke-Remote($retryCommand)
}

Assert-Command "ssh"
Assert-Command "scp"
Assert-PathExists $AgentRoot
Assert-PathExists $ConsoleRoot
Assert-PathExists $SshKeyPath

$fingerprints = @{
    agent = Get-ScopeFingerprint $AgentScope
    console = Get-ScopeFingerprint $ConsoleScope
}
$state = Load-State
$targetsToPublish = @(Resolve-Targets $state $fingerprints)

if ($targetsToPublish.Count -eq 0) {
    Write-Host "No changed scope detected. Nothing to publish."
    exit 0
}

Write-Host ("Publish target resolved to: {0}" -f ($targetsToPublish -join ", "))

foreach ($name in $targetsToPublish) {
    Publish-Scope $ScopesByName[$name]
}

if (-not $SkipRestart) {
    Restart-Services $targetsToPublish
}
if (-not $SkipHealthCheck) {
    Check-Health $targetsToPublish
    if ($targetsToPublish -contains "agent") {
        Wait-AgentFeishuReady
    }
}

foreach ($name in $targetsToPublish) {
    $state["${name}Hash"] = $fingerprints[$name]
}
$state["lastPublishedAt"] = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
$state["lastPublishedTarget"] = ($targetsToPublish -join ",")
Save-State $state

Write-Host ("Publish completed: {0}" -f ($targetsToPublish -join ", "))
