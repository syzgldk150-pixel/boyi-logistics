[CmdletBinding()]
param(
    [ValidateSet("auto", "all", "agent", "console", "shared")]
    [string]$Target = "auto",
    [string]$RemoteHost = "100.107.181.3",
    [string]$SshKeyPath = "C:\Users\DENG\.ssh\codex_ecs_ed25519",
    [ValidatePattern("^[0-9a-f]{40}$")]
    [string]$RecoverReleaseHoldSha,
    [switch]$CheckConnection,
    [switch]$SkipRestart,
    [switch]$SkipHealthCheck,
    [switch]$EmergencyUserAuthorizedScheduledWindowOverride
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RemoteUser = "boyce"
# Reuse the verified server identity; this is not a public-network destination.
$SshHostKeyAlias = "123.57.106.70"
$RemoteDeployRoot = "/home/boyce/.boyi-deploy"
$ScriptDir = Split-Path -Parent $PSCommandPath
$AgentRoot = Split-Path -Parent $ScriptDir
$RepoRoot = Split-Path -Parent $AgentRoot
$StateDir = Join-Path $ScriptDir "state"
$StateFile = Join-Path $StateDir "publish_state.json"
$TaskTempRoot = Join-Path $RepoRoot ".task_tmp"
$ReleaseScopeHelper = Join-Path $AgentRoot "scripts/release_scope.py"
$remoteSpec = "${RemoteUser}@${RemoteHost}"
$sshArgs = @(
    "-i", $SshKeyPath,
    "-o", "IdentitiesOnly=yes",
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=yes",
    "-o", "HostKeyAlias=$SshHostKeyAlias",
    "-o", "ConnectTimeout=10"
)
$scpArgs = @(
    "-i", $SshKeyPath,
    "-o", "IdentitiesOnly=yes",
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=yes",
    "-o", "HostKeyAlias=$SshHostKeyAlias",
    "-o", "ConnectTimeout=10"
)

$AgentFiles = @(
    "AGENTS.md", "CLAUDE.md", "README.md", "main.py", "requirements.txt", "requirements.lock",
    "agent.service", "business_composition.py", "harness_composition.py", "start_agent.sh", "stop_agent.sh",
    "dev_local_tunnel.sh"
)
$AgentDirs = @(
    "agent", "deploy", "docs", "feishu", "service_v2_plugins", "knowledge", "prompts", "tms_docs",
    "tools", "plugin_core_adapters", "migrations", "scripts"
)
$ConsoleFiles = @(
    "AGENTS.md", "CLAUDE.md", "README.md", "app.py", "app_support.py", "check_syntax.py", "config.py",
    "console.service", "database.py", "finance_service.py", "known_issues.md",
    "line_haul_contacts.py", "navigation.py", "ocr_providers.py", "preprocessing.py", "requirements.txt",
    "requirements.lock", "runtime_config.py", "start_backend.sh", "stop_backend.sh", "task_queue.py",
    "template_store.py"
)
$ConsoleDirs = @("config", "routes", "services", "static", "templates")
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
    ".feather", ".pkl", ".pickle", ".db", ".sqlite", ".sqlite3", ".tif",
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
    & ssh-keygen -F $SshHostKeyAlias *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "SSH host identity $SshHostKeyAlias for $RemoteHost is not present in known_hosts. Verify the server identity before publishing."
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

function Convert-ToWslUbuntuPath([string]$PathValue) {
    $normalized = $PathValue.Replace("/", "\")
    foreach ($prefix in @("\\wsl.localhost\Ubuntu\", "\\wsl$\Ubuntu\")) {
        if ($normalized.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
            return "/" + $normalized.Substring($prefix.Length).Replace("\", "/")
        }
    }
    throw "Release scope paths must remain inside the configured Ubuntu workspace."
}

function Invoke-ReleaseScopeHelper(
    [string]$RepositoryRoot,
    [string[]]$Arguments
) {
    $linuxHelper = Convert-ToWslUbuntuPath $ReleaseScopeHelper
    $linuxRoot = Convert-ToWslUbuntuPath $RepositoryRoot
    $output = @(
        & wsl.exe -d Ubuntu --exec /usr/bin/python3 `
            $linuxHelper --repository-root $linuxRoot @Arguments
    )
    if ($LASTEXITCODE -ne 0) {
        throw "Release scope validation failed."
    }
    return $output
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
    if ($extension -in @(".png", ".jpg", ".jpeg", ".webp")) {
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
    $content = if ($Lines.Count -gt 0) {
        [string]::Join("`n", $Lines) + "`n"
    }
    else {
        ""
    }
    [IO.File]::WriteAllText($PathValue, $content, $encoding)

    $bytes = [IO.File]::ReadAllBytes($PathValue)
    if ($bytes -contains [byte]13) {
        throw "Generated publish manifest contains a CR byte: $PathValue"
    }
}

function Build-Payload([string]$PayloadRoot) {
    Assert-PathExists $ReleaseScopeHelper
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

    $scopeResult = @(
        Invoke-ReleaseScopeHelper $PayloadRoot @("verify-staged")
    )
    if ($scopeResult.Count -ne 1 -or $scopeResult[0] -cne "release_source_scope=ok") {
        throw "Staged plugin source returned an invalid validation result."
    }
}

function Get-Sha256Hex([string]$PathValue) {
    $stream = [IO.File]::OpenRead($PathValue)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return (($sha.ComputeHash($stream) | ForEach-Object { $_.ToString("x2") }) -join "")
    }
    finally {
        $sha.Dispose()
        $stream.Dispose()
    }
}

function Get-ReleaseFingerprints() {
    $sharedEntries = [Collections.Generic.List[string]]::new()
    $agentEntries = [Collections.Generic.List[string]]::new()
    $consoleEntries = [Collections.Generic.List[string]]::new()
    $sharedAgentPaths = @(
        "agent/migrations/",
        "agent/requirements.txt",
        "agent/requirements.lock",
        "agent/scripts/run_migrations.py",
        "agent/scripts/business_module_migration_contract.py",
        "agent/scripts/migration_055_domain_databases.py",
        "agent/scripts/migration_064_retire_pre_v2.py",
        "agent/scripts/migration_connection.py",
        "agent/scripts/scheduled_write_window.py",
        "agent/scripts/repair_yunda_send_waybills_sheet.py",
        "agent/scripts/verify_locked_environment.py"
    )
    $sharedConsolePaths = @("console/requirements.txt", "console/requirements.lock")

    foreach ($file in Get-ChildItem -LiteralPath $PayloadRoot -Recurse -File | Sort-Object FullName) {
        $relative = $file.FullName.Substring($PayloadRoot.Length).TrimStart("\", "/").Replace("\", "/")
        if ($relative.StartsWith("_manifests/", [StringComparison]::Ordinal)) {
            continue
        }
        $entry = "${relative}=$(Get-Sha256Hex $file.FullName)"
        if ($relative.StartsWith("shared/", [StringComparison]::Ordinal) -or
            ($sharedAgentPaths | Where-Object {
                $relative -ceq $_ -or ($_.EndsWith("/") -and $relative.StartsWith($_, [StringComparison]::Ordinal))
            }) -or
            ($sharedConsolePaths -ccontains $relative)) {
            $sharedEntries.Add($entry)
        }
        elseif ($relative.StartsWith("agent/", [StringComparison]::Ordinal)) {
            $agentEntries.Add($entry)
        }
        elseif ($relative.StartsWith("console/", [StringComparison]::Ordinal)) {
            $consoleEntries.Add($entry)
        }
    }

    $result = @{}
    foreach ($scope in @("agent", "console", "shared")) {
        $entries = switch ($scope) {
            "agent" { $agentEntries }
            "console" { $consoleEntries }
            "shared" { $sharedEntries }
        }
        if ($entries.Count -eq 0) {
            throw "Release fingerprint scope is empty: $scope"
        }
        $joined = [string]::Join("`n", @($entries | Sort-Object))
        $bytes = [Text.Encoding]::UTF8.GetBytes($joined)
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            $result[$scope] = (($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString("x2") }) -join "")
        }
        finally {
            $sha.Dispose()
        }
    }
    return $result
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
    $sharedChanged = $State["sharedHash"] -ne $Fingerprints["shared"]
    switch ($Target) {
        "all" { return @("shared") }
        "shared" { return @("shared") }
        "agent" {
            if ($sharedChanged) {
                throw "Explicit Agent-only release conflicts with changed shared/dependency/migration scope. Publish -Target shared."
            }
            return @("agent")
        }
        "console" {
            if ($sharedChanged) {
                throw "Explicit Console-only release conflicts with changed shared/dependency/migration scope. Publish -Target shared."
            }
            return @("console")
        }
        "auto" {
            if ($sharedChanged) {
                return @("shared")
            }
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

Assert-Command "ssh"
Assert-Command "ssh-keygen"
Assert-PathExists $RepoRoot
Assert-PathExists $SshKeyPath
Assert-SshHostKey

Write-Host "Checking ECS connection over Tailscale: $remoteSpec"
Invoke-Remote 'test "$(id -un)" = boyce && test "$(systemctl show agent.service -p WorkingDirectory --value)" = /home/boyce/agent && test "$(systemctl show console.service -p WorkingDirectory --value)" = /home/boyce/console'
if ($CheckConnection) {
    Write-Host "ECS connection, host identity, user and service directories verified. No release performed."
    return
}

Assert-Command "git"
Assert-Command "wsl.exe"
Assert-Command "scp"
$releaseSha = Assert-CleanPublishedCommit
if (Test-LocalTcpListener 9000) {
    throw "A local Agent is listening on 127.0.0.1:9000. Stop it before publishing."
}

$releaseId = "release-$($releaseSha.Substring(0, 12))-$(Get-Date -Format 'yyyyMMddHHmmss')"
$TaskTempDir = Join-Path $TaskTempRoot $releaseId
$PayloadRoot = Join-Path $TaskTempDir $releaseId
$remoteStage = ""
$remoteStageCreated = $false
$remoteStageBaseTime = Get-Date
$remoteStageSequence = 0

try {
    New-Item -ItemType Directory -Path $PayloadRoot -Force | Out-Null
    Build-Payload $PayloadRoot

    $fingerprints = Get-ReleaseFingerprints
    $state = Load-State
    $targetsToPublish = @(Resolve-Targets $state $fingerprints)
    if ($targetsToPublish.Count -eq 0) {
        Write-Host "No changed source scope detected. Nothing to publish."
        exit 0
    }

    $skipRestartValue = if ($SkipRestart) { "1" } else { "0" }
    $skipHealthValue = if ($SkipHealthCheck) { "1" } else { "0" }
    Invoke-Remote "test `"`$(id -un)`" = boyce && test -d /home/boyce && mkdir -p '$RemoteDeployRoot'"
    foreach ($publishTarget in $targetsToPublish) {
        # Successful releases deliberately retain their recovery bundle. Give
        # each independently published service its own remote stage, while a
        # shared release remains one coordinated invocation and one stage.
        $remoteStageTimestamp = $remoteStageBaseTime.AddSeconds($remoteStageSequence).ToString("yyyyMMddHHmmss")
        $remoteStageSequence += 1
        $remoteStageReleaseId = "release-$($releaseSha.Substring(0, 12))-$remoteStageTimestamp"
        $remoteStage = "${RemoteDeployRoot}/${remoteStageReleaseId}"
        Invoke-Remote "test ! -e '$remoteStage'"
        & scp @scpArgs -r $PayloadRoot "${remoteSpec}:${remoteStage}"
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to upload staged $publishTarget release"
        }
        $remoteStageCreated = $true

        $remoteReleaseCommand = "bash '$remoteStage/agent/deploy/remote_release.sh' '$remoteStage' '$releaseSha' '$publishTarget' '$skipRestartValue' '$skipHealthValue'"
        if ($EmergencyUserAuthorizedScheduledWindowOverride) {
            Write-Warning "Emergency scheduled-window override requested: emergency_user_authorized=true"
            $remoteReleaseCommand += " '--emergency-scheduled-window-override=emergency_user_authorized'"
        }
        if (-not [string]::IsNullOrWhiteSpace($RecoverReleaseHoldSha)) {
            if (-not $EmergencyUserAuthorizedScheduledWindowOverride) {
                $remoteReleaseCommand += " ''"
            }
            $remoteReleaseCommand += " '--recover-held-release=$RecoverReleaseHoldSha'"
        }
        Invoke-Remote $remoteReleaseCommand
        $remoteStageCreated = $false

        $publishedFingerprintScopes = if ($publishTarget -eq "shared") {
            @("agent", "console", "shared")
        }
        else {
            @($publishTarget)
        }
        foreach ($name in $publishedFingerprintScopes) {
            $state["${name}Hash"] = $fingerprints[$name]
        }
        $state["lastPublishedAt"] = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
        $state["lastPublishedTarget"] = $publishTarget
        $state["lastReleaseSha"] = $releaseSha
        Save-State $state
        Write-Host "Publish completed: $publishTarget @ $releaseSha"
    }
}
finally {
    if (
        $remoteStageCreated -and
        $remoteStage.StartsWith("/home/boyce/.boyi-deploy/release-")
    ) {
        # Successful stages are retained as recovery bundles; failed releases
        # normally clean up after a complete rollback. If SSH or rollback fails,
        # this stage may contain the only recovery material.
        Write-Warning "Remote release failed; verify whether recovery material remains at: $remoteStage"
    }
    if (Test-Path -LiteralPath $TaskTempDir) {
        $resolvedTemp = (Resolve-Path -LiteralPath $TaskTempDir).ProviderPath
        $resolvedRoot = (Resolve-Path -LiteralPath $TaskTempRoot).ProviderPath
        if ($resolvedTemp.StartsWith($resolvedRoot, [StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $resolvedTemp -Recurse -Force
        }
    }
}
