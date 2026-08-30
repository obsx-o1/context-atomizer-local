function Get-RegisteredQuietUninstall {
    param(
        [Parameter(Mandatory = $true)][string]$UninstallKey,
        [Parameter(Mandatory = $true)][string]$ApplicationDirectory
    )

    $registration = Get-ItemProperty -LiteralPath $UninstallKey -ErrorAction Stop
    if ($registration.InstallLocation -ne $ApplicationDirectory) {
        throw 'Registered InstallLocation does not match the actual install directory.'
    }
    $command = [string]$registration.QuietUninstallString
    if ([string]::IsNullOrWhiteSpace($command)) {
        throw 'QuietUninstallString was not registered.'
    }
    if ($command -eq ('"{0}" /S' -f (Join-Path $ApplicationDirectory 'Uninstall.exe'))) {
        throw 'QuietUninstallString still advertises the raw NSIS self-copying command.'
    }
    $powerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $expectedPrefix = '"{0}" -NoProfile -NonInteractive -EncodedCommand ' -f $powerShell
    if (-not $command.StartsWith($expectedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'QuietUninstallString does not use the supported encoded Windows PowerShell launcher.'
    }
    $encoded = $command.Substring($expectedPrefix.Length)
    if ($encoded -notmatch '^[A-Za-z0-9+/]+={0,2}$') {
        throw 'QuietUninstallString does not contain one valid encoded payload.'
    }
    return $command
}

function Invoke-RegisteredQuietUninstall {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [Parameter(Mandatory = $true)][string]$ApplicationDirectory
    )

    $stale = Join-Path $env:TEMP 'Atomizer-Q-stale.exe'
    if (Test-Path -LiteralPath $stale) {
        throw 'Disposable stale quiet-uninstaller fixture already exists.'
    }
    Copy-Item -LiteralPath (Join-Path $ApplicationDirectory 'Uninstall.exe') -Destination $stale
    $staleHash = (Get-FileHash -LiteralPath $stale -Algorithm SHA256).Hash
    try {
        $before = @(
            Get-ChildItem -LiteralPath $env:TEMP -File `
                -Filter 'Atomizer-Q-*.exe' `
                -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty FullName
        )
        $result = Invoke-BoundedProcess `
            -FilePath $env:ComSpec `
            -ArgumentList ('/D /S /C "' + $Command + '"') `
            -TimeoutSeconds 180
        $after = @(
            Get-ChildItem -LiteralPath $env:TEMP -File `
                -Filter 'Atomizer-Q-*.exe' `
                -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty FullName
        )
        $added = @(
            Compare-Object -ReferenceObject $before -DifferenceObject $after |
                Where-Object SideIndicator -eq '=>'
        )
        if ($added.Count -ne 0) {
            throw 'Registered quiet uninstall left its temporary executable behind.'
        }
        if (
            (-not (Test-Path -LiteralPath $stale)) -or
            ((Get-FileHash -LiteralPath $stale -Algorithm SHA256).Hash -ne $staleHash)
        ) {
            throw 'Registered quiet uninstall replaced the pre-existing stale executable fixture.'
        }
        return $result
    }
    finally {
        Remove-Item -LiteralPath $stale -Force -ErrorAction SilentlyContinue
    }
}
