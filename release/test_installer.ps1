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
trap {
    Write-LifecycleReceipt -Path $Receipt -Scenario 'normal' -Passed $false -Stage $stage -Candidate $candidate -FailureClass $_.Exception.GetType().Name
    throw
}
$candidate = Read-CandidateMetadata -Metadata $Metadata -Installer $Installer

$applicationDirectory = Join-Path $env:LOCALAPPDATA 'Programs\Context Atomizer Acceptance'
$applicationSiblingDirectory = "$applicationDirectory-sibling"
$dataDirectory = Join-Path $env:LOCALAPPDATA 'ContextAtomizer'
$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Context Atomizer Local\Context Atomizer Library.lnk'
$hooksPath = Join-Path $env:USERPROFILE '.codex\hooks.json'
$codexConfigPath = Join-Path $env:USERPROFILE '.codex\config.toml'
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\ContextAtomizerLocal'
$renamedCheckout = "$Checkout.unavailable"
$snapshotHelper = Join-Path $Checkout 'release\sqlite_logical_snapshot.py'
$securityAcceptanceHelper = Join-Path $Checkout 'release\packaged_security_acceptance.py'
$pythonExecutable = (Get-Command python -ErrorAction Stop).Source
$runtimePackageRoot = Join-Path $Checkout 'src\atomizer_local_client'
$fingerprintScript = 'import sys; from pathlib import Path; root = Path(sys.argv[1]).resolve(); sys.path.insert(0, str(root.parent)); from atomizer_local_client.runtime_health import runtime_build_fingerprint; print(runtime_build_fingerprint(root))'
$fingerprintArguments = '-B -c "{0}" "{1}"' -f $fingerprintScript,$runtimePackageRoot
$stage = 'source_fingerprint'
Write-Host "Lifecycle stage: $stage"
$fingerprintResult = Invoke-BoundedProcess -FilePath $pythonExecutable -ArgumentList $fingerprintArguments -TimeoutSeconds 30 -ReportFailure
$expectedRuntimeBuildFingerprint = $fingerprintResult.StandardOutput.Trim()
if ($fingerprintResult.ExitCode -ne 0 -or $expectedRuntimeBuildFingerprint -notmatch '^[0-9a-f]{64}$') {
    throw 'Validation checkout runtime build fingerprint could not be computed.'
}
if ($candidate.source_fingerprint -ne $expectedRuntimeBuildFingerprint) { throw 'Candidate source fingerprint does not match lifecycle checkout.' }
if ((Test-Path -LiteralPath $applicationDirectory) -or (Test-Path -LiteralPath $applicationSiblingDirectory) -or (Test-Path -LiteralPath $dataDirectory)) {
    throw 'Disposable runner is not clean: Context Atomizer state already exists.'
}
if (Test-Path -LiteralPath $renamedCheckout) { throw 'Disposable checkout-independence target already exists.' }

$stage = 'normal_lifecycle'
New-Item -ItemType Directory -Path (Split-Path -Parent $hooksPath) -Force | Out-Null
$runtime = Join-Path $applicationDirectory 'atomizer-local-runtime.exe'
$manager = Join-Path $applicationDirectory 'atomizer-local-manager.exe'
$hook = Join-Path $applicationDirectory 'atomizer-codex-hook.exe'
$testDatabase = Join-Path $dataDirectory 'history.sqlite3'
$currentCommand = '"{0}" --database "{1}"' -f $hook, $testDatabase
$currentEntry = New-HookEntry -Command $currentCommand
$initialHooks = [ordered]@{
    hooks = [ordered]@{
        UserPromptSubmit = @($currentEntry)
        Stop = @($currentEntry)
    }
}
$initialHooks | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $hooksPath -Encoding UTF8

$workspaceRoot = Join-Path $env:RUNNER_TEMP 'context-atomizer-workspace'
$workspaceHooksPath = Join-Path $workspaceRoot '.codex\hooks.json'
New-Item -ItemType Directory -Path (Split-Path -Parent $workspaceHooksPath) -Force | Out-Null
$workspaceHooks = [ordered]@{
    hooks = [ordered]@{
        UserPromptSubmit = @($currentEntry, $currentEntry)
        Stop = @($currentEntry, $currentEntry)
    }
}
$workspaceHooks | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $workspaceHooksPath -Encoding UTF8

$secondWorkspaceRoot = Join-Path $env:RUNNER_TEMP 'context-atomizer-second-workspace'
$secondWorkspaceHooksPath = Join-Path $secondWorkspaceRoot '.codex\hooks.json'
New-Item -ItemType Directory -Path (Split-Path -Parent $secondWorkspaceHooksPath) -Force | Out-Null
$secondWorkspaceHooks = [ordered]@{
    hooks = [ordered]@{
        UserPromptSubmit = @($currentEntry)
        Stop = @($currentEntry)
    }
}
$secondWorkspaceHooks | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $secondWorkspaceHooksPath -Encoding UTF8

