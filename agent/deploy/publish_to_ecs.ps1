[CmdletBinding()]
param(
    [ValidateSet("auto", "all", "agent", "console")]
    [string]$Target = "auto",
    [string]$RemoteHost = "123.57.106.70",
    [string]$SshKeyPath = "C:\Users\DENG\.ssh\codex_ecs_ed25519",
    [string]$AutomationPluginArtifactRoot,
    [string]$AutomationPluginTrustRoot,
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
$ReleaseScopeHelper = Join-Path $AgentRoot "scripts/first_party_release_scope.py"
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
    "AGENTS.md", "CLAUDE.md", "README.md", "main.py", "requirements.txt", "requirements.lock",
    "agent.service", "start_agent.sh", "stop_agent.sh",
    "dev_local_tunnel.sh"
)
$AgentDirs = @(
    "agent", "deploy", "docs", "feishu", "first_party_automation_plugins", "knowledge", "prompts", "tms_docs",
    "tools", "price_scripts", "plugin_core_adapters", "migrations", "scripts"
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
    "outputs", "reports", "metadata", "data", "windows_worker"
)
$BlockedFileNames = @(
    ".env", "config.json", "cookies.json", "credentials.json", "feishu_ws.lock",
    "pending_actions.json", "session_meta.json", "storage_state.json",
    "windows_worker_host.py", "windows_worker_requirements.txt", "windows_worker_requirements.lock"
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

function Copy-AutomationPluginReleaseInputs(
    [string]$ArtifactRoot,
    [string]$TrustRoot,
    [string]$ExpectedReleaseSha,
    [string]$DestinationRoot
) {
    if ([string]::IsNullOrWhiteSpace($ArtifactRoot) -or [string]::IsNullOrWhiteSpace($TrustRoot)) {
        throw "Agent releases require -AutomationPluginArtifactRoot and -AutomationPluginTrustRoot."
    }
    foreach ($pathValue in @($ArtifactRoot, $TrustRoot)) {
        if (-not (Test-Path -LiteralPath $pathValue -PathType Container)) {
            throw "Automation plugin release input is not a directory: $pathValue"
        }
        $rootItem = Get-Item -LiteralPath $pathValue -Force
        if ($rootItem.LinkType) {
            throw "Automation plugin release input cannot be a symbolic link: $pathValue"
        }
    }

    $artifactItems = @(Get-ChildItem -LiteralPath $ArtifactRoot -Force)
    $zipItems = @($artifactItems | Where-Object { -not $_.PSIsContainer -and $_.Extension -ceq ".zip" })
    $indexItems = @($artifactItems | Where-Object { -not $_.PSIsContainer -and $_.Name -ceq "release-index.json" })
    if ($indexItems.Count -ne 1 -or $zipItems.Count -lt 1 -or $artifactItems.Count -ne ($zipItems.Count + 1)) {
        throw "Signed first-party artifact root must contain one release-index.json and only its ZIP packages."
    }
    if (@($artifactItems | Where-Object { $_.PSIsContainer -or $_.LinkType }).Count -ne 0) {
        throw "Signed first-party artifact root cannot contain directories or symbolic links."
    }
    foreach ($zip in $zipItems) {
        if ($zip.Length -le 0 -or $zip.Length -gt 268435456) {
            throw "Signed first-party package size is invalid: $($zip.Name)"
        }
    }
    $releaseIndex = Get-Content -Raw -Encoding utf8 -LiteralPath $indexItems[0].FullName | ConvertFrom-Json
    if ([string]$releaseIndex.release_sha -cne $ExpectedReleaseSha) {
        throw "Signed first-party release index does not match the committed release SHA."
    }
    $indexedPluginCount = @($releaseIndex.plugins.PSObject.Properties).Count
    if ($indexedPluginCount -ne $zipItems.Count) {
        throw "Signed first-party release index and ZIP package counts differ."
    }

    $trustItems = @(Get-ChildItem -LiteralPath $TrustRoot -Force)
    if ($trustItems.Count -lt 1 -or @(
        $trustItems | Where-Object {
            $_.PSIsContainer -or $_.LinkType -or $_.Extension -cne ".pub" -or $_.Length -le 0
        }
    ).Count -ne 0) {
        throw "Automation plugin trust root must contain only non-empty Ed25519 .pub files."
    }

    $artifactDestination = Join-Path $DestinationRoot "_plugin_artifacts"
    $trustDestination = Join-Path $DestinationRoot "_plugin_trust"
    New-Item -ItemType Directory -Path $artifactDestination -Force | Out-Null
    New-Item -ItemType Directory -Path $trustDestination -Force | Out-Null
    foreach ($item in $artifactItems) {
        Copy-Item -LiteralPath $item.FullName -Destination (Join-Path $artifactDestination $item.Name)
    }
    foreach ($item in $trustItems) {
        Copy-Item -LiteralPath $item.FullName -Destination (Join-Path $trustDestination $item.Name)
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
        throw "First-party release scope validation failed."
    }
    return $output
}

function Get-ReleaseFirstPartyPluginIds() {
    Assert-PathExists $ReleaseScopeHelper
    $values = @(Invoke-ReleaseScopeHelper $RepoRoot @("plugin-ids"))
    $selected = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($value in $values) {
        $pluginId = ([string]$value).Trim()
        if ($pluginId -notmatch '^[a-z][a-z0-9_]{1,63}$' -or -not $selected.Add($pluginId)) {
            throw "First-party release scope returned an invalid or duplicate plugin ID."
        }
    }
    if ($selected.Count -lt 1) {
        throw "First-party release scope is empty."
    }
    return @($selected | Sort-Object)
}

function Test-ReleaseScopedFirstPartyPath(
    [string]$AgentRelativePath,
    [string[]]$ReleasePluginIds
) {
    $normalized = $AgentRelativePath.Replace("\", "/").TrimStart("/")
    $prefix = "first_party_automation_plugins/"
    if (-not $normalized.StartsWith($prefix, [StringComparison]::Ordinal)) {
        return $true
    }
    $tail = $normalized.Substring($prefix.Length)
    if ($tail -in @("README.md", "MIGRATION_MATRIX.md", "digests.json")) {
        return $true
    }
    if ($tail -in @("_runtime/main.py", "_runtime/result.py")) {
        return $true
    }
    $separator = $tail.IndexOf("/", [StringComparison]::Ordinal)
    if ($separator -le 0) {
        return $false
    }
    $pluginId = $tail.Substring(0, $separator)
    return $ReleasePluginIds -ccontains $pluginId
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
    $releasePluginIds = Get-ReleaseFirstPartyPluginIds
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
            if (
                (Test-AllowedRelativePath $relative $AgentFiles $AgentDirs) -and
                (Test-ReleaseScopedFirstPartyPath $relative $releasePluginIds)
            ) {
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
    if ($scopeResult.Count -ne 1 -or $scopeResult[0] -cne "first_party_release_source_scope=ok") {
        throw "Staged first-party source returned an invalid validation result."
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
Assert-Command "wsl.exe"
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
    if ($targetsToPublish -contains "agent") {
        Copy-AutomationPluginReleaseInputs `
            $AutomationPluginArtifactRoot `
            $AutomationPluginTrustRoot `
            $releaseSha `
            $PayloadRoot
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
        # remote_release.sh owns successful cleanup and complete rollback cleanup.
        # If SSH or rollback fails, this stage may contain the only recovery bundle.
        Write-Warning "Remote release stage preserved for recovery: $remoteStage"
    }
    if (Test-Path -LiteralPath $TaskTempDir) {
        $resolvedTemp = (Resolve-Path -LiteralPath $TaskTempDir).ProviderPath
        $resolvedRoot = (Resolve-Path -LiteralPath $TaskTempRoot).ProviderPath
        if ($resolvedTemp.StartsWith($resolvedRoot, [StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $resolvedTemp -Recurse -Force
        }
    }
}
