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
    Write-LifecycleReceipt -Path $Receipt -Scenario 'ambiguous' -Passed $false -Stage $stage -Candidate $candidate -Evidence $evidence -FailureClass $_.Exception.GetType().Name
    throw
}
$candidate = Read-CandidateMetadata -Metadata $Metadata -Installer $Installer

$applicationDirectory = Join-Path $env:LOCALAPPDATA 'Programs\Context Atomizer Ambiguous Acceptance'
$applicationSiblingDirectory = "$applicationDirectory-sibling"
$dataDirectory = Join-Path $env:LOCALAPPDATA 'ContextAtomizer'
$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Context Atomizer Local\Context Atomizer Library.lnk'
$hooksPath = Join-Path $env:USERPROFILE '.codex\hooks.json'
$codexConfigPath = Join-Path $env:USERPROFILE '.codex\config.toml'
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\ContextAtomizerLocal'
$snapshotHelper = Join-Path $Checkout 'release\sqlite_logical_snapshot.py'
$diagnosticHelper = Join-Path $Checkout 'release\codex_workspace_diagnostic.py'
$pythonExecutable = (Get-Command python -ErrorAction Stop).Source
if ((Test-Path -LiteralPath $applicationDirectory) -or (Test-Path -LiteralPath $applicationSiblingDirectory) -or (Test-Path -LiteralPath $dataDirectory)) {
    throw 'Disposable ambiguous runner is not clean.'
}

New-Item -ItemType Directory -Path (Split-Path -Parent $hooksPath) -Force | Out-Null
[ordered]@{ hooks = [ordered]@{} } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $hooksPath -Encoding UTF8
$workspacePaths = @{}
foreach ($label in @('workspace_one','workspace_two','unrelated')) {
    $path = Join-Path $env:RUNNER_TEMP "context-atomizer-$label\.codex\hooks.json"
    New-Item -ItemType Directory -Path (Split-Path -Parent $path) -Force | Out-Null
    [ordered]@{ hooks = [ordered]@{} } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $path -Encoding UTF8
    $workspacePaths[$label] = $path
}
$unrelatedUser = 'workspace-unrelated-user'
$unrelatedStop = 'workspace-unrelated-stop'
[ordered]@{ hooks = [ordered]@{
    UserPromptSubmit = @((New-HookEntry -Command $unrelatedUser))
    Stop = @((New-HookEntry -Command $unrelatedStop))
} } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $workspacePaths.unrelated -Encoding UTF8