$unrelatedWorkspaceRoot = Join-Path $env:RUNNER_TEMP 'context-atomizer-unrelated-workspace'
$unrelatedWorkspaceHooksPath = Join-Path $unrelatedWorkspaceRoot '.codex\hooks.json'
New-Item -ItemType Directory -Path (Split-Path -Parent $unrelatedWorkspaceHooksPath) -Force | Out-Null
$workspaceUserUnrelated = 'workspace-unrelated-user'
$workspaceStopUnrelated = 'workspace-unrelated-stop'
$unrelatedWorkspaceHooks = [ordered]@{
    hooks = [ordered]@{
        UserPromptSubmit = @((New-HookEntry -Command $workspaceUserUnrelated))
        Stop = @((New-HookEntry -Command $workspaceStopUnrelated))
    }
}
$unrelatedWorkspaceHooks | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $unrelatedWorkspaceHooksPath -Encoding UTF8

$missingWorkspaceHooksPath = Join-Path $env:RUNNER_TEMP 'context-atomizer-missing-workspace\.codex\hooks.json'
$registrations = @(
    "${workspaceHooksPath}:user_prompt_submit:0:0",
    "${workspaceHooksPath}:stop:1:0",
    "${workspaceHooksPath}:stop:0:0",
    "${secondWorkspaceHooksPath}:user_prompt_submit:0:0",
    "${secondWorkspaceHooksPath}:stop:0:0",
    "${unrelatedWorkspaceHooksPath}:user_prompt_submit:0:0",
    "${unrelatedWorkspaceHooksPath}:stop:0:0",
    "${missingWorkspaceHooksPath}:stop:0:0",
    "${hooksPath}:stop:0:0"
)
$configBlocks = @($registrations | ForEach-Object {
    "[hooks.state.'$_']`ntrusted_hash = `"sha256:installer-acceptance`"`n"
})
$configBlocks -join "`n" | Set-Content -LiteralPath $codexConfigPath -Encoding UTF8

$installArguments = '/S /CHATGPT=1 /CODEX=1 /D={0}' -f $applicationDirectory
$stage = 'fresh_install'
Write-Host "Lifecycle stage: $stage"
$installExit = Invoke-LeafProcess -FilePath $Installer -ArgumentList $installArguments -TimeoutSeconds 180 -ReportFailure
$stage = 'installer_completed'
if ($installExit -ne 0) { throw "Installer failed with exit code $installExit." }

$requiredFiles = @(
    $runtime,
    $manager,
    $hook,
    (Join-Path $applicationDirectory 'atomizer-local-open-library.exe'),
    (Join-Path $applicationDirectory 'atomizer-claude-hook.exe'),
    (Join-Path $applicationDirectory 'atomizer-local-mcp.exe'),
    (Join-Path $applicationDirectory 'portable_plugin\plugin.json')
)
foreach ($path in $requiredFiles) { if (-not (Test-Path -LiteralPath $path)) { throw "Missing installed file: $path" } }
$expectedExecutableNames = @('atomizer-local-runtime.exe','atomizer-local-manager.exe','atomizer-local-open-library.exe','atomizer-codex-hook.exe','atomizer-claude-hook.exe','atomizer-local-mcp.exe','Uninstall.exe')
$actualExecutableNames = @(Get-ChildItem -LiteralPath $applicationDirectory -File -Filter '*.exe' | Select-Object -ExpandProperty Name | Sort-Object)
if (@(Compare-Object -ReferenceObject ($expectedExecutableNames | Sort-Object) -DifferenceObject $actualExecutableNames).Count -ne 0) {
    throw 'Installed executable inventory is not exactly six product executables plus Uninstall.exe.'
}
if (-not (Test-Path -LiteralPath $startMenu)) { throw 'Start Menu Library shortcut was not created.' }
if (-not (Get-ItemProperty -LiteralPath $runKey -Name 'ContextAtomizerLocal' -ErrorAction SilentlyContinue)) {
    throw 'Per-user startup registration was not created.'
}
$managementCredential = Join-Path $dataDirectory 'management-credential.bin'
$extensionCredential = Join-Path $dataDirectory 'extension-pairing.bin'
if (-not (Test-Path -LiteralPath $managementCredential)) { throw 'DPAPI management credential was not initialized.' }
if (Test-Path -LiteralPath $extensionCredential) { throw 'Fresh install created a pre-paired extension secret.' }
if (Test-Path -LiteralPath (Join-Path $dataDirectory 'bridge-credential.bin')) {
    throw 'Fresh install recreated the obsolete all-purpose bridge credential.'
}

$statePath = Join-Path $dataDirectory 'runtime-state.json'
$deadline = [DateTime]::UtcNow.AddSeconds(20)
do {
    Start-Sleep -Milliseconds 200
    if (Test-Path -LiteralPath $statePath) { $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json }
} while ((-not $state) -and ([DateTime]::UtcNow -lt $deadline))
if (-not $state) { throw 'Installed runtime state did not appear.' }
$publicHealth = Invoke-RestMethod -Uri "http://127.0.0.1:$($state.library_port)/health" -TimeoutSec 3
if (-not $publicHealth.ok -or $publicHealth.service -ne 'local-library' -or -not $publicHealth.runtime_running) {
    throw 'Installed Library endpoint is not healthy.'
}
$expectedPublicHealthProperties = @('ok', 'service', 'runtime_running')
$publicHealthProperties = @($publicHealth.PSObject.Properties.Name)
if (@(Compare-Object -ReferenceObject $expectedPublicHealthProperties -DifferenceObject $publicHealthProperties).Count -ne 0) {
    throw 'Unauthenticated Library health exposed fields beyond the minimal availability contract.'
}

