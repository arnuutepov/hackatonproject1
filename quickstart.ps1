param([string]$EnvironmentPath = '.venv', [string]$PythonPath,
      [string]$RulesDirectory, [ValidateRange(1024,65535)][int]$Port = 8001,
      [switch]$Offline, [switch]$SetupOnly)
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    Write-Host 'OGUZKI SUPPORT COPILOT - quick start' -ForegroundColor Cyan
    $taskEnvRoot = if ([IO.Path]::IsPathRooted($EnvironmentPath)) { $EnvironmentPath } else { Join-Path $PSScriptRoot $EnvironmentPath }
    $taskPython = Join-Path $taskEnvRoot 'Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $taskPython)) {
        Write-Host 'Installing the locked dependencies. Internet and Python 3.11+ are required.'
        $taskInstall = @{Install=$true; Mode='AIStatus'; EnvironmentPath=$taskEnvRoot}
        if ($PythonPath) { $taskInstall.PythonPath = $PythonPath }
        & "$PSScriptRoot/run.ps1" @taskInstall
    }
    $taskData = Join-Path $PSScriptRoot 'data/handout-2026-10-27'
    if ($RulesDirectory -or -not (Test-Path -LiteralPath "$taskData/rules.json") -or -not (Test-Path -LiteralPath "$taskData/taxonomy.json")) {
        if (-not $RulesDirectory) {
            Write-Host 'Select the organizer handout folder containing rules.json and taxonomy.json.'
            $RulesDirectory = (Read-Host 'Folder path (without quotes)').Trim().Trim('"')
        }
        $taskSource = (Resolve-Path -LiteralPath $RulesDirectory).Path
        foreach ($taskFile in @('rules.json','taxonomy.json')) {
            if (-not (Test-Path -LiteralPath (Join-Path $taskSource $taskFile) -PathType Leaf)) {
                throw "Missing $taskFile in the selected folder. Extract the organizer archive first."
            }
        }
        New-Item -ItemType Directory -Force -Path $taskData | Out-Null
        foreach ($taskFile in @('rules.json','taxonomy.json')) {
            $taskFrom = Join-Path $taskSource $taskFile
            $taskTo = Join-Path $taskData $taskFile
            if ([IO.Path]::GetFullPath($taskFrom) -ne [IO.Path]::GetFullPath($taskTo)) {
                if (Test-Path -LiteralPath $taskTo) {
                    if ((Get-FileHash -LiteralPath $taskFrom).Hash -ne (Get-FileHash -LiteralPath $taskTo).Hash) {
                        throw "An existing $taskFile differs. Review it before replacing the local dictionary."
                    }
                } else { Copy-Item -LiteralPath $taskFrom -Destination $taskTo }
            }
        }
    }
    & $taskPython -c "from support_copilot.knowledge import get_knowledge; k=get_knowledge(); print('Knowledge ready:',len(k.rules),'rules;',len(k.categories),'categories')"
    if ($LASTEXITCODE) { throw 'Invalid rules/taxonomy. See the error above.' }
    if ($SetupOnly) { Write-Host 'Setup complete.' -ForegroundColor Green; return }
    if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
        throw "Port $Port is busy. Stop the previous server with Ctrl+C or use -Port 8002."
    }
    $taskUsers = @(& $taskPython -m support_copilot.manage_operators list)
    if ($LASTEXITCODE) { throw 'Cannot read local operator accounts.' }
    if ($taskUsers.Count -eq 0) {
        Write-Host 'Create your operator login. Password: at least 15 characters, entered twice.'
        Write-Host 'Password input is invisible: type or paste it and press Enter.' -ForegroundColor Yellow
        $taskUsername = Read-Host 'Login (Enter = admin)'
        if (-not $taskUsername) { $taskUsername = 'admin' }
        & $taskPython -m support_copilot.manage_operators create --username $taskUsername --role admin
        if ($LASTEXITCODE) { throw 'Account was not created. Run quickstart again.' }
    } else {
        Write-Host 'Existing local operator accounts (use the password you set):'
        $taskUsers | ForEach-Object { Write-Host $_ }
    }
    Write-Host "Open http://127.0.0.1:$Port/ after 'Application startup complete'." -ForegroundColor Green
    Write-Host 'Keep this window open. Ctrl+C stops the server.'
    $taskMode = if ($Offline) { '2' } else { Read-Host '1 = Gemini with your key; 2 = offline templates (Enter = 2)' }
    if ($taskMode -eq '1') {
        Write-Host 'Enter your Google Gemini key at the next hidden prompt. It is not saved to a file.'
        & "$PSScriptRoot/start-model.ps1" -EnvironmentPath $taskEnvRoot -Port $Port
    } else {
        $taskPrevious = @{}
        foreach ($taskName in @('COPILOT_DRAFT_PROVIDER','COPILOT_INSECURE_LOCAL_HTTP','PYTHONUTF8')) {
            $taskPrevious[$taskName] = [Environment]::GetEnvironmentVariable($taskName,'Process')
        }
        try {
            $env:COPILOT_DRAFT_PROVIDER = 'template'
            $env:COPILOT_INSECURE_LOCAL_HTTP = '1'
            $env:PYTHONUTF8 = '1'
            & $taskPython -m uvicorn support_copilot.api:app --host 127.0.0.1 --port $Port --no-proxy-headers
        } finally {
            foreach ($taskName in $taskPrevious.Keys) { [Environment]::SetEnvironmentVariable($taskName,$taskPrevious[$taskName],'Process') }
        }
    }
} finally { Pop-Location }
