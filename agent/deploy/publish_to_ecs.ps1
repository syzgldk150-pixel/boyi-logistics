[CmdletBinding()]
param(
    [ValidateSet("auto", "all", "agent", "console")]
    [string]$Target = "auto",
    [string]$RemoteHost = "123.57.106.70",
    [string]$SshKeyPath = "C:\Users\DENG\.ssh\codex_ecs_ed25519",
    [switch]$SkipRestart,
    [switch]$SkipHealthCheck
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RemoteUser = "boyce"
$RemoteDeployRoot = "/home/boyce/.boyi-deploy"
$ScriptDir = Split-Path -Parent $PSCommandPath
$AgentRoot = Split-Path -Parent $ScriptDir
$RepoRoot = Split-Path -Parent $AgentRoot
$StateDir = Join-Path $ScriptDir "state"
$StateFile = Join-Path $StateDir "publish_state.json"
$TaskTempRoot = Join-Path $RepoRoot ".task_tmp"
$remoteSpec = "${RemoteUser}@${RemoteHost}"
$sshArgs = @(
    "-i", $SshKeyPath,
    "-o", "IdentitiesOnly=yes",
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=yes",
    "-o", "ConnectTimeout=10"
)
$scpArgs = @(
    "-i", $SshKeyPath,
    "-o", "IdentitiesOnly=yes",
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=yes",
    "-o", "ConnectTimeout=10"
)

$AgentFiles = @(
    "AGENTS.md", "CLAUDE.md", "README.md", "main.py", "requirements.txt",
    "agent.service", "project_overview.md", "start_agent.sh", "stop_agent.sh",
    "dev_local_tunnel.sh"
)
$AgentDirs = @(
    "agent", "deploy", "docs", "feishu", "knowledge", "prompts", "tms_docs",
    "tools", "price_scripts", "finance_reconciliation"
)
$ConsoleFiles = @(
    "AGENTS.md", "CLAUDE.md", "README.md", "app.py", "check_syntax.py", "config.py",
    "console.service", "database.py", "finance_service.py", "known_issues.md",
    "line_haul_contacts.py", "ocr_providers.py", "preprocessing.py", "requirements.txt",
    "start_backend.sh", "stop_backend.sh", "task_queue.py", "template_store.py"
)
$ConsoleDirs = @("config", "static", "templates")
$BlockedDirNames = @(
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", "logs", "runtime",
    "state", "sessions", "cache", "temp", "tmp", "uploads", "downloads", "output",
    "outputs", "reports", "metadata", "data"
)
$BlockedFileNames = @(
    ".env", "config.json", "cookies.json", "credentials.json", "feishu_ws.lock",
    "pending_actions.json", "session_meta.json", "storage_state.json"
)
$BlockedExtensions = @(
    ".log", ".pyc", ".xlsx", ".xls", ".xlsm", ".csv", ".tsv", ".pdf", ".parquet",
    ".feather", ".pkl", ".pickle", ".db", ".sqlite", ".sqlite3", ".webp", ".tif",
    ".tiff", ".bmp", ".zip", ".7z", ".rar", ".tar", ".gz"
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

function Invoke-Git([string[]]$Arguments) {
    $output = & git -c core.filemode=false -C $RepoRoot @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Git command failed: git $($Arguments -join ' ')`n$($output -join [Environment]::NewLine)"
    }
    return @($output)
}

function Invoke-Remote([string]$CommandText) {
    & ssh @sshArgs $remoteSpec $CommandText
    if ($LASTEXITCODE -ne 0) {
        throw "Remote command failed"
    }
}

function Assert-CleanPublishedCommit() {
    $status = @(Invoke-Git @("status", "--porcelain", "--untracked-files=all"))
    if ($status.Count -gt 0) {
        throw "Git worktree is not clean. Commit and push the release before publishing."
    }

    $branch = [string](Invoke-Git @("branch", "--show-current") | Select-Object -First 1)
    if ([string]::IsNullOrWhiteSpace($branch)) {
        throw "Detached HEAD cannot be published."
    }
    Invoke-Git @("fetch", "--quiet", "origin", $branch) | Out-Null

    $upstream = [string](Invoke-Git @("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}") | Select-Object -First 1)
    $countLine = [string](Invoke-Git @("rev-list", "--left-right", "--count", "${upstream}...HEAD") | Select-Object -First 1)
    $counts = $countLine -split "\s+"
    if ($counts.Count -lt 2 -or $counts[0] -ne "0" -or $counts[1] -ne "0") {
        throw "HEAD does not exactly match its remote upstream: $upstream"
    }

    return [string](Invoke-Git @("rev-parse", "HEAD") | Select-Object -First 1)
}

function Assert-SshHostKey() {
    & ssh-keygen -F $RemoteHost *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "SSH host key for $RemoteHost is not present in known_hosts. Verify and add it manually first."
    }
}

function Test-LocalTcpListener([int]$Port) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $pending = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $pending.AsyncWaitHandle.WaitOne(400)) {
            return $false
        }
        $client.EndConnect($pending)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Test-AllowedRelativePath(
    [string]$RelativePath,
    [string[]]$AllowedFiles,
    [string[]]$AllowedDirs
) {
    $normalized = $RelativePath.Replace("\", "/").TrimStart("/")
    if ($AllowedFiles -contains $normalized) {
        return $true
    }
    foreach ($directory in $AllowedDirs) {
        if ($normalized.StartsWith("$directory/", [StringComparison]::Ordinal)) {
            return $true
        }
    }
    return $false
}

function Test-BlockedPublishPath([string]$RepoRelativePath) {
    $normalized = $RepoRelativePath.Replace("\", "/")
    $parts = @($normalized -split "/" | Where-Object { $_ })
    foreach ($part in $parts) {
        if ($BlockedDirNames -contains $part.ToLowerInvariant()) {
            return $true
        }
    }

    $name = [IO.Path]::GetFileName($normalized)
    $lowerName = $name.ToLowerInvariant()
    if ($BlockedFileNames -contains $lowerName) {
        return $true
    }
    if ($lowerName -like "credentials*" -or $lowerName -like "secrets*") {
        return $true
    }
    if ($lowerName -like ".env.*") {
        return $true
    }

    $extension = [IO.Path]::GetExtension($lowerName)
    if ($BlockedExtensions -contains $extension) {
        return $true
    }
    if ($extension -in @(".png", ".jpg", ".jpeg")) {
        return -not $normalized.StartsWith("console/static/", [StringComparison]::Ordinal)
    }
    return $false
}

function Copy-TrackedFile(
    [string]$RepoRelativePath,
    [string]$Scope,
    [string]$ScopeRelativePath,
    [string]$PayloadRoot,
    [hashtable]$ManifestEntries
) {
    if (Test-BlockedPublishPath $RepoRelativePath) {
        return
    }
    $source = Join-Path $RepoRoot ($RepoRelativePath.Replace("/", [IO.Path]::DirectorySeparatorChar))
    Assert-PathExists $source
    $destination = Join-Path (Join-Path $PayloadRoot $Scope) ($ScopeRelativePath.Replace("/", [IO.Path]::DirectorySeparatorChar))
    $parent = Split-Path -Parent $destination
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    Copy-Item -LiteralPath $source -Destination $destination -Force
    $ManifestEntries[$Scope].Add($ScopeRelativePath.Replace("\", "/"))
}

function Write-Utf8NoBomLines([string]$PathValue, [string[]]$Lines) {
    $encoding = [Text.UTF8Encoding]::new($false)
    [IO.File]::WriteAllLines($PathValue, $Lines, $encoding)
}

function Build-Payload([string]$PayloadRoot) {
    $manifestEntries = @{
        agent = [Collections.Generic.List[string]]::new()
        console = [Collections.Generic.List[string]]::new()
        shared = [Collections.Generic.List[string]]::new()
    }
    foreach ($scope in @("agent", "console", "shared", "_manifests")) {
        New-Item -ItemType Directory -Path (Join-Path $PayloadRoot $scope) -Force | Out-Null
    }

    $trackedFiles = @(Invoke-Git @("ls-files"))
    foreach ($repoPathRaw in $trackedFiles) {
        $repoPath = ([string]$repoPathRaw).Replace("\", "/")
        if ($repoPath.StartsWith("agent/", [StringComparison]::Ordinal)) {
            $relative = $repoPath.Substring(6)
            if (Test-AllowedRelativePath $relative $AgentFiles $AgentDirs) {
                Copy-TrackedFile $repoPath "agent" $relative $PayloadRoot $manifestEntries
            }
        }
        elseif ($repoPath.StartsWith("console/", [StringComparison]::Ordinal)) {
            $relative = $repoPath.Substring(8)
            if (Test-AllowedRelativePath $relative $ConsoleFiles $ConsoleDirs) {
                Copy-TrackedFile $repoPath "console" $relative $PayloadRoot $manifestEntries
            }
        }
        elseif ($repoPath.StartsWith("shared/", [StringComparison]::Ordinal)) {
            $relative = $repoPath.Substring(7)
            Copy-TrackedFile $repoPath "shared" $relative $PayloadRoot $manifestEntries
        }
    }

    foreach ($scope in @("agent", "console", "shared")) {
        $entries = @($manifestEntries[$scope] | Sort-Object -Unique)
        if ($entries.Count -eq 0) {
            throw "Publish whitelist produced an empty scope: $scope"
        }
        Write-Utf8NoBomLines (Join-Path $PayloadRoot "_manifests/$scope.txt") $entries
    }
}

function Get-TreeFingerprint([string[]]$Paths) {
    $entries = [Collections.Generic.List[string]]::new()
    foreach ($pathValue in $Paths) {
        $root = Join-Path $PayloadRoot $pathValue
        foreach ($file in Get-ChildItem -LiteralPath $root -Recurse -File | Sort-Object FullName) {
            $relative = $file.FullName.Substring($PayloadRoot.Length).TrimStart("\", "/").Replace("\", "/")
            $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            $entries.Add("${relative}=${hash}")
        }
    }
    $joined = [string]::Join("`n", @($entries | Sort-Object))
    $bytes = [Text.Encoding]::UTF8.GetBytes($joined)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return (($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString("x2") }) -join "")
    }
    finally {
        $sha.Dispose()
    }
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
    New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
    $json = $State | ConvertTo-Json -Depth 4
    [IO.File]::WriteAllText($StateFile, $json, [Text.UTF8Encoding]::new($false))
}