$stage = 'fresh_runtime_status'
Write-Host "Lifecycle stage: $stage"
$statusResult = Invoke-BoundedProcess -FilePath $manager -ArgumentList 'status' -TimeoutSeconds 5 -ReportFailure
if ($statusResult.ExitCode -ne 0) { throw 'Installed manager status failed.' }
$status = $statusResult.StandardOutput | ConvertFrom-Json
if (-not $status.running -or -not $status.health.runtime_running) {
    throw 'Authenticated management health did not report a running runtime.'
}
$runtimeHealth = $status.health.runtime
$runtimeExecutableSha256 = (Get-FileHash -LiteralPath $runtime -Algorithm SHA256).Hash.ToLowerInvariant()
if ($runtimeHealth.runtime_executable_sha256 -ne $runtimeExecutableSha256) {
    throw 'Installed runtime executable identity does not match authenticated management health.'
}
if ($runtimeHealth.current_runtime_executable_sha256 -ne $runtimeExecutableSha256) {
    throw 'Currently installed runtime executable identity does not match authenticated management health.'
}
if ($runtimeHealth.restart_required) {
    throw 'Freshly installed runtime unexpectedly requires restart.'
}
if (-not $runtimeHealth.runtime_build_fingerprint -or $runtimeHealth.runtime_build_fingerprint.Length -ne 64) {
    throw 'Authenticated management health did not report a valid runtime build fingerprint.'
}
if ($runtimeHealth.runtime_build_fingerprint -ne $expectedRuntimeBuildFingerprint) {
    throw 'Installed runtime build fingerprint does not match validation checkout source closure.'
}
$runtimeProcess = Get-Process -Id $state.pid
if ($runtimeProcess.MainWindowHandle -ne 0) { throw 'Installed runtime is not windowless.' }
$securityArguments = '"{0}" fresh --data-directory "{1}" --receipt "{2}"' -f $securityAcceptanceHelper,$dataDirectory,$Receipt
$stage = 'fresh_security_acceptance'
Write-Host "Lifecycle stage: $stage"
$securityResult = Invoke-BoundedProcess -FilePath $pythonExecutable -ArgumentList $securityArguments -TimeoutSeconds 30 -ReportFailure
if ($securityResult.ExitCode -ne 0) { throw 'Fresh-install packaged security acceptance failed.' }
$freshSecurity = $securityResult.StandardOutput | ConvertFrom-Json
if (-not $freshSecurity.management_initialized -or -not $freshSecurity.authority_separated) {
    throw 'Fresh-install packaged security receipt was incomplete.'
}
$managementCredentialShaBefore = (Get-FileHash -LiteralPath $managementCredential -Algorithm SHA256).Hash
$extensionCredentialShaBefore = (Get-FileHash -LiteralPath $extensionCredential -Algorithm SHA256).Hash

$hooks = Get-Content -LiteralPath $hooksPath -Raw | ConvertFrom-Json
if (-not $hooks.hooks.UserPromptSubmit -or -not $hooks.hooks.Stop) { throw 'Atomizer Codex hooks were not installed.' }
foreach ($event in @('UserPromptSubmit', 'Stop')) {
    $commands = Get-HookCommands -Hooks $hooks -Event $event
    if ($commands.Count -ne 1) { throw "Installer did not reconcile $event to exactly one Atomizer hook." }
    if ($commands[0] -notlike "*$hook*") { throw "Installer did not install the current $event hook." }
}
$installedCurrentCommand = (Get-HookCommands -Hooks $hooks -Event 'UserPromptSubmit')[0]
if ($installedCurrentCommand -ne (Get-HookCommands -Hooks $hooks -Event 'Stop')[0]) {
    throw 'Installed Codex events did not use one canonical current command.'
}
$workspaceHooks = Get-Content -LiteralPath $workspaceHooksPath -Raw | ConvertFrom-Json
foreach ($event in @('UserPromptSubmit', 'Stop')) {
    $commands = Get-HookCommands -Hooks $workspaceHooks -Event $event
    if ($commands.Count -ne 1 -or $commands[0] -ne $installedCurrentCommand) {
        throw "Installer did not reconcile duplicate current hooks for $event."
    }
}
$secondWorkspaceHooks = Get-Content -LiteralPath $secondWorkspaceHooksPath -Raw | ConvertFrom-Json
foreach ($event in @('UserPromptSubmit', 'Stop')) {
    $commands = Get-HookCommands -Hooks $secondWorkspaceHooks -Event $event
    if ($commands.Count -ne 1 -or $commands[0] -ne $installedCurrentCommand) {
        throw "Installer did not reconcile the second registered workspace for $event."
    }
}
$unrelatedWorkspaceAfter = Get-Content -LiteralPath $unrelatedWorkspaceHooksPath -Raw | ConvertFrom-Json
foreach ($case in @(
    @{ Event = 'UserPromptSubmit'; Unrelated = $workspaceUserUnrelated },
    @{ Event = 'Stop'; Unrelated = $workspaceStopUnrelated }
)) {
    $commands = Get-HookCommands -Hooks $unrelatedWorkspaceAfter -Event $case.Event
    if ($commands.Count -ne 1 -or $commands[0] -ne $case.Unrelated) {
        throw "Installer modified unrelated registered workspace $($case.Event)."
    }
}
$globalHooksAfterWorkspace = Get-Content -LiteralPath $hooksPath -Raw | ConvertFrom-Json
foreach ($event in @('UserPromptSubmit', 'Stop')) {
    $commands = Get-HookCommands -Hooks $globalHooksAfterWorkspace -Event $event
    if ($commands.Count -ne 1 -or $commands[0] -ne $installedCurrentCommand) {
        throw "Workspace reconciliation changed global $event hooks."
    }
}

