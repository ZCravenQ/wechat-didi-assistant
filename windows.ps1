param(
    [Parameter(Position=0)]
    [ValidateSet('Install','Run','Login','UI','Console','Tools','MemoryCheck','EnableAutostart','DisableAutostart')]
    [string]$Action = 'Run',
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'WechatDidiAssistant'),
    [switch]$Background
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed (exit $LASTEXITCODE): $Program" }
}

try {
    $root = [IO.Path]::GetFullPath($InstallRoot)
    if ($root -eq [IO.Path]::GetPathRoot($root) -or $root -match '["\r\n]') {
        throw 'Choose a dedicated installation directory.'
    }
    if ((Test-Path -LiteralPath $root) -and
        ((Get-Item -LiteralPath $root).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'The installation directory must not be a link or junction.'
    }
    $app = Join-Path $root 'app'
    $state = Join-Path $root 'data'
    $config = Join-Path $root 'config\.env'
    $marker = Join-Path $root 'installation.json'
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $sid = $identity.User.Value
    $taskName = 'WechatDidiAssistant-' + $sid

    if ($Action -eq 'Install') {
        if ([IO.Path]::GetFullPath($PSScriptRoot) -eq $app) {
            throw 'Install/update from a separate downloaded source folder.'
        }
        if ((Test-Path -LiteralPath $root) -and !(Test-Path -LiteralPath $marker) -and
            @(Get-ChildItem -LiteralPath $root -Force).Count -gt 0) {
            throw 'The target directory is not empty and was not created by this installer.'
        }
        $lockPath = Join-Path $state 'runner.lock'
        if (Test-Path -LiteralPath $lockPath) {
            try {
                $probe = [IO.File]::Open($lockPath, 'Open', 'ReadWrite', 'None')
                $probe.Dispose()
            } catch { throw 'Stop the running assistant before updating.' }
        }
        $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($pythonCommand) {
            $python = & $pythonCommand.Source -c 'import sys; assert sys.version_info >= (3,11); print(sys.executable)'
        } else {
            $launcher = Get-Command py.exe -ErrorAction Stop
            $python = & $launcher.Source -3 -c 'import sys; assert sys.version_info >= (3,11); print(sys.executable)'
        }
        if ($LASTEXITCODE -ne 0 -or !$python) { throw 'Install Python 3.11+ and reopen Command Prompt.' }
        $node = (Get-Command node.exe -ErrorAction Stop).Source
        $npm = (Get-Command npm.cmd -ErrorAction Stop).Source
        $nodeVersion = & $node --version
        if ($LASTEXITCODE -ne 0 -or ([version]($nodeVersion -replace '^v','')).Major -lt 22) {
            throw "Install Node.js 22+ and reopen Command Prompt. Detected: $nodeVersion"
        }
        $files = @('agent.py','main.py','wechat.py','memory_mcp.py','memory_bridge.mjs',
                   'place_search.py','admin_ui.py','admin.html','package.json','package-lock.json',
                   'windows.cmd','windows.ps1')
        foreach ($file in ($files + '.env.example')) {
            if (!(Test-Path -LiteralPath (Join-Path $PSScriptRoot $file) -PathType Leaf)) {
                throw "Source file missing: $file"
            }
        }
        [IO.Directory]::CreateDirectory($root) | Out-Null
        # Only change permissions inside this dedicated installation directory.
        Invoke-Checked 'icacls.exe' @($root, '/inheritance:r', '/grant:r',
            "*${sid}:(OI)(CI)F", '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F')
        foreach ($directory in @($app,$state,(Split-Path $config))) {
            [IO.Directory]::CreateDirectory($directory) | Out-Null
        }
        @{python=[string]$python; node_directory=(Split-Path $node)} |
            ConvertTo-Json | Set-Content -LiteralPath $marker -Encoding UTF8
        foreach ($file in $files) {
            Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file) -Destination (Join-Path $app $file) -Force
        }
        if (!(Test-Path -LiteralPath $config)) {
            Copy-Item -LiteralPath (Join-Path $PSScriptRoot '.env.example') -Destination $config
        }
        Invoke-Checked $npm @('ci','--prefix',$app,'--ignore-scripts','--no-audit','--no-fund')
        Write-Host "Installed in: $root"
        Write-Host "Edit configuration: $config"
        Write-Host 'Then run: windows.cmd MemoryCheck, windows.cmd Login, windows.cmd Run'
        Write-Host 'Existing configuration and data were preserved. No service was started.'
        exit 0
    }

    if (!(Test-Path -LiteralPath $marker)) { throw 'Run windows.cmd Install first.' }
    $installed = Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
    $python = [string]$installed.python
    if (!(Test-Path -LiteralPath $python) -or !(Test-Path -LiteralPath (Join-Path $installed.node_directory 'node.exe'))) {
        throw 'Python or Node.js moved. Run the installer again.'
    }
    $env:PATH = [string]$installed.node_directory + [IO.Path]::PathSeparator + $env:PATH
    $env:APP_ENV_FILE = $config
    $env:APP_RUNTIME_DIR = $state
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:PYTHONUNBUFFERED = '1'

    if ($Action -in @('EnableAutostart','DisableAutostart')) {
        $script = Join-Path $app 'windows.ps1'
        $arguments = '-NoLogo -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' +
            $script + '" -Action Run -Background -InstallRoot "' + $root + '"'
        $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($existing -and (@($existing.Actions).Count -ne 1 -or $existing.Actions[0].Arguments -ne $arguments)) {
            throw 'A different scheduled task already uses this name. No task was modified.'
        }
        if ($Action -eq 'DisableAutostart') {
            if ($existing) {
                Stop-ScheduledTask -TaskName $taskName
                Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
            }
            Write-Host 'Assistant autostart disabled. Configuration and memory were kept.'
        } elseif (!$existing) {
            $taskAction = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument $arguments -WorkingDirectory $app
            $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
            $principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
            $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
            Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
            Write-Host 'Enabled for the next sign-in of this Windows account; no password was stored.'
        } else { Write-Host 'Autostart is already enabled.' }
        exit 0
    }

    $modes = @{Run='wechat'; Login='login'; UI='ui'; Console='console'; Tools='tools'; MemoryCheck='memory-check'}
    if ($Background -and $Action -ne 'Run') { throw 'Background mode is only supported for Run.' }
    Push-Location $app
    try {
        if ($Background) {
            & $python -X utf8 (Join-Path $app 'main.py') $modes[$Action] >> (Join-Path $state 'service.log') 2>&1
        } else {
            & $python -X utf8 (Join-Path $app 'main.py') $modes[$Action]
        }
        $code = $LASTEXITCODE
    } finally { Pop-Location }
    exit $code
} catch {
    Write-Error $_.Exception.Message -ErrorAction Continue
    exit 1
}
