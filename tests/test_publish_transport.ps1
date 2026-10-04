# Offline transport checks: no server access and no real SSH keys.
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$repo = Split-Path -Parent $PSScriptRoot
$publisher = Join-Path $repo "agent/deploy/publish_to_ecs.ps1"
$fixture = Join-Path $repo (".task_tmp/publish-transport-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $fixture -Force | Out-Null
$fixtureKey = Join-Path $fixture "fixture-key.txt"
Set-Content -LiteralPath $fixtureKey -Value "Non-secret test fixture; not a private key." -Encoding Ascii
$global:PublishTestSshCalls = [Collections.Generic.List[object]]::new()
$global:PublishTestSshExit = 0
$global:PublishTestKeygenExit = 0

function Assert-That([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}
function global:ssh-keygen {
    if (($args.Count -ne 2) -or ($args[0] -ne "-F") -or ($args[1] -ne "123.57.106.70")) {
        throw "Host-key lookup must use the previously verified server identity."
    }
    $global:LASTEXITCODE = $global:PublishTestKeygenExit
}
function global:ssh {
    $global:PublishTestSshCalls.Add(@($args))
    $global:LASTEXITCODE = $global:PublishTestSshExit
}
function global:git { throw "Connection check must not run Git." }
function global:scp { throw "Connection check must not upload files." }
function global:wsl.exe { throw "Connection check must not construct a release." }

try {
    $tokens = $null
    $parseErrors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile($publisher, [ref]$tokens, [ref]$parseErrors)
    Assert-That ($parseErrors.Count -eq 0) "Publisher must parse in Windows PowerShell."

    & $publisher -CheckConnection -SshKeyPath $fixtureKey
    Assert-That ($global:PublishTestSshCalls.Count -eq 1) "Preflight must use exactly one SSH connection."
    $arguments = $global:PublishTestSshCalls[0]
    Assert-That ($arguments[-2] -eq "boyce@100.107.181.3") "Default connection must target A over Tailscale."
    foreach ($option in @("HostKeyAlias=123.57.106.70", "IdentitiesOnly=yes", "BatchMode=yes", "StrictHostKeyChecking=yes", "ConnectTimeout=10")) {
        Assert-That ($arguments -contains $option) "Missing required SSH option: $option"
    }
    Assert-That ($arguments -contains $fixtureKey) "The selected key must be passed to the SSH client."
    Assert-That ($arguments[-1].Contains('test "$(id -un)" = boyce')) "Preflight must check the remote user."
    Assert-That ($arguments[-1].Contains('/home/boyce/agent')) "Preflight must check Agent's working directory."
    Assert-That ($arguments[-1].Contains('/home/boyce/console')) "Preflight must check Console's working directory."
    Assert-That ($arguments[-1] -notmatch 'mkdir|restart|remote_release') "Connection check must remain read-only."

    # Execute the real SCP option initializer without entering deployment.
    $SshKeyPath = $fixtureKey
    $SshHostKeyAlias = "123.57.106.70"
    $scpAssignment = $ast.Find({
        param($node)
        ($node -is [Management.Automation.Language.AssignmentStatementAst]) -and
        ($node.Left.Extent.Text -eq '$scpArgs')
    }, $true)
    $scpOptions = @(& ([scriptblock]::Create($scpAssignment.Right.Extent.Text)))
    $sshOptions = $arguments[0..($arguments.Count - 3)]
    Assert-That (($scpOptions -join "`n") -ceq ($sshOptions -join "`n")) "SCP must use the same key and host verification as SSH."

    $global:PublishTestSshCalls.Clear()
    $global:PublishTestKeygenExit = 1
    $failed = $false
    try { & $publisher -CheckConnection -SshKeyPath $fixtureKey }
    catch { $failed = $_.Exception.Message.Contains("not present in known_hosts") }
    Assert-That $failed "Unknown host identity must stop the check."
    Assert-That ($global:PublishTestSshCalls.Count -eq 0) "Unknown host must not be contacted or auto-accepted."

    $global:PublishTestKeygenExit = 0
    $global:PublishTestSshExit = 255
    $failed = $false
    try { & $publisher -CheckConnection -SshKeyPath $fixtureKey }
    catch { $failed = $_.Exception.Message.Contains("Remote command failed") }
    Assert-That $failed "An unreachable Tailscale server must fail the check."
    Assert-That ($global:PublishTestSshCalls.Count -eq 1) "SSH failure must not retry over the public address."
    Write-Host "PASS: Tailscale destination, SSH/SCP identity, read-only preflight and failure handling."
}
finally {
    Remove-Item -LiteralPath $fixtureKey -Force
    Remove-Item -LiteralPath $fixture -Force
}
# Expected failure mocks must not leak their native exit code into the CI wrapper.
# Reaching here means every assertion and cleanup succeeded; exceptions still fail.
$global:LASTEXITCODE = 0
