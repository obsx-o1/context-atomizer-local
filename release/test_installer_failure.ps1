param(
    [Parameter(Mandatory = $true)][string]$Installer,
    [Parameter(Mandatory = $true)][string]$Checkout,
    [Parameter(Mandatory = $true)][string]$Metadata,
    [Parameter(Mandatory = $true)][string]$Receipt
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'test_installer_common.ps1')

$candidate = $null
$stage = 'bootstrap'
$evidence = @{}
trap {
    Write-LifecycleReceipt -Path $Receipt -Scenario 'failure' -Passed $false -Stage $stage -Candidate $candidate -Evidence $evidence -FailureClass $_.Exception.GetType().Name
    throw
}
$candidate = Read-CandidateMetadata -Metadata $Metadata -Installer $Installer

$applicationDirectory = Join-Path $env:LOCALAPPDATA 'Programs\Context Atomizer Failure Acceptance'
$dataDirectory = Join-Path $env:LOCALAPPDATA 'ContextAtomizer'
$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Context Atomizer Local\Context Atomizer Library.lnk'
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\ContextAtomizerLocal'
$snapshotHelper = Join-Path $Checkout 'release\sqlite_logical_snapshot.py'
$pythonExecutable = (Get-Command python -ErrorAction Stop).Source
if ((Test-Path -LiteralPath $applicationDirectory) -or (Test-Path -LiteralPath $dataDirectory)) { throw 'Disposable failure runner is not clean.' }

$stage = 'install'
$installExit = Invoke-LeafProcess -FilePath $Installer -ArgumentList ('/S /CHATGPT=1 /D={0}' -f $applicationDirectory) -TimeoutSeconds 180 -ReportFailure
$evidence.install_exit = [int]$installExit
if ($installExit -ne 0) { throw "Failure scenario install failed with exit code $installExit." }
$manager = Join-Path $applicationDirectory 'atomizer-local-manager.exe'
$hook = Join-Path $applicationDirectory 'atomizer-codex-hook.exe'
$database = Join-Path $dataDirectory 'history.sqlite3'
$statePath = Join-Path $dataDirectory 'runtime-state.json'
$ready = Wait-RuntimeReady -StatePath $statePath -Manager $manager -TimeoutSeconds 30
$capture = @{
    session_id = 'failure-lifecycle-session'; turn_id = 'failure-lifecycle-user';
    cwd = $Checkout; hook_event_name = 'UserPromptSubmit'; prompt = 'disposable failure lifecycle evidence'
} | ConvertTo-Json -Compress
$capture | & $hook --database $database
if ($LASTEXITCODE -ne 0) { throw 'Failure scenario installed hook could not seed evidence.' }
$ready = Wait-RuntimeReady -StatePath $statePath -Manager $manager -MinimumUnitsIndexed 1 -TimeoutSeconds 30
Stop-DisposableRuntime -Manager $manager -State $ready.State
$snapshotBefore = Get-LogicalSnapshot -Python $pythonExecutable -Helper $snapshotHelper -Database $database -TimeoutSeconds 30

$stage = 'inject_manager_failure'
Copy-Item -LiteralPath (Join-Path $env:SystemRoot 'System32\net.exe') -Destination $manager -Force
$quietCommand = Get-RegisteredQuietUninstall -UninstallKey $uninstallKey -ApplicationDirectory $applicationDirectory
$stage = 'quiet_uninstall'
$uninstall = Invoke-RegisteredQuietUninstall -Command $quietCommand -ApplicationDirectory $applicationDirectory
$evidence.uninstall_exit = [int]$uninstall.ExitCode
$evidence.quiet_uninstall_timed_out = [bool]$uninstall.TimedOut
if ($uninstall.TimedOut -or $uninstall.ExitCode -ne 3) { throw "Unexpected-manager-failure quiet uninstall did not return bounded exit 3; exit=$($uninstall.ExitCode)." }
if ((Test-Path -LiteralPath $applicationDirectory) -or (Test-Path -LiteralPath $uninstallKey) -or (Test-Path -LiteralPath $startMenu)) {
    throw 'Failure-path quiet uninstall did not remove NSIS-owned files, shortcut, and registration.'
}
if (-not (Test-Path -LiteralPath $database)) { throw 'Failure-path quiet uninstall deleted the Library database.' }
$snapshotAfter = Get-LogicalSnapshot -Python $pythonExecutable -Helper $snapshotHelper -Database $database -TimeoutSeconds 30
if ($snapshotAfter.logical_fingerprint -ne $snapshotBefore.logical_fingerprint) { throw 'Failure-path quiet uninstall changed logical Library state.' }
$startupResidual = [bool](Get-ItemProperty -LiteralPath $runKey -Name 'ContextAtomizerLocal' -ErrorAction SilentlyContinue)
$credentialResidual = Test-Path -LiteralPath (Join-Path $dataDirectory 'management-credential.bin')
$extensionCredentialResidual = Test-Path -LiteralPath (Join-Path $dataDirectory 'extension-pairing.bin')
$configResidual = Test-Path -LiteralPath (Join-Path $dataDirectory 'runtime.json')
$permissionsResidual = Test-Path -LiteralPath (Join-Path $dataDirectory 'permissions.json')
$stateResidual = Test-Path -LiteralPath (Join-Path $dataDirectory 'runtime-state.json')
$lockResidual = Test-Path -LiteralPath (Join-Path $dataDirectory 'runtime.lock')
$logResidual = Test-Path -LiteralPath (Join-Path $dataDirectory 'logs')
if (-not $startupResidual -or -not $credentialResidual) {
    throw 'Failure-path fixture did not leave the explicitly expected manager-owned residual state.'
}

$evidence += @{
    quiet_uninstall_bounded = (-not $uninstall.TimedOut); quiet_uninstall_noninteractive = (-not $uninstall.TimedOut)
    quiet_uninstall_temp_copy_removed = $true; quiet_uninstall_stale_copy_preserved = $true
    nsis_owned_application_removed = $true; nsis_owned_registration_removed = $true; nsis_owned_shortcut_removed = $true
    manager_owned_startup_residual_expected = $startupResidual
    manager_owned_credential_residual_expected = $credentialResidual
    manager_owned_extension_credential_residual = $extensionCredentialResidual
    manager_owned_config_residual = $configResidual
    manager_owned_permissions_residual = $permissionsResidual
    manager_owned_state_residual = $stateResidual
    manager_owned_lock_residual = $lockResidual
    manager_owned_log_residual = $logResidual
    manager_failure_did_not_claim_full_cleanup = $true
    library_preserved = $true; pre_logical_fingerprint = [string]$snapshotBefore.logical_fingerprint
    post_logical_fingerprint = [string]$snapshotAfter.logical_fingerprint; pre_post_equal = $true
    sqlite_integrity_check = [string]$snapshotAfter.checks.integrity_check[0]
    sqlite_quick_check = [string]$snapshotAfter.checks.quick_check[0]
    sqlite_foreign_key_violations = @($snapshotAfter.checks.foreign_key_violations).Count
    sqlite_fts_equal = [bool]$snapshotAfter.checks.fts_matches_lexical_projection
}
$stage = 'complete'
Write-LifecycleReceipt -Path $Receipt -Scenario 'failure' -Passed $true -Stage $stage -Candidate $candidate -Evidence $evidence
Write-Output 'LIFECYCLE_RESULT failure=PASS'
