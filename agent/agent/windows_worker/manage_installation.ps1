[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Install', 'Stop', 'Remove')]
    [string]$Mode,
    [Parameter(Mandatory = $true)][string]$ServiceName,
    [Parameter(Mandatory = $true)][string]$TaskName,
    [Parameter(Mandatory = $true)][string]$ServiceCommand,
    [Parameter(Mandatory = $true)][string]$TrayExecutable,
    [Parameter(Mandatory = $true)][string]$TrayArguments,
    [Parameter(Mandatory = $true)][string]$TrayUser,
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [Parameter(Mandatory = $true)][string]$StateRoot,
    [Parameter(Mandatory = $true)][string]$PackageRoot,
    [Parameter(Mandatory = $true)][string]$TrayPipeKeyPath,
    [Parameter(Mandatory = $true)][string]$DeviceSigningKeyPath,
    [Parameter(Mandatory = $true)][string]$TlsClientPrivateKeyPath,
    [Parameter(Mandatory = $true)][string]$TlsClientCertificatePath,
    [Parameter(Mandatory = $true)][string]$TlsCaPath,
    [Parameter(Mandatory = $true)][string]$ServerTrustRoot,
    [Parameter(Mandatory = $true)][string]$PackageTrustRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    $administrator = [Security.Principal.WindowsBuiltInRole]::Administrator
    if (-not $principal.IsInRole($administrator)) {
        throw 'Windows Worker registration requires an elevated administrator process.'
    }
}

function Invoke-CheckedIcacls {
    param(
        [Parameter(Mandatory = $true)][string]$TargetPath,
        [Parameter(Mandatory = $true)][string[]]$Grants
    )
    $icacls = Join-Path $env:SystemRoot 'System32\icacls.exe'
    & $icacls $TargetPath '/inheritance:r' '/grant:r' $Grants | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to apply a closed ACL to $TargetPath"
    }
}

function Set-WorkerAcls {
    foreach ($directory in @($StateRoot, $PackageRoot)) {
        if (Test-Path -LiteralPath $directory -PathType Leaf) {
            throw "Worker root is a file: $directory"
        }
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
        Invoke-CheckedIcacls -TargetPath $directory -Grants @(
            '*S-1-5-18:(OI)(CI)F',
            '*S-1-5-32-544:(OI)(CI)F',
            "${TrayUser}:(OI)(CI)RX"
        )
    }
    foreach ($path in @(
        $DeviceSigningKeyPath,
        $TlsClientPrivateKeyPath,
        $TlsClientCertificatePath,
        $TlsCaPath
    )) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Required Worker service material is missing: $path"
        }
        Invoke-CheckedIcacls -TargetPath $path -Grants @(
            '*S-1-5-18:F',
            '*S-1-5-32-544:F'
        )
    }
    foreach ($directory in @($ServerTrustRoot, $PackageTrustRoot)) {
        if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
            throw "Required Worker trust root is missing: $directory"
        }
        Invoke-CheckedIcacls -TargetPath $directory -Grants @(
            '*S-1-5-18:(OI)(CI)F',
            '*S-1-5-32-544:(OI)(CI)F'
        )
    }
    foreach ($path in @($ConfigPath, $TrayPipeKeyPath)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Required Tray material is missing: $path"
        }
        Invoke-CheckedIcacls -TargetPath $path -Grants @(
            '*S-1-5-18:F',
            '*S-1-5-32-544:F',
            "${TrayUser}:R"
        )
    }
}

function Stop-WorkerHosts {
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($null -ne $service -and $service.Status -ne 'Stopped') {
        Stop-Service -Name $ServiceName -ErrorAction Stop
        $service.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(60))
        $service.Refresh()
        if ($service.Status -ne 'Stopped') {
            throw 'Windows Worker service did not stop cleanly.'
        }
    }
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $task -and $task.State -eq 'Running') {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        $deadline = [DateTime]::UtcNow.AddSeconds(30)
        do {
            Start-Sleep -Milliseconds 200
            $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        } while ($task.State -eq 'Running' -and [DateTime]::UtcNow -lt $deadline)
        if ($task.State -eq 'Running') {
            throw 'Windows Tray task did not stop cleanly.'
        }
    }
}

Assert-Administrator

if ($Mode -eq 'Install') {
    if ($null -ne (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {
        throw 'Windows Worker service is already registered.'
    }
    if ($null -ne (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
        throw 'Windows Tray task is already registered.'
    }
    if (-not (Test-Path -LiteralPath $TrayExecutable -PathType Leaf)) {
        throw 'Windows Tray executable is missing.'
    }

    Set-WorkerAcls
    $serviceCreated = $false
    $taskCreated = $false
    try {
        New-Service `
            -Name $ServiceName `
            -DisplayName 'Boyi Automation Windows Worker' `
            -Description 'Outbound-only signed automation Worker' `
            -BinaryPathName $ServiceCommand `
            -StartupType Automatic | Out-Null
        $serviceCreated = $true
        & (Join-Path $env:SystemRoot 'System32\sc.exe') failure $ServiceName `
            'reset=' '86400' 'actions=' 'restart/60000/restart/60000/""/0' | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw 'Failed to configure Windows Worker recovery policy.'
        }

        $action = New-ScheduledTaskAction -Execute $TrayExecutable -Argument $TrayArguments
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $TrayUser
        $principal = New-ScheduledTaskPrincipal `
            -UserId $TrayUser `
            -LogonType Interactive `
            -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet `
            -MultipleInstances IgnoreNew `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -StartWhenAvailable `
            -ExecutionTimeLimit ([TimeSpan]::Zero)
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $action `
            -Trigger $trigger `
            -Principal $principal `
            -Settings $settings `
            -Description 'Boyi interactive automation Tray Runner' | Out-Null
        $taskCreated = $true
    }
    catch {
        if ($taskCreated) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        }
        if ($serviceCreated) {
            & (Join-Path $env:SystemRoot 'System32\sc.exe') delete $ServiceName | Out-Null
        }
        throw
    }
    # Registration is deliberately side-effect bounded: no host is started,
    # no key material is read and no network connection occurs during install.
    exit 0
}

if ($Mode -eq 'Stop') {
    Stop-WorkerHosts
    exit 0
}

if ($Mode -eq 'Remove') {
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($null -ne $service -and $service.Status -ne 'Stopped') {
        throw 'Windows Worker service must be stopped before removal.'
    }
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $task -and $task.State -eq 'Running') {
        throw 'Windows Tray task must be stopped before removal.'
    }
    if ($null -ne $task) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    }
    if ($null -ne $service) {
        & (Join-Path $env:SystemRoot 'System32\sc.exe') delete $ServiceName | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw 'Failed to remove the Windows Worker service.'
        }
    }
    # State, packages, configuration and protected key files are intentionally
    # retained. They require a separate reviewed cleanup after reconciliation.
    exit 0
}

throw 'Unsupported Windows Worker installation mode.'
