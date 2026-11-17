# Run outside NIS with a standalone Python interpreter. No live MCP calls.
[CmdletBinding()]
param(
    [string]$Python = 'python',
    [string]$ReportDirectory = (Join-Path $PSScriptRoot 'test-reports')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Each invocation owns a new directory, including pytest's temporary data.
# Never point --basetemp at an existing user directory: pytest clears it.
$runName = 'mock-{0}-{1}' -f (Get-Date -Format 'yyyyMMdd-HHmmss'), ([guid]::NewGuid().ToString('N').Substring(0, 8))
$runDirectory = Join-Path ([IO.Path]::GetFullPath($ReportDirectory)) $runName
New-Item -ItemType Directory -Path $runDirectory -ErrorAction Stop | Out-Null
$report = Join-Path $runDirectory 'results.xml'
$consoleLog = Join-Path $runDirectory 'pytest.log'
$temporaryData = Join-Path $runDirectory 'test-data'
$testExitCode = 1

Push-Location -LiteralPath $PSScriptRoot
try {
    & $Python -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11+ required'; import pytest"
    if ($LASTEXITCODE -ne 0) {
        throw "Python 3.11+ and pytest are required. Install pytest with: & '$Python' -m pip install pytest"
    }

    Write-Host "Mock-only suite; no connection to NIS or the live MCP server."
    Write-Host "JUnit report: $report"
    Write-Host "Console log:  $consoleLog"
    Write-Host "Mock artifacts: $temporaryData"

    # Explicit files prevent collecting unrelated integration tests. Never
    # include the test that reads this PC's real saved experiment settings.
    $pytestArguments = @(
        '-m', 'pytest', '-q', '-ra', '-s',
        'test_nis_mcp.py', 'test_capture_mock_http.py',
        '-k', 'not reads_this_pcs_saved_experiments',
        "--junitxml=$report", "--basetemp=$temporaryData"
    )
    & $Python @pytestArguments 2>&1 | Tee-Object -FilePath $consoleLog
    $testExitCode = $LASTEXITCODE
    Write-Host "Mock suite exit code: $testExitCode"
}
finally {
    Pop-Location
}

exit $testExitCode
