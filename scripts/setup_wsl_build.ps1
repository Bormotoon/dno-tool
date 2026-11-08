[CmdletBinding()]
param(
    [string]$Distribution = 'dno-build',
    [string]$InstallPath = 'D:\WSL\dno-build'
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path $PSScriptRoot -Parent
$StateDir = Join-Path $ProjectRoot '.cache/wsl-build'
New-Item -ItemType Directory -Force $StateDir | Out-Null
$StateFile = Join-Path $StateDir 'setup-state.json'

function Save-State([string]$Stage) {
    @{
        stage = $Stage
        distribution = $Distribution
        install_path = $InstallPath
        updated_at = (Get-Date).ToString('o')
        resume = '.\scripts\setup_wsl_build.ps1; .\scripts\build_linux.ps1'
    } | ConvertTo-Json | Set-Content -Encoding UTF8 $StateFile
}

function Invoke-Wsl([string[]]$Command, [switch]$CaptureOutput) {
    # stderr нативной команды не должен скрывать её код завершения в PowerShell 5.1.
    $ErrorActionPreference = 'Continue'
    $WslOutput = [Collections.Generic.Queue[string]]::new()
    & wsl.exe -d $Distribution -u root --exec @Command 2>&1 | ForEach-Object {
        $Line = $_.ToString()
        $WslOutput.Enqueue($Line)
        if (-not $CaptureOutput) { Write-Host $Line }
        # Полный журнал выводится сразу; для ошибки достаточно последних строк.
        if (-not $CaptureOutput -and $WslOutput.Count -gt 80) { $null = $WslOutput.Dequeue() }
    }
    $WslExitCode = $LASTEXITCODE
    if ($WslExitCode -ne 0) {
        throw "Команда WSL завершилась с кодом ${WslExitCode}:`n$($WslOutput -join [Environment]::NewLine)"
    }
    if ($CaptureOutput) { $WslOutput.ToArray() }
}

# Изменение компонентов Windows требует административного токена.
$ResolvedInstallPath = [IO.Path]::GetFullPath($InstallPath)
$InstallParent = Split-Path -Path $ResolvedInstallPath -Parent
if (-not $InstallParent) {
    throw "Укажите полный путь к подкаталогу для установки WSL: $InstallPath"
}
$SwapPath = (Join-Path $InstallParent 'dno-build-swap.vhdx').Replace('\', '\\')
$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$Identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $LogFile = Join-Path $StateDir ('setup-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '.log')
    # EncodedCommand сохраняет пробелы, Unicode и кавычки в передаваемых параметрах.
    $ScriptLiteral = $PSCommandPath.Replace("'", "''")
    $DistributionLiteral = $Distribution.Replace("'", "''")
    $InstallLiteral = $InstallPath.Replace("'", "''")
    $LogLiteral = $LogFile.Replace("'", "''")
    $ElevatedCommand = @"
`$ErrorActionPreference = 'Stop'
Start-Transcript -LiteralPath '$LogLiteral' | Out-Null
try {
    & '$ScriptLiteral' -Distribution '$DistributionLiteral' -InstallPath '$InstallLiteral'
    `$SetupExitCode = `$LASTEXITCODE
    if (`$null -eq `$SetupExitCode) { `$SetupExitCode = 0 }
} catch {
    Write-Output (`$_ | Out-String)
    `$SetupExitCode = 1
} finally {
    Stop-Transcript | Out-Null
}
exit `$SetupExitCode
"@
    $EncodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($ElevatedCommand))
    $PowerShellExe = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
    Write-Host "Подтвердите запрос UAC для установки WSL. Журнал: $LogFile"
    try {
        $SetupProcess = Start-Process -FilePath $PowerShellExe -Verb RunAs -WindowStyle Hidden `
            -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $EncodedCommand) -PassThru -Wait
    } catch {
        throw "Не удалось получить права администратора: $($_.Exception.Message)"
    }
    if (Test-Path -LiteralPath $LogFile) { Get-Content -LiteralPath $LogFile -Encoding UTF8 }
    if ($SetupProcess.ExitCode -eq 3010) { exit 3010 }
    if ($SetupProcess.ExitCode -ne 0) {
        throw "Установка завершилась с кодом $($SetupProcess.ExitCode). Журнал: $LogFile"
    }
    exit 0
}
New-Item -ItemType Directory -Force $InstallPath | Out-Null
$ConfigPath = Join-Path $env:USERPROFILE '.wslconfig'
$Config = if (Test-Path $ConfigPath) { [IO.File]::ReadAllText($ConfigPath) } else { '' }
if ($Config) {
    Copy-Item -LiteralPath $ConfigPath -Destination (Join-Path $StateDir ('wslconfig-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.bak'))
}
# Остальные секции и настройки пользователя сохраняются.
$Values = [ordered]@{
    memory         = '12GB'
    processors     = '8'
    swap           = '8GB'
    swapFile       = $SwapPath
    defaultVhdSize = '80GB'
}
if ($Config -notmatch '(?im)^\[wsl2\]\s*$') { $Config += "`n[wsl2]`n" }
$Config = [regex]::Replace($Config, '(?ims)(^\[wsl2\]\s*\r?\n)(.*?)(?=^\[|\z)', {
    param($Match)
    $Body = $Match.Groups[2].Value
    foreach ($Key in $Values.Keys) {
        $Line = "$Key=$($Values[$Key])"
        if ($Body -match "(?im)^$Key\s*=") {
            $Body = [regex]::Replace($Body, "(?im)^$Key\s*=.*$", [System.Text.RegularExpressions.MatchEvaluator]{ param($Ignored) $Line })
        } else { $Body = $Body.TrimEnd() + "`n$Line`n" }
    }
    return "[wsl2]`n" + $Body.TrimEnd() + "`n"
})
[IO.File]::WriteAllText($ConfigPath, $Config, [Text.UTF8Encoding]::new($false))
Save-State 'enabling-windows-features'
$NeedsReboot = $false
foreach ($Feature in @('Microsoft-Windows-Subsystem-Linux', 'VirtualMachinePlatform')) {
    $Current = Get-WindowsOptionalFeature -Online -FeatureName $Feature
    if ($Current.State -eq 'EnablePending') { $NeedsReboot = $true }
    elseif ($Current.State -ne 'Enabled') {
        $Result = Enable-WindowsOptionalFeature -Online -FeatureName $Feature -All -NoRestart
        if ($Result.RestartNeeded) { $NeedsReboot = $true }
    }
}
if ($NeedsReboot) {
    Save-State 'reboot-required'
    Write-Host "Нужна перезагрузка Windows. Состояние: $StateFile"
    Write-Host 'После перезагрузки повторите setup_wsl_build.ps1, затем build_linux.ps1.'
    exit 3010
}
Save-State 'installing-wsl'
& wsl.exe --version | Out-Null
if ($LASTEXITCODE -ne 0) {
    & wsl.exe --install --no-distribution --web-download
    if ($LASTEXITCODE -eq 3010) { Save-State 'reboot-required'; exit 3010 }
    if ($LASTEXITCODE -ne 0) { Save-State 'wsl-install-failed'; throw "Установка WSL: код $LASTEXITCODE" }
}
& wsl.exe --set-default-version 2
if ($LASTEXITCODE -ne 0) { Save-State 'wsl-not-ready'; throw "WSL 2 ещё недоступен: код $LASTEXITCODE. Проверьте необходимость перезагрузки." }
$Installed = (& wsl.exe --list --quiet | Out-String).Replace([string][char]0, '')
if ($LASTEXITCODE -ne 0) { Save-State 'wsl-not-ready'; throw "Не удалось получить список дистрибутивов WSL: код $LASTEXITCODE`n$Installed" }
if ($Installed -notmatch "(?m)^$([regex]::Escape($Distribution))\s*$") {
    Save-State 'installing-ubuntu'
    & wsl.exe --install Ubuntu-24.04 --name $Distribution --location $InstallPath --no-launch --web-download
    if ($LASTEXITCODE -eq 3010) { Save-State 'reboot-required'; exit 3010 }
    if ($LASTEXITCODE -ne 0) { throw "Установка Ubuntu: код $LASTEXITCODE" }
}
$BootstrapFile = Join-Path $StateDir 'bootstrap-wsl.sh'
$Bootstrap = [IO.File]::ReadAllText((Join-Path $ProjectRoot 'packaging/linux/bootstrap-wsl.sh')).Replace("`r`n", "`n")
[IO.File]::WriteAllText($BootstrapFile, $Bootstrap, [Text.UTF8Encoding]::new($false))
Save-State 'preparing-ubuntu'
try {
    $LinuxBootstrap = (Invoke-Wsl -Command @('wslpath', '-a', '-u', $BootstrapFile.Replace('\', '/')) -CaptureOutput | Out-String).Trim()
    Invoke-Wsl -Command @('bash', $LinuxBootstrap)
} catch {
    Save-State 'ubuntu-bootstrap-failed'
    throw
}
Save-State 'ready'
Write-Host 'Стенд готов к запуску .\scripts\build_linux.ps1.'
