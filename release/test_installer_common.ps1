Set-StrictMode -Version Latest

. (Join-Path $PSScriptRoot 'windows_process.ps1')
. (Join-Path $PSScriptRoot 'test_quiet_uninstall.ps1')

function New-HookEntry {
    param([Parameter(Mandatory = $true)][string]$Command)
    return [ordered]@{ hooks = @([ordered]@{ type = 'command'; command = $Command }) }
}

function Get-HookCommands {
    param(
        [Parameter(Mandatory = $true)][object]$Hooks,
        [Parameter(Mandatory = $true)][string]$Event
    )
    $eventProperty = $Hooks.hooks.PSObject.Properties[$Event]
    if ($null -eq $eventProperty) { return ,@() }
    return ,@($eventProperty.Value | ForEach-Object { $_.hooks } | ForEach-Object { $_.command })
}

function Test-LoopbackPortOpen {
    param([Parameter(Mandatory = $true)][int]$Port)
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $attempt = $client.ConnectAsync('127.0.0.1', $Port)
        if (-not $attempt.Wait(250)) { return $false }
        return $client.Connected
    }
    catch { return $false }
    finally { $client.Dispose() }
}

function Wait-RuntimeReady {
    param(
        [Parameter(Mandatory = $true)][string]$StatePath,
        [Parameter(Mandatory = $true)][string]$Manager,
        [int]$MinimumUnitsIndexed = 0,
        [int]$TimeoutSeconds = 30
    )
    $readyDeadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $readyState = $null
    $readyHealth = $null
    do {
        Start-Sleep -Milliseconds 200
        try {
            if (Test-Path -LiteralPath $StatePath) {
                $readyState = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
                $statusResult = Invoke-BoundedProcess -FilePath $Manager -ArgumentList 'status' -TimeoutSeconds 5
                if ($statusResult.ExitCode -eq 0) {
                    $statusPayload = $statusResult.StandardOutput | ConvertFrom-Json
                    $readyHealth = $statusPayload.health
                }
            }
        }
        catch { $readyHealth = $null }
        $derived = if ($readyHealth) { $readyHealth.derived_state } else { $null }
        $requiredUnitsIndexed = (
            $MinimumUnitsIndexed -eq 0 -or
            ($null -ne $derived.units_indexed -and [int]$derived.units_indexed -ge $MinimumUnitsIndexed)
        )
        $ready = (
            $readyState -and $readyHealth -and $readyHealth.runtime_running -and
            $derived.convergence_state -eq 'converged' -and $derived.state -eq 'idle' -and
            $derived.pending_count -eq 0 -and $requiredUnitsIndexed -and
            $derived.units_failed -eq 0 -and -not $derived.last_error_class
        )
    } while ((-not $ready) -and ([DateTime]::UtcNow -lt $readyDeadline))
    if (-not $ready) { throw 'Installed runtime did not become healthy and converge derived Library state.' }
    return [pscustomobject]@{ State = $readyState; Health = $readyHealth }
}

function Stop-DisposableRuntime {
    param(
        [Parameter(Mandatory = $true)][string]$Manager,
        [Parameter(Mandatory = $true)][object]$State
    )
    $stopExit = Invoke-LeafProcess -FilePath $Manager -ArgumentList 'stop' -TimeoutSeconds 30 -ReportFailure
    if ($stopExit -ne 0) { throw "Installed runtime stop failed with exit code $stopExit." }
    $stopDeadline = [DateTime]::UtcNow.AddSeconds(10)
    do {
        Start-Sleep -Milliseconds 100
        $processPresent = [bool](Get-Process -Id ([int]$State.pid) -ErrorAction SilentlyContinue)
        $bridgeListening = Test-LoopbackPortOpen -Port ([int]$State.bridge_port)
        $libraryListening = Test-LoopbackPortOpen -Port ([int]$State.library_port)
    } while (($processPresent -or $bridgeListening -or $libraryListening) -and ([DateTime]::UtcNow -lt $stopDeadline))
    if ($processPresent -or $bridgeListening -or $libraryListening) {
        throw 'Installed runtime process or loopback listener remained active after clean stop.'
    }
}