function Resolve-Targets([hashtable]$State, [hashtable]$Fingerprints) {
    switch ($Target) {
        "all" { return @("agent", "console") }
        "agent" { return @("agent") }
        "console" { return @("console") }
        "auto" {
            $resolved = [Collections.Generic.List[string]]::new()
            foreach ($name in @("agent", "console")) {
                if ($State["${name}Hash"] -ne $Fingerprints[$name]) {
                    $resolved.Add($name)
                }
            }
            return @($resolved)
        }
    }
}

Assert-Command "git"
Assert-Command "ssh"
Assert-Command "scp"
Assert-Command "ssh-keygen"
Assert-PathExists $RepoRoot
Assert-PathExists $SshKeyPath
Assert-SshHostKey

$releaseSha = Assert-CleanPublishedCommit
if (Test-LocalTcpListener 9000) {
    throw "A local Agent is listening on 127.0.0.1:9000. Stop it before publishing."
}

$releaseId = "release-$($releaseSha.Substring(0, 12))-$(Get-Date -Format 'yyyyMMddHHmmss')"
$TaskTempDir = Join-Path $TaskTempRoot $releaseId
$PayloadRoot = Join-Path $TaskTempDir $releaseId
$remoteStage = "${RemoteDeployRoot}/${releaseId}"
$remoteStageCreated = $false

