param([string]$EnvironmentPath = '.venv', [string]$PythonPath, [string]$RulesDirectory)
$ErrorActionPreference = 'Stop'
$taskSetup = @{EnvironmentPath=$EnvironmentPath; SetupOnly=$true}
if ($PythonPath) { $taskSetup.PythonPath = $PythonPath }
if ($RulesDirectory) { $taskSetup.RulesDirectory = $RulesDirectory }
& "$PSScriptRoot/quickstart.ps1" @taskSetup
$taskEnvRoot = if ([IO.Path]::IsPathRooted($EnvironmentPath)) { $EnvironmentPath } else { Join-Path $PSScriptRoot $EnvironmentPath }
$taskPython = Join-Path $taskEnvRoot 'Scripts/python.exe'
Push-Location $PSScriptRoot
try {
    & $taskPython -m support_copilot.project_metrics --no-dev --http --output outputs/jury-check
    if ($LASTEXITCODE) { throw 'Checks failed. See outputs/jury-check/metrics.json.' }
    Write-Host 'PASS: HTTP/CLI, drafts and safety. No external AI calls.' -ForegroundColor Green
    Write-Host 'Report: outputs/jury-check/summary.md'
} finally { Pop-Location }
