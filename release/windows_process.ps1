function Get-BoundedProcessText {
    param(
        [AllowEmptyString()][string]$Text,
        [ValidateRange(1, 65536)][int]$Limit = 4096
    )

    if ($null -eq $Text) { return '' }
    $trimmed = $Text.Trim()
    if ($trimmed.Length -le $Limit) { return $trimmed }
    return $trimmed.Substring(0, $Limit) + "`n[diagnostic truncated]"
}

function Invoke-BoundedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string]$ArgumentList,
        [Parameter(Mandatory = $true)][ValidateRange(1, 3600)][int]$TimeoutSeconds,
        [switch]$ReportFailure
    )

    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = $ArgumentList
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true

    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    $standardOutput = ''
    $standardError = ''
    $timedOut = $false
    try {
        try {
            $started = $process.Start()
        }
        catch {
            $started = $false
            $standardError = "External process launch failed: $($_.Exception.GetType().Name)"
        }
        if (-not $started) {
            if ($ReportFailure) {
                Write-Host 'Installed lifecycle command failed; bounded stdout follows.'
                Write-Host ''
                Write-Host 'Installed lifecycle command failed; bounded stderr follows.'
                Write-Host $standardError
            }
            return [pscustomobject]@{
                ExitCode = [int]127
                TimedOut = $false
                StandardOutput = ''
                StandardError = $standardError
            }
        }
        $outputTask = $process.StandardOutput.ReadToEndAsync()
        $errorTask = $process.StandardError.ReadToEndAsync()
        $timeoutMilliseconds = [int]([int64]$TimeoutSeconds * 1000)
        if (-not $process.WaitForExit($timeoutMilliseconds)) {
            $timedOut = $true
            try { $process.Kill() } catch { }
            [void]$process.WaitForExit(5000)
        }

        if ($outputTask.Wait(1000)) {
            $standardOutput = [string]$outputTask.Result
        }
        else {
            $standardOutput = '[stdout remained open after leaf process exit]'
        }
        if ($errorTask.Wait(1000)) {
            $standardError = [string]$errorTask.Result
        }
        else {
            $standardError = '[stderr remained open after leaf process exit]'
        }

        $exitCode = if ($timedOut -or -not $process.HasExited) {
            124
        }
        else {
            [int]$process.ExitCode
        }
        if ($timedOut) {
            $timeoutMessage = "Process exceeded the ${TimeoutSeconds}-second leaf timeout."
            $standardError = if ($standardError) {
                "$timeoutMessage`n$standardError"
            }
            else {
                $timeoutMessage
            }
        }
        $standardOutput = Get-BoundedProcessText -Text $standardOutput
        $standardError = Get-BoundedProcessText -Text $standardError
        if ($ReportFailure -and $exitCode -ne 0) {
            Write-Host 'Installed lifecycle command failed; bounded stdout follows.'
            Write-Host $standardOutput
            Write-Host 'Installed lifecycle command failed; bounded stderr follows.'
            Write-Host $standardError
        }
        return [pscustomobject]@{
            ExitCode = [int]$exitCode
            TimedOut = $timedOut
            StandardOutput = $standardOutput
            StandardError = $standardError
        }
    }
    finally {
        $process.Dispose()
    }
}

function Invoke-LeafProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string]$ArgumentList,
        [Parameter(Mandatory = $true)][ValidateRange(1, 3600)][int]$TimeoutSeconds,
        [switch]$ReportFailure
    )

    $result = Invoke-BoundedProcess `
        -FilePath $FilePath `
        -ArgumentList $ArgumentList `
        -TimeoutSeconds $TimeoutSeconds `
        -ReportFailure:$ReportFailure
    return [int]$result.ExitCode
}