try {
    New-Item -ItemType Directory -Path $PayloadRoot -Force | Out-Null
    Build-Payload $PayloadRoot

    $fingerprints = @{
        agent = Get-TreeFingerprint @("agent", "shared")
        console = Get-TreeFingerprint @("console", "shared")
    }
    $state = Load-State
    $targetsToPublish = @(Resolve-Targets $state $fingerprints)
    if ($targetsToPublish.Count -eq 0) {
        Write-Host "No changed source scope detected. Nothing to publish."
        exit 0
    }

    Invoke-Remote "test `"`$(id -un)`" = boyce && test -d /home/boyce && mkdir -p '$RemoteDeployRoot'"
    & scp @scpArgs -r $PayloadRoot "${remoteSpec}:${RemoteDeployRoot}/"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to upload staged release"
    }
    $remoteStageCreated = $true

    $skipRestartValue = if ($SkipRestart) { "1" } else { "0" }
    $skipHealthValue = if ($SkipHealthCheck) { "1" } else { "0" }
    $targetCsv = $targetsToPublish -join ","
    Invoke-Remote "bash '$remoteStage/agent/deploy/remote_release.sh' '$remoteStage' '$releaseSha' '$targetCsv' '$skipRestartValue' '$skipHealthValue'"
    $remoteStageCreated = $false

    foreach ($name in $targetsToPublish) {
        $state["${name}Hash"] = $fingerprints[$name]
    }
    $state["lastPublishedAt"] = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    $state["lastPublishedTarget"] = $targetCsv
    $state["lastReleaseSha"] = $releaseSha
    Save-State $state
    Write-Host "Publish completed: $targetCsv @ $releaseSha"
}
finally {
    if ($remoteStageCreated -and $remoteStage.StartsWith("/home/boyce/.boyi-deploy/release-")) {
        & ssh @sshArgs $remoteSpec "rm -rf -- '$remoteStage'" *> $null
    }
    if (Test-Path -LiteralPath $TaskTempDir) {
        $resolvedTemp = (Resolve-Path -LiteralPath $TaskTempDir).ProviderPath
        $resolvedRoot = (Resolve-Path -LiteralPath $TaskTempRoot).ProviderPath
        if ($resolvedTemp.StartsWith($resolvedRoot, [StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $resolvedTemp -Recurse -Force
        }
    }
}