$registrations = @(
    "$($workspacePaths.workspace_one):user_prompt_submit:0:0",
    "$($workspacePaths.workspace_one):stop:0:0",
    "$($workspacePaths.workspace_two):user_prompt_submit:0:0",
    "$($workspacePaths.workspace_two):stop:0:0",
    "$($workspacePaths.unrelated):user_prompt_submit:0:0",
    "$($workspacePaths.unrelated):stop:0:0"
)
@($registrations | ForEach-Object { "[hooks.state.'$_']`ntrusted_hash = `"sha256:ambiguous-acceptance`"`n" }) -join "`n" |
    Set-Content -LiteralPath $codexConfigPath -Encoding UTF8

$stage = 'install'
$installExit = Invoke-LeafProcess -FilePath $Installer -ArgumentList ('/S /CODEX=1 /D={0}' -f $applicationDirectory) -TimeoutSeconds 180 -ReportFailure
$evidence.install_exit = [int]$installExit
if ($installExit -ne 0) { throw "Ambiguous scenario install failed with exit code $installExit." }
$manager = Join-Path $applicationDirectory 'atomizer-local-manager.exe'
$hook = Join-Path $applicationDirectory 'atomizer-codex-hook.exe'
$database = Join-Path $dataDirectory 'history.sqlite3'
$statePath = Join-Path $dataDirectory 'runtime-state.json'
$ready = Wait-RuntimeReady -StatePath $statePath -Manager $manager -TimeoutSeconds 30
$installedHooks = Get-Content -LiteralPath $hooksPath -Raw | ConvertFrom-Json
$currentCommand = (Get-HookCommands -Hooks $installedHooks -Event 'UserPromptSubmit')[0]
if ((Get-HookCommands -Hooks $installedHooks -Event 'UserPromptSubmit').Count -ne 1 -or
    (Get-HookCommands -Hooks $installedHooks -Event 'Stop').Count -ne 1 -or
    $currentCommand -cne (Get-HookCommands -Hooks $installedHooks -Event 'Stop')[0]) {
    throw 'Fresh ambiguous scenario did not establish one canonical current command.'
}

$capture = @{
    session_id = 'ambiguous-lifecycle-session'; turn_id = 'ambiguous-lifecycle-user';
    cwd = $Checkout; hook_event_name = 'UserPromptSubmit'; prompt = 'disposable ambiguous lifecycle evidence'
} | ConvertTo-Json -Compress
$capture | & $hook --database $database
if ($LASTEXITCODE -ne 0) { throw 'Ambiguous scenario installed hook could not seed evidence.' }
$ready = Wait-RuntimeReady -StatePath $statePath -Manager $manager -MinimumUnitsIndexed 1 -TimeoutSeconds 30

$stage = 'workspace_diagnostic'
$encodedCommand = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($currentCommand))
$diagnosticArguments = '"{0}" --config "{1}" --global-hooks "{2}" --current-command-base64 {3} --target workspace_one "{4}" --target workspace_two "{5}" --target unrelated "{6}"' -f `
    $diagnosticHelper,$codexConfigPath,$hooksPath,$encodedCommand,$workspacePaths.workspace_one,$workspacePaths.workspace_two,$workspacePaths.unrelated
$configBytesBefore = [IO.File]::ReadAllBytes($codexConfigPath)
$configShaBefore = (Get-FileHash -LiteralPath $codexConfigPath -Algorithm SHA256).Hash
$diagnosticResult = Invoke-BoundedProcess -FilePath $pythonExecutable -ArgumentList $diagnosticArguments -TimeoutSeconds 30 -ReportFailure
if ($diagnosticResult.ExitCode -ne 0) { throw 'Workspace ownership diagnostic failed.' }
$diagnostic = $diagnosticResult.StandardOutput | ConvertFrom-Json
$evidence.workspace_discovery_count = [int]$diagnostic.discovered_count
if ($diagnostic.discovered_count -ne 3 -or @($diagnostic.targets).Count -ne 3) { throw 'Workspace diagnostic did not discover exactly three labeled targets.' }
foreach ($case in @(
    @{ Label = 'workspace_one'; Class = 'CURRENT_ATOMIZER' },
    @{ Label = 'workspace_two'; Class = 'CURRENT_ATOMIZER' },
    @{ Label = 'unrelated'; Class = 'UNRELATED' }
)) {
    $target = @($diagnostic.targets | Where-Object label -eq $case.Label)
    $evidence["$($case.Label)_discovered"] = ($target.Count -eq 1)
    if ($target.Count -ne 1) { throw "Workspace diagnostic label count was incorrect for $($case.Label)." }
    foreach ($event in @('UserPromptSubmit','Stop')) {
        $eventResult = $target[0].events.$event
        $eventLabel = if ($event -eq 'UserPromptSubmit') { 'user' } else { 'stop' }
        $evidence["$($case.Label)_${eventLabel}_count"] = [int]$eventResult.count
        $evidence["$($case.Label)_${eventLabel}_class"] = if (@($eventResult.classes).Count -eq 1) { [string]$eventResult.classes[0] } else { 'NON_UNIQUE' }
        if ($eventResult.count -ne 1 -or @($eventResult.classes).Count -ne 1 -or $eventResult.classes[0] -ne $case.Class) {
            throw "Workspace diagnostic $event ownership was incorrect for $($case.Label)."
        }
    }
}
$configBytesAfterDiagnostic = [IO.File]::ReadAllBytes($codexConfigPath)
if ([Convert]::ToBase64String($configBytesAfterDiagnostic) -cne [Convert]::ToBase64String($configBytesBefore) -or
    (Get-FileHash -LiteralPath $codexConfigPath -Algorithm SHA256).Hash -ne $configShaBefore) {
    throw 'Read-only workspace diagnostic changed Codex configuration bytes.'
}

Stop-DisposableRuntime -Manager $manager -State $ready.State
$snapshotBefore = Get-LogicalSnapshot -Python $pythonExecutable -Helper $snapshotHelper -Database $database -TimeoutSeconds 30
$unrelatedBytesBefore = [IO.File]::ReadAllBytes($workspacePaths.unrelated)
$unrelatedShaBefore = (Get-FileHash -LiteralPath $workspacePaths.unrelated -Algorithm SHA256).Hash
$ambiguousHook = Join-Path $env:RUNNER_TEMP 'custom\atomizer-codex-hook.exe'
$ambiguousCommand = '"{0}" --database "{1}"' -f $ambiguousHook,$database
[ordered]@{ hooks = [ordered]@{
    UserPromptSubmit = @((New-HookEntry -Command $ambiguousCommand))
    Stop = @((New-HookEntry -Command $ambiguousCommand))
    UnrelatedEvent = @((New-HookEntry -Command 'unrelated-tool'))
} } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $hooksPath -Encoding UTF8
$globalBytesBefore = [IO.File]::ReadAllBytes($hooksPath)
$globalShaBefore = (Get-FileHash -LiteralPath $hooksPath -Algorithm SHA256).Hash
$evidence.ambiguous_global_length = [int64]$globalBytesBefore.Length
$evidence.ambiguous_global_sha256 = $globalShaBefore.ToLowerInvariant()
$evidence.unrelated_workspace_length = [int64]$unrelatedBytesBefore.Length
$evidence.unrelated_workspace_sha256 = $unrelatedShaBefore.ToLowerInvariant()

New-Item -ItemType Directory -Path $applicationSiblingDirectory | Out-Null
$siblingMarker = Join-Path $applicationSiblingDirectory 'must-remain.txt'
'unrelated sibling' | Set-Content -LiteralPath $siblingMarker -Encoding UTF8
$stage = 'quiet_uninstall'
$quietCommand = Get-RegisteredQuietUninstall -UninstallKey $uninstallKey -ApplicationDirectory $applicationDirectory
$uninstall = Invoke-RegisteredQuietUninstall -Command $quietCommand -ApplicationDirectory $applicationDirectory
$evidence.uninstall_exit = [int]$uninstall.ExitCode
$evidence.quiet_uninstall_timed_out = [bool]$uninstall.TimedOut
if ($uninstall.TimedOut -or $uninstall.ExitCode -ne 2) { throw "Ambiguous quiet uninstall did not return bounded exit 2; exit=$($uninstall.ExitCode)." }
if ((Test-Path -LiteralPath $applicationDirectory) -or (Test-Path -LiteralPath $startMenu) -or (Test-Path -LiteralPath $uninstallKey)) {
    throw 'Ambiguous quiet uninstall left product files, shortcut, or registration.'
}
if (Get-ItemProperty -LiteralPath $runKey -Name 'ContextAtomizerLocal' -ErrorAction SilentlyContinue) { throw 'Ambiguous quiet uninstall left startup registration.' }
foreach ($residue in @('management-credential.bin','extension-pairing.bin','runtime.json','permissions.json','runtime-state.json','runtime.lock','capture-errors.log','logs')) {
    if (Test-Path -LiteralPath (Join-Path $dataDirectory $residue)) { throw "Ambiguous quiet uninstall left managed residue: $residue" }
}
if (-not (Test-Path -LiteralPath $siblingMarker)) { throw 'Ambiguous quiet uninstall changed install-path sibling state.' }
$globalBytesAfter = [IO.File]::ReadAllBytes($hooksPath)
if ([Convert]::ToBase64String($globalBytesAfter) -cne [Convert]::ToBase64String($globalBytesBefore) -or
    (Get-FileHash -LiteralPath $hooksPath -Algorithm SHA256).Hash -ne $globalShaBefore) {
    throw 'Ambiguous global hook bytes were not preserved exactly.'
}
$configBytesAfter = [IO.File]::ReadAllBytes($codexConfigPath)
if ([Convert]::ToBase64String($configBytesAfter) -cne [Convert]::ToBase64String($configBytesBefore) -or
    (Get-FileHash -LiteralPath $codexConfigPath -Algorithm SHA256).Hash -ne $configShaBefore) {
    throw 'Ambiguous quiet uninstall changed Codex config bytes.'
}
foreach ($label in @('workspace_one','workspace_two')) {
    $workspaceHooks = Get-Content -LiteralPath $workspacePaths[$label] -Raw | ConvertFrom-Json
    foreach ($event in @('UserPromptSubmit','Stop')) {
        if ((Get-HookCommands -Hooks $workspaceHooks -Event $event).Count -ne 0) { throw "Owned workspace hook remained for $label/$event." }
    }
}
$unrelatedBytesAfter = [IO.File]::ReadAllBytes($workspacePaths.unrelated)
if ([Convert]::ToBase64String($unrelatedBytesAfter) -cne [Convert]::ToBase64String($unrelatedBytesBefore) -or
    (Get-FileHash -LiteralPath $workspacePaths.unrelated -Algorithm SHA256).Hash -ne $unrelatedShaBefore) {
    throw 'Unrelated workspace hook bytes were not preserved exactly.'
}
$snapshotAfter = Get-LogicalSnapshot -Python $pythonExecutable -Helper $snapshotHelper -Database $database -TimeoutSeconds 30
if ($snapshotAfter.logical_fingerprint -ne $snapshotBefore.logical_fingerprint) { throw 'Ambiguous uninstall changed logical Library state.' }

$evidence += @{
    diagnostic_config_bytes_preserved = $true; diagnostic_config_sha256_preserved = $true
    ambiguous_global_bytes_preserved = $true; ambiguous_global_sha256_preserved = $true
    workspace_owned_hooks_removed = $true; unrelated_workspace_bytes_preserved = $true
    runtime_cleanup = $true; log_cleanup = $true; credential_cleanup = $true
    library_preserved = $true; pre_logical_fingerprint = [string]$snapshotBefore.logical_fingerprint
    pre_authoritative_fingerprint = [string]$snapshotBefore.authoritative_fingerprint
    pre_derived_fingerprint = [string]$snapshotBefore.derived_fingerprint
    post_logical_fingerprint = [string]$snapshotAfter.logical_fingerprint; pre_post_equal = $true
    post_authoritative_fingerprint = [string]$snapshotAfter.authoritative_fingerprint
    post_derived_fingerprint = [string]$snapshotAfter.derived_fingerprint
    sqlite_integrity_check = [string]$snapshotAfter.checks.integrity_check[0]
    sqlite_quick_check = [string]$snapshotAfter.checks.quick_check[0]
    sqlite_foreign_key_violations = @($snapshotAfter.checks.foreign_key_violations).Count
    sqlite_fts_equal = [bool]$snapshotAfter.checks.fts_matches_lexical_projection
    quiet_uninstall_bounded = (-not $uninstall.TimedOut); quiet_uninstall_temp_copy_removed = $true
    quiet_uninstall_stale_copy_preserved = $true; install_path_with_spaces = $true; sibling_preserved = $true
}
Remove-Item -LiteralPath $applicationSiblingDirectory -Recurse -Force
$stage = 'complete'
Write-LifecycleReceipt -Path $Receipt -Scenario 'ambiguous' -Passed $true -Stage $stage -Candidate $candidate -Evidence $evidence
Write-Output 'LIFECYCLE_RESULT ambiguous=PASS'
