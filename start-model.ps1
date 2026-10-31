param([string]$Model = 'gemini-2.5-flash-lite', [string]$EnvironmentPath = '.venv',
      [ValidateRange(1024,65535)][int]$Port = 8001, [string]$ApiKeyFile, [switch]$CheckAI)
$ErrorActionPreference = 'Stop'
$taskEnvRoot = if ([System.IO.Path]::IsPathRooted($EnvironmentPath)) { $EnvironmentPath } else { Join-Path $PSScriptRoot $EnvironmentPath }
$python = Join-Path $taskEnvRoot 'Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python) -and $EnvironmentPath -eq '.venv') {
    $python = Join-Path $PSScriptRoot '..\..\work\oguzki-venv\Scripts\python.exe'
}
if (-not (Test-Path -LiteralPath $python)) { throw 'Python environment not found; specify -EnvironmentPath.' }
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { throw "Port $Port is already in use." }
$taskNames = @('GEMINI_API_KEY', 'GEMINI_API_KEY_FILE', 'COPILOT_GEMINI_MODEL', 'COPILOT_DRAFT_PROVIDER', 'COPILOT_INSECURE_LOCAL_HTTP', 'PYTHONUTF8')
$taskPrevious = @{}
foreach ($taskName in $taskNames) { $taskPrevious[$taskName] = [Environment]::GetEnvironmentVariable($taskName, 'Process') }
try {
    if ($ApiKeyFile) {
        $env:GEMINI_API_KEY_FILE = (Resolve-Path -LiteralPath $ApiKeyFile).Path
    } elseif (-not $env:GEMINI_API_KEY_FILE -and -not $env:GEMINI_API_KEY) {
        $key = Read-Host 'Google Gemini API key (input is hidden)' -AsSecureString
        $credential = [System.Net.NetworkCredential]::new('', $key)
        $env:GEMINI_API_KEY = $credential.Password
        if ([string]::IsNullOrWhiteSpace($env:GEMINI_API_KEY)) { throw 'API key was not provided.' }
    }
    $env:COPILOT_GEMINI_MODEL = $Model
    $env:COPILOT_DRAFT_PROVIDER = 'gemini'
    $env:COPILOT_INSECURE_LOCAL_HTTP = '1'
    $env:PYTHONUTF8 = '1'
    Push-Location $PSScriptRoot
    try {
        & $python -m support_copilot.ai_runtime
        if ($LASTEXITCODE) { throw 'Invalid AI configuration; inspect the error code above.' }
        if ($CheckAI) { & $python -m support_copilot.ai_runtime --check }
        & $python -m uvicorn support_copilot.api:app --host 127.0.0.1 --port $Port --no-proxy-headers
    }
    finally { Pop-Location }
} finally {
    foreach ($taskName in $taskNames) { [Environment]::SetEnvironmentVariable($taskName, $taskPrevious[$taskName], 'Process') }
    if ($key) { $key.Dispose() }
    $credential = $null
}
