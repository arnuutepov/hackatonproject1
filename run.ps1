param(
    [ValidateSet('Serve', 'Test', 'Evaluate', 'DraftEvaluate', 'RegressionEvaluate', 'Measure', 'Benchmark', 'Predict')][string]$Mode = 'Serve',
    [switch]$Install,
    [string]$PythonPath,
    [string]$EnvironmentPath = '.venv',
    [string]$InputFile,
    [string]$OutputFile
)
$ErrorActionPreference = 'Stop'
$previousTemp, $previousTmp, $previousUtf8 = $env:TEMP, $env:TMP, $env:PYTHONUTF8
Push-Location $PSScriptRoot
try {
    $venvPython = Join-Path $EnvironmentPath 'Scripts/python.exe'
    if ($Install) {
        if (-not $PythonPath) {
            $bundled = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
            if (Test-Path -LiteralPath $bundled) { $PythonPath = $bundled }
            else { $PythonPath = 'python' }
        }
        $installTemp = Join-Path $PSScriptRoot '.install-temp'
        New-Item -ItemType Directory -Force -Path $installTemp | Out-Null
        $env:TEMP = $installTemp
        $env:TMP = $installTemp
        & $PythonPath -m venv --without-pip $EnvironmentPath
        if ($LASTEXITCODE) { throw 'Cannot create Python 3.11+ environment' }
        & $PythonPath -m pip --python $venvPython install --no-cache-dir -r requirements-dev.lock
        if ($LASTEXITCODE) { throw 'Dependency installation failed' }
    }
    if (-not (Test-Path -LiteralPath $venvPython)) {
        throw 'Run ./run.ps1 -Install first (optionally -PythonPath C:/path/python.exe)'
    }
    $env:PYTHONUTF8 = '1'
    switch ($Mode) {
        'Serve' { & $venvPython -m uvicorn support_copilot.api:app --host 127.0.0.1 --port 8000 }
        'Test' { & $venvPython -m pytest -q --basetemp .test-temp }
        'Evaluate' { & $venvPython -m support_copilot.evaluate --output outputs/wave-2/dev-metrics.json }
        'DraftEvaluate' { & $venvPython -m support_copilot.evaluate_drafts --output outputs/wave-3/synthetic-safety.json }
        'RegressionEvaluate' { & $venvPython -m support_copilot.evaluate_regressions --output outputs/wave-4/regressions.json }
        'Measure' { & $venvPython -m support_copilot.measure --output outputs/wave-5 }
        'Benchmark' { & $venvPython -m support_copilot.benchmark --output outputs/wave-5/http-latency.json }
        'Predict' {
            if (-not $InputFile -or -not $OutputFile) { throw 'Specify -InputFile and -OutputFile' }
            & $venvPython -m support_copilot.cli --input $InputFile --output $OutputFile
        }
    }
    if ($LASTEXITCODE) { throw "Command failed: $LASTEXITCODE" }
} finally {
    $env:TEMP, $env:TMP, $env:PYTHONUTF8 = $previousTemp, $previousTmp, $previousUtf8
    Pop-Location
}