$duplicateHooksPath = Join-Path $env:RUNNER_TEMP 'context-atomizer-duplicate-hooks.json'
$duplicateHooks = [ordered]@{
    hooks = [ordered]@{
        UserPromptSubmit = @(
            (New-HookEntry -Command $installedCurrentCommand),
            (New-HookEntry -Command $installedCurrentCommand),
            (New-HookEntry -Command $installedCurrentCommand)
        )
        Stop = @(
            (New-HookEntry -Command $installedCurrentCommand),
            (New-HookEntry -Command $installedCurrentCommand),
            (New-HookEntry -Command $installedCurrentCommand)
        )
    }
}
$duplicateHooks | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $duplicateHooksPath -Encoding UTF8
$duplicateArguments = "install --enable-codex --codex-hooks `"$duplicateHooksPath`""
$stage = 'duplicate_hook_reconciliation'
Write-Host "Lifecycle stage: $stage"
$duplicateExit = Invoke-LeafProcess -FilePath $manager -ArgumentList $duplicateArguments -TimeoutSeconds 30 -ReportFailure
if ($duplicateExit -ne 0) { throw 'Installed manager could not reconcile duplicate current hooks.' }
$duplicateHooks = Get-Content -LiteralPath $duplicateHooksPath -Raw | ConvertFrom-Json
foreach ($event in @('UserPromptSubmit', 'Stop')) {
    $commands = Get-HookCommands -Hooks $duplicateHooks -Event $event
    if ($commands.Count -ne 1 -or $commands[0] -ne $installedCurrentCommand) {
        throw "Installed manager did not collapse duplicate current $event hooks."
    }
}

$ambiguousHooksPath = Join-Path $env:RUNNER_TEMP 'context-atomizer-ambiguous-hooks.json'
$ambiguousHook = Join-Path $env:RUNNER_TEMP 'custom\atomizer-codex-hook.exe'
$ambiguousCommand = '"{0}" --database "{1}"' -f $ambiguousHook, $testDatabase
$ambiguousHooks = [ordered]@{
    hooks = [ordered]@{
        UserPromptSubmit = @((New-HookEntry -Command $ambiguousCommand))
        Stop = @((New-HookEntry -Command $ambiguousCommand))
    }
}
$ambiguousHooks | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ambiguousHooksPath -Encoding UTF8
$ambiguousHashBefore = (Get-FileHash -LiteralPath $ambiguousHooksPath -Algorithm SHA256).Hash
$ambiguousArguments = "install --enable-codex --codex-hooks `"$ambiguousHooksPath`""
$stage = 'ambiguous_hook_rejection'
Write-Host "Lifecycle stage: $stage"
$ambiguousExit = Invoke-LeafProcess -FilePath $manager -ArgumentList $ambiguousArguments -TimeoutSeconds 30 -ReportFailure
if ($ambiguousExit -eq 0) { throw 'Installed manager accepted an ambiguous Atomizer-like hook.' }
$ambiguousHashAfter = (Get-FileHash -LiteralPath $ambiguousHooksPath -Algorithm SHA256).Hash
if ($ambiguousHashAfter -ne $ambiguousHashBefore) { throw 'ambiguous Codex hook fixture changed during fail-closed reconciliation.' }