function Get-LogicalSnapshot {
    param(
        [Parameter(Mandatory = $true)][string]$Python,
        [Parameter(Mandatory = $true)][string]$Helper,
        [Parameter(Mandatory = $true)][string]$Database,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )
    $snapshotPath = Join-Path (Split-Path -Parent $Database) ("validation-logical-snapshot-{0}.json" -f [Guid]::NewGuid().ToString('N'))
    if (Test-Path -LiteralPath $snapshotPath) { throw 'Disposable logical snapshot path already exists.' }
    try {
        $snapshotArguments = '"{0}" "{1}" --output "{2}"' -f $Helper,$Database,$snapshotPath
        $snapshotResult = Invoke-BoundedProcess -FilePath $Python -ArgumentList $snapshotArguments -TimeoutSeconds $TimeoutSeconds -ReportFailure
        if ($snapshotResult.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $snapshotPath -PathType Leaf)) {
            throw 'Logical SQLite snapshot helper failed.'
        }
        try { $snapshot = Get-Content -LiteralPath $snapshotPath -Raw -Encoding UTF8 | ConvertFrom-Json }
        catch { throw 'Logical SQLite snapshot output was not complete valid JSON.' }
        Assert-SnapshotHealthy -Snapshot $snapshot
        return $snapshot
    }
    finally { Remove-Item -LiteralPath $snapshotPath -Force -ErrorAction SilentlyContinue }
}

function Assert-SnapshotHealthy {
    param([Parameter(Mandatory = $true)][object]$Snapshot)
    if ($Snapshot.logical_fingerprint -notmatch '^[0-9a-f]{64}$') { throw 'Snapshot logical fingerprint is invalid.' }
    if (@($Snapshot.checks.integrity_check).Count -ne 1 -or $Snapshot.checks.integrity_check[0] -ne 'ok') {
        throw 'Snapshot integrity_check did not return ok.'
    }
    if (@($Snapshot.checks.quick_check).Count -ne 1 -or $Snapshot.checks.quick_check[0] -ne 'ok') {
        throw 'Snapshot quick_check did not return ok.'
    }
    if (@($Snapshot.checks.foreign_key_violations).Count -ne 0) { throw 'Snapshot contains foreign-key violations.' }
    if (-not $Snapshot.checks.fts_matches_lexical_projection) { throw 'Snapshot FTS projection is inconsistent.' }
}

function Read-CandidateMetadata {
    param(
        [Parameter(Mandatory = $true)][string]$Metadata,
        [Parameter(Mandatory = $true)][string]$Installer
    )
    $candidate = Get-Content -LiteralPath $Metadata -Raw -Encoding UTF8 | ConvertFrom-Json
    $installerItem = Get-Item -LiteralPath $Installer
    $installerSha = (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($candidate.schema_version -ne 1 -or $candidate.installer.sha256 -ne $installerSha) {
        throw 'Downloaded installer does not match candidate metadata.'
    }
    if ([int64]$candidate.installer.size_bytes -ne [int64]$installerItem.Length) {
        throw 'Downloaded installer size does not match candidate metadata.'
    }
    if ($candidate.source_runtime_equal -ne $true) { throw 'Candidate source/runtime fingerprint equality is not true.' }
    return $candidate
}

function Write-LifecycleReceipt {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Scenario,
        [Parameter(Mandatory = $true)][bool]$Passed,
        [Parameter(Mandatory = $true)][string]$Stage,
        [Parameter(Mandatory = $false)][object]$Candidate,
        [Parameter(Mandatory = $false)][hashtable]$Evidence,
        [Parameter(Mandatory = $false)][string]$FailureClass
    )
    $payload = [ordered]@{
        schema_version = 1
        scenario = $Scenario
        passed = $Passed
        stage = $Stage
        failure_class = if ($Passed) { $null } elseif ($FailureClass) { $FailureClass } else { 'unclassified_failure' }
        git_commit_sha = if ($Candidate) { [string]$Candidate.git_commit_sha } else { $null }
        installer_sha256 = if ($Candidate) { [string]$Candidate.installer.sha256 } else { $null }
        installer_size_bytes = if ($Candidate) { [int64]$Candidate.installer.size_bytes } else { $null }
        evidence = if ($Evidence) { $Evidence } else { @{} }
    }
    $parent = Split-Path -Parent $Path
    if ($parent) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    $payload | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Path -Encoding UTF8
}
