# Сборка RPM kmsvnc на WSL-стенде dno-build (контейнер МОС 12, пакет для МОС 12 и 13).
# Результат: dist/kmsvnc. Новый пакет для мастера: скопировать RPM в
# assets/remote и обновить имя, SHA-256 и версию в
# src/workers/setup/installers/kmsvnc.py.
[CmdletBinding()]
param([string]$Distribution = 'dno-build')

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path $PSScriptRoot -Parent
$Out = Join-Path $ProjectRoot 'dist/kmsvnc'
New-Item -ItemType Directory -Force $Out | Out-Null
$Packaging = (& wsl.exe -d $Distribution -- wslpath -a -u ((Join-Path $ProjectRoot 'packaging/kmsvnc').Replace('\', '/'))).Trim()
$LinuxOut = (& wsl.exe -d $Distribution -- wslpath -a -u ($Out.Replace('\', '/'))).Trim()
# Скрипт читается через tr: в Windows-checkout возможны CRLF.
& wsl.exe -d $Distribution -u root -- bash -c "KMSVNC_PACKAGING='$Packaging' bash <(tr -d '\r' < '$Packaging/build.sh') '$LinuxOut'"
if ($LASTEXITCODE -ne 0) { throw "Сборка kmsvnc завершилась с кодом $LASTEXITCODE" }
Get-ChildItem $Out -Filter 'kmsvnc-*.rpm' | ForEach-Object {
    '{0}  {1}' -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower(), $_.Name
}