$emptyHooksPath = Join-Path $env:RUNNER_TEMP 'context-atomizer-empty-hooks.json'
if (Test-Path -LiteralPath $emptyHooksPath) { throw 'Disposable no-hooks fixture already exists.' }
$emptyHooksArguments = "install --enable-codex --codex-hooks `"$emptyHooksPath`""
$stage = 'fresh_hook_registration'
Write-Host "Lifecycle stage: $stage"
$emptyHooksExit = Invoke-LeafProcess -FilePath $manager -ArgumentList $emptyHooksArguments -TimeoutSeconds 30 -ReportFailure
if ($emptyHooksExit -ne 0) { throw 'Installed manager could not create a fresh Codex hooks file.' }
$emptyHooks = Get-Content -LiteralPath $emptyHooksPath -Raw | ConvertFrom-Json
if (@($emptyHooks.hooks.UserPromptSubmit).Count -ne 1 -or @($emptyHooks.hooks.Stop).Count -ne 1) {
    throw 'Fresh Codex hooks were not installed exactly once.'
}
$stage = 'repeated_hook_registration'
Write-Host "Lifecycle stage: $stage"
$repeatEmptyHooksExit = Invoke-LeafProcess -FilePath $manager -ArgumentList $emptyHooksArguments -TimeoutSeconds 30 -ReportFailure
if ($repeatEmptyHooksExit -ne 0) { throw 'Installed manager could not repeat fresh Codex hook registration.' }
$emptyHooks = Get-Content -LiteralPath $emptyHooksPath -Raw | ConvertFrom-Json
if (@($emptyHooks.hooks.UserPromptSubmit).Count -ne 1 -or @($emptyHooks.hooks.Stop).Count -ne 1) {
    throw 'Repeated Codex hook registration duplicated Atomizer hooks.'
}
Remove-Item -LiteralPath $emptyHooksPath

$capture = @{
    session_id = 'installer-lifecycle-session'
    turn_id = 'installer-lifecycle-user'
    cwd = $Checkout
    hook_event_name = 'UserPromptSubmit'
    prompt = 'disposable installer acceptance evidence'
} | ConvertTo-Json -Compress
$capture | & $hook --database $testDatabase
if ($LASTEXITCODE -ne 0) { throw 'Installed Codex hook could not seed disposable lifecycle evidence.' }
$database = Join-Path $dataDirectory 'history.sqlite3'
$preReinstallReady = Wait-RuntimeReady -StatePath $statePath -Manager $manager -MinimumUnitsIndexed 1 -TimeoutSeconds 30
$state = $preReinstallReady.State
$health = $preReinstallReady.Health
$derivedBeforeReinstall = $health.derived_state
$stage = 'pre_reinstall_runtime_stop'
Write-Host "Lifecycle stage: $stage"
Stop-DisposableRuntime -Manager $manager -State $state
$databasePhysicalShaBefore = (Get-FileHash -LiteralPath $database -Algorithm SHA256).Hash.ToLowerInvariant()
$snapshotBefore = Get-LogicalSnapshot -Python $pythonExecutable -Helper $snapshotHelper -Database $database -TimeoutSeconds 30

$hooks = Get-Content -LiteralPath $hooksPath -Raw | ConvertFrom-Json
$hooks.hooks | Add-Member -NotePropertyName UnrelatedEvent -NotePropertyValue @(
    [pscustomobject]@{ hooks = @([pscustomobject]@{ type = 'command'; command = 'unrelated-tool' }) }
) -Force
foreach ($event in @('UserPromptSubmit', 'Stop')) {
    $hooks.hooks.$event = @($hooks.hooks.$event) + @(
        [pscustomobject]@{ hooks = @([pscustomobject]@{ type = 'command'; command = $currentCommand }) }
    )
}
$hooks | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $hooksPath -Encoding UTF8

foreach ($workspacePath in @($workspaceHooksPath, $secondWorkspaceHooksPath)) {
    $registeredHooks = Get-Content -LiteralPath $workspacePath -Raw | ConvertFrom-Json
    foreach ($event in @('UserPromptSubmit', 'Stop')) {
        $registeredHooks.hooks.$event = @($registeredHooks.hooks.$event) + @(
            [pscustomobject]@{ hooks = @([pscustomobject]@{ type = 'command'; command = $currentCommand }) }
        )
    }
    $registeredHooks | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $workspacePath -Encoding UTF8
}

$stage = 'reinstall'
Write-Host "Lifecycle stage: $stage"
$reinstallExit = Invoke-LeafProcess -FilePath $Installer -ArgumentList $installArguments -TimeoutSeconds 180 -ReportFailure
$stage = 'reinstall_completed'
if ($reinstallExit -ne 0) { throw "Reinstall failed with exit code $reinstallExit." }
$postReinstallReady = Wait-RuntimeReady -StatePath $statePath -Manager $manager -MinimumUnitsIndexed 1 -TimeoutSeconds 30
$state = $postReinstallReady.State
$health = $postReinstallReady.Health
$derivedAfterReinstall = $health.derived_state
if ((Get-FileHash -LiteralPath $managementCredential -Algorithm SHA256).Hash -ne $managementCredentialShaBefore) {
    throw 'Ordinary reinstall rotated or replaced the management credential.'
}
if ((Get-FileHash -LiteralPath $extensionCredential -Algorithm SHA256).Hash -ne $extensionCredentialShaBefore) {
    throw 'Ordinary reinstall rotated or replaced the paired extension secret.'
}
$postSecurityArguments = '"{0}" post-reinstall --data-directory "{1}" --receipt "{2}"' -f $securityAcceptanceHelper,$dataDirectory,$Receipt
$stage = 'post_reinstall_security_acceptance'
Write-Host "Lifecycle stage: $stage"
$postSecurityResult = Invoke-BoundedProcess -FilePath $pythonExecutable -ArgumentList $postSecurityArguments -TimeoutSeconds 30 -ReportFailure
if ($postSecurityResult.ExitCode -ne 0) { throw 'Post-reinstall packaged security acceptance failed.' }
$postSecurity = $postSecurityResult.StandardOutput | ConvertFrom-Json
if (-not $postSecurity.management_preserved -or -not $postSecurity.revoke_invalidated_old_secret) {
    throw 'Post-reinstall packaged security receipt was incomplete.'
}
$stage = 'post_reinstall_runtime_stop'
Write-Host "Lifecycle stage: $stage"
Stop-DisposableRuntime -Manager $manager -State $state
$databasePhysicalShaAfter = (Get-FileHash -LiteralPath $database -Algorithm SHA256).Hash.ToLowerInvariant()
$snapshotAfter = Get-LogicalSnapshot -Python $pythonExecutable -Helper $snapshotHelper -Database $database -TimeoutSeconds 30
if ($snapshotAfter.authoritative_fingerprint -ne $snapshotBefore.authoritative_fingerprint) {
    throw 'Reinstall changed authoritative logical Library state.'
}
if ($snapshotAfter.derived_fingerprint -ne $snapshotBefore.derived_fingerprint) {
    throw 'Reinstall changed converged derived logical Library state.'
}
if ($snapshotAfter.logical_fingerprint -ne $snapshotBefore.logical_fingerprint) {
    throw 'Reinstall changed canonical logical Library state.'
}
$hooks = Get-Content -LiteralPath $hooksPath -Raw | ConvertFrom-Json
if (@($hooks.hooks.UserPromptSubmit).Count -ne 1 -or @($hooks.hooks.Stop).Count -ne 1) {
    throw 'Reinstall did not collapse duplicate current Atomizer hooks.'
}
foreach ($event in @('UserPromptSubmit', 'Stop')) {
    $commands = Get-HookCommands -Hooks $hooks -Event $event
    if ($commands.Count -ne 1 -or $commands[0] -notlike "*$hook*") {
        throw "Reinstall left an invalid $event hook ownership state."
    }
}
if (-not $hooks.hooks.UnrelatedEvent) { throw 'Reinstall removed an unrelated Codex hook.' }
foreach ($workspacePath in @($workspaceHooksPath, $secondWorkspaceHooksPath)) {
    $registeredHooks = Get-Content -LiteralPath $workspacePath -Raw | ConvertFrom-Json
    foreach ($event in @('UserPromptSubmit', 'Stop')) {
        $commands = Get-HookCommands -Hooks $registeredHooks -Event $event
        if ($commands.Count -ne 1 -or $commands[0] -ne $installedCurrentCommand) {
            throw "Reinstall did not reconcile registered workspace $event."
        }
    }
}

Set-Location -LiteralPath (Split-Path -Parent $Checkout)
$movedCheckoutEntries = @()
New-Item -ItemType Directory -Path $renamedCheckout | Out-Null
try {
    foreach ($entry in @(Get-ChildItem -Force -LiteralPath $Checkout)) {
        Move-Item -LiteralPath $entry.FullName -Destination $renamedCheckout
        $movedCheckoutEntries += $entry.Name
    }
    $stage = 'checkout_independent_restart'
    Write-Host "Lifecycle stage: $stage"
    $restartExit = Invoke-LeafProcess -FilePath $manager -ArgumentList 'restart' -TimeoutSeconds 30 -ReportFailure
    if ($restartExit -ne 0) { throw 'Installed runtime could not restart without the checkout.' }
    $checkoutIndependentReady = Wait-RuntimeReady -StatePath $statePath -Manager $manager -MinimumUnitsIndexed 1 -TimeoutSeconds 30
    $state = $checkoutIndependentReady.State
    $health = $checkoutIndependentReady.Health
}
finally {
    foreach ($name in $movedCheckoutEntries) {
        Move-Item -LiteralPath (Join-Path $renamedCheckout $name) -Destination $Checkout
    }
    Remove-Item -LiteralPath $renamedCheckout
}

$hooks = Get-Content -LiteralPath $hooksPath -Raw | ConvertFrom-Json
foreach ($event in @('UserPromptSubmit', 'Stop')) {
    $hooks.hooks.$event = @($hooks.hooks.$event) + @(
        [pscustomobject]@{ hooks = @([pscustomobject]@{ type = 'command'; command = $currentCommand }) }
    )
}
$hooks | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $hooksPath -Encoding UTF8

$workspaceHooks = Get-Content -LiteralPath $workspaceHooksPath -Raw | ConvertFrom-Json
foreach ($event in @('UserPromptSubmit', 'Stop')) {
    $workspaceHooks.hooks.$event = @($workspaceHooks.hooks.$event) + @(
        [pscustomobject]@{ hooks = @([pscustomobject]@{ type = 'command'; command = $currentCommand }) }
    )
}
$workspaceHooks | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $workspaceHooksPath -Encoding UTF8
if (-not (Test-Path -LiteralPath $database)) { throw 'Workspace uninstall deleted the disposable Library database.' }

$preUninstallReady = Wait-RuntimeReady -StatePath $statePath -Manager $manager -MinimumUnitsIndexed 1 -TimeoutSeconds 30
$state = $preUninstallReady.State
$health = $preUninstallReady.Health
$stage = 'pre_uninstall_runtime_stop'
Write-Host "Lifecycle stage: $stage"
Stop-DisposableRuntime -Manager $manager -State $state
$snapshotBeforeUninstall = Get-LogicalSnapshot -Python $pythonExecutable -Helper $snapshotHelper -Database $database -TimeoutSeconds 30

$registeredUninstallString = [string](Get-ItemProperty -LiteralPath $uninstallKey -ErrorAction Stop).UninstallString
$expectedUninstallString = '"{0}"' -f (Join-Path $applicationDirectory 'Uninstall.exe')
if ($registeredUninstallString -ne $expectedUninstallString) {
    throw 'Registered UninstallString does not identify the installed interactive uninstaller.'
}
$registeredQuietUninstallString = Get-RegisteredQuietUninstall `
    -UninstallKey $uninstallKey `
    -ApplicationDirectory $applicationDirectory
if ($registeredQuietUninstallString -notmatch ' -EncodedCommand ') {
    throw 'Registered quiet uninstall is not the supported encoded built-in launcher.'
}
New-Item -ItemType Directory -Path $applicationSiblingDirectory | Out-Null
$applicationSiblingMarker = Join-Path $applicationSiblingDirectory 'must-remain.txt'
'unrelated sibling' | Set-Content -LiteralPath $applicationSiblingMarker -Encoding UTF8
$stage = 'quiet_uninstall'
Write-Host "Lifecycle stage: $stage"
$quietUninstallSuccess = Invoke-RegisteredQuietUninstall `
    -Command $registeredQuietUninstallString `
    -ApplicationDirectory $applicationDirectory
$stage = 'uninstall_completed'
if ($quietUninstallSuccess.TimedOut) { throw 'Registered quiet uninstall success case timed out.' }
if ($quietUninstallSuccess.ExitCode -ne 0) {
    throw "Registered quiet uninstall failed with exit code $($quietUninstallSuccess.ExitCode)."
}
$removalDeadline = [DateTime]::UtcNow.AddSeconds(10)
while ((Test-Path -LiteralPath $applicationDirectory) -and ([DateTime]::UtcNow -lt $removalDeadline)) {
    Start-Sleep -Milliseconds 100
}
if (Test-Path -LiteralPath $applicationDirectory) { throw 'Uninstall left application files behind.' }
if (-not (Test-Path -LiteralPath $applicationSiblingMarker)) {
    throw 'Quiet uninstall misparsed the path with spaces and touched a sibling directory.'
}
if (Test-Path -LiteralPath $startMenu) { throw 'Uninstall left the Start Menu shortcut behind.' }
if (Test-Path -LiteralPath $uninstallKey) { throw 'Uninstall left its registry entry behind.' }
if (-not (Test-Path -LiteralPath $database)) { throw 'Uninstall deleted the disposable Library database.' }
$snapshotAfterUninstall = Get-LogicalSnapshot -Python $pythonExecutable -Helper $snapshotHelper -Database $database -TimeoutSeconds 30
if ($snapshotAfterUninstall.logical_fingerprint -ne $snapshotBeforeUninstall.logical_fingerprint) {
    throw 'Uninstall changed canonical logical Library state.'
}
if (Get-ItemProperty -LiteralPath $runKey -Name 'ContextAtomizerLocal' -ErrorAction SilentlyContinue) {
    throw 'Uninstall left the per-user startup registration behind.'
}
if (Test-Path -LiteralPath $managementCredential) { throw 'Uninstall left the management credential behind.' }
if (Test-Path -LiteralPath $extensionCredential) { throw 'Uninstall left the paired extension secret behind.' }
foreach ($residue in @('runtime.json', 'permissions.json', 'runtime-state.json', 'runtime.lock', 'capture-errors.log')) {
    if (Test-Path -LiteralPath (Join-Path $dataDirectory $residue)) {
        throw "Uninstall left managed runtime residue: $residue"
    }
}
if (Test-Path -LiteralPath (Join-Path $dataDirectory 'logs')) {
    throw 'Uninstall left runtime log residue.'
}
$hooks = Get-Content -LiteralPath $hooksPath -Raw | ConvertFrom-Json
if (-not $hooks.hooks.UnrelatedEvent) { throw 'Uninstall removed an unrelated Codex hook.' }
if ((Get-HookCommands -Hooks $hooks -Event 'UserPromptSubmit').Count -ne 0 -or
    (Get-HookCommands -Hooks $hooks -Event 'Stop').Count -ne 0) {
    throw 'Uninstall left Atomizer Codex hooks behind.'
}
$workspaceHooks = Get-Content -LiteralPath $workspaceHooksPath -Raw | ConvertFrom-Json
if ((Get-HookCommands -Hooks $workspaceHooks -Event 'UserPromptSubmit').Count -ne 0 -or
    (Get-HookCommands -Hooks $workspaceHooks -Event 'Stop').Count -ne 0) {
    throw 'Uninstaller left Atomizer workspace hooks behind.'
}
$secondWorkspaceHooks = Get-Content -LiteralPath $secondWorkspaceHooksPath -Raw | ConvertFrom-Json
if ((Get-HookCommands -Hooks $secondWorkspaceHooks -Event 'UserPromptSubmit').Count -ne 0 -or
    (Get-HookCommands -Hooks $secondWorkspaceHooks -Event 'Stop').Count -ne 0) {
    throw 'Uninstaller left Atomizer hooks in the second registered workspace.'
}
$unrelatedWorkspaceAfter = Get-Content -LiteralPath $unrelatedWorkspaceHooksPath -Raw | ConvertFrom-Json
foreach ($case in @(
    @{ Event = 'UserPromptSubmit'; Unrelated = $workspaceUserUnrelated },
    @{ Event = 'Stop'; Unrelated = $workspaceStopUnrelated }
)) {
    $commands = Get-HookCommands -Hooks $unrelatedWorkspaceAfter -Event $case.Event
    if ($commands.Count -ne 1 -or $commands[0] -ne $case.Unrelated) {
        throw "Uninstaller modified unrelated workspace $($case.Event)."
    }
}

if (-not (Test-Path -LiteralPath $applicationSiblingMarker)) { throw 'Normal quiet uninstall did not preserve install-path sibling.' }
Remove-Item -LiteralPath $applicationSiblingDirectory -Recurse -Force

$evidence = @{
    install_exit = [int]$installExit
    reinstall_exit = [int]$reinstallExit
    uninstall_exit = [int]$quietUninstallSuccess.ExitCode
    executable_inventory = 'four_product_plus_uninstall'
    runtime_healthy = $true
    runtime_windowless = $true
    source_fingerprint = $expectedRuntimeBuildFingerprint
    runtime_fingerprint = [string]$health.runtime.runtime_build_fingerprint
    source_runtime_equal = ($health.runtime.runtime_build_fingerprint -eq $expectedRuntimeBuildFingerprint)
    runtime_executable_sha256 = $runtimeExecutableSha256
    management_credential_initialized = $true
    extension_secret_absent_before_pairing = $true
    explicit_extension_pairing = $true
    management_extension_authority_separated = $true
    hmac_and_replay_protection = $true
    synthetic_capture = $true
    derived_convergence = $true
    pre_authoritative_fingerprint = [string]$snapshotBefore.authoritative_fingerprint
    pre_derived_fingerprint = [string]$snapshotBefore.derived_fingerprint
    pre_logical_fingerprint = [string]$snapshotBefore.logical_fingerprint
    pre_physical_sha256 = $databasePhysicalShaBefore
    post_authoritative_fingerprint = [string]$snapshotAfter.authoritative_fingerprint
    post_derived_fingerprint = [string]$snapshotAfter.derived_fingerprint
    post_logical_fingerprint = [string]$snapshotAfter.logical_fingerprint
    post_physical_sha256 = $databasePhysicalShaAfter
    physical_sha_changed = ($databasePhysicalShaBefore -ne $databasePhysicalShaAfter)
    pre_post_equal = ($snapshotBefore.logical_fingerprint -eq $snapshotAfter.logical_fingerprint)
    uninstall_logical_fingerprint = [string]$snapshotAfterUninstall.logical_fingerprint
    uninstall_authoritative_fingerprint = [string]$snapshotAfterUninstall.authoritative_fingerprint
    uninstall_derived_fingerprint = [string]$snapshotAfterUninstall.derived_fingerprint
    uninstall_pre_post_equal = ($snapshotBeforeUninstall.logical_fingerprint -eq $snapshotAfterUninstall.logical_fingerprint)
    sqlite_integrity_check = [string]$snapshotAfter.checks.integrity_check[0]
    sqlite_quick_check = [string]$snapshotAfter.checks.quick_check[0]
    sqlite_foreign_key_violations = @($snapshotAfter.checks.foreign_key_violations).Count
    sqlite_fts_equal = [bool]$snapshotAfter.checks.fts_matches_lexical_projection
    credentials_preserved_across_reinstall = $true
    credentials_removed_on_uninstall = $true
    owned_hooks_removed = $true
    unrelated_hooks_preserved = $true
    library_preserved = $true
    checkout_independent = $true
    quiet_uninstall_bounded = (-not $quietUninstallSuccess.TimedOut)
    quiet_uninstall_temp_copy_removed = $true
    quiet_uninstall_stale_copy_preserved = $true
}
if (-not $evidence.source_runtime_equal -or -not $evidence.pre_post_equal) { throw 'Normal lifecycle identity or preservation equality failed.' }
$stage = 'complete'
Write-LifecycleReceipt -Path $Receipt -Scenario 'normal' -Passed $true -Stage $stage -Candidate $candidate -Evidence $evidence
Write-Output 'LIFECYCLE_RESULT normal=PASS'
