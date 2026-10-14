# Isolated installer smoke test; does not register tasks or contact business APIs.
$ErrorActionPreference = 'Stop'
$root = Join-Path ([IO.Path]::GetTempPath()) ('Wechat DiDi Test ' + [guid]::NewGuid().ToString('N'))
function Run-Installer {
    param([string]$Action)
    & "$PSScriptRoot\windows.cmd" $Action -InstallRoot $root
    if ($LASTEXITCODE -ne 0) { throw "Installer action failed: $Action" }
}
Run-Installer Install
$config = Join-Path $root 'config\.env'
$marker = Join-Path $root 'data\sentinel.txt'
Add-Content -LiteralPath $config -Value '# installer-test-preserve' -Encoding UTF8
Set-Content -LiteralPath $marker -Value 'preserve-test-data' -Encoding UTF8
$before = (Get-FileHash -LiteralPath $config).Hash
$lockFile = Join-Path $root 'data\runner.lock'
$lock = [IO.File]::Open($lockFile, 'OpenOrCreate', 'ReadWrite', 'None')
try {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$PSScriptRoot\windows.ps1" Install -InstallRoot $root
    if ($LASTEXITCODE -eq 0) { throw 'Installer must reject updates while runtime is locked.' }
} finally { $lock.Dispose() }
Run-Installer Install
if ((Get-FileHash -LiteralPath $config).Hash -ne $before) { throw 'Configuration was overwritten.' }
if (!(Test-Path -LiteralPath $marker)) { throw 'Runtime data was lost.' }
if (Test-Path -LiteralPath (Join-Path $root 'app\.env')) { throw 'Configured env must not be copied into app.' }
$acl = Get-Acl -LiteralPath $root
if (!$acl.AreAccessRulesProtected) { throw 'Runtime root must not inherit public permissions.' }
$allowed = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18', 'S-1-5-32-544')
foreach ($rule in $acl.Access) {
    $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
    if ($sid -notin $allowed) { throw 'Unexpected ACL principal.' }
}
Run-Installer MemoryCheck
Write-Host 'Windows install, update, lock protection, private ACL and official memory checks passed.'
# Keep temporary files for CI diagnostics; the runner clears its temp directory.
