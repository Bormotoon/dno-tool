# build_debug_win.ps1 - отладочная сборка dno-tool для Windows.
#
# Отличия от release (build_win.ps1):
#   - имя dno-tool-dbg.exe (не трогает релизный dno-tool.exe)
#   - console=True (видны Qt-fatal, stderr, print)
#   - upx=False (UPX ломает нативные символы в дампах)
#   - без zip - просто dist\dno-tool-dbg.exe
#
# Запуск: .\scripts\build_debug_win.ps1
#
# ВАЖНО: crash-файлы (crash_pydump.log, crash-dumps/) создаются только в
# режиме отладки - переменную ниже ставить ОБЯЗАТЕЛЬНО перед запуском,
# иначе при падении не будет ни Python-стеков, ни minidump.
#
# Символы для расшифровки дампов: официальный PDB с python.org
#   https://www.python.org/ftp/python/3.14.2/python-3.14.2-pdb.zip
# (версия должна совпадать с python314.dll внутри сборки, см. analyze_dump.ps1).

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$ScriptsDir = if ($PSScriptRoot) { $PSScriptRoot } else { $PWD.Path }
$ProjectRoot = Split-Path $ScriptsDir
Set-Location $ProjectRoot

$InitFile = Join-Path $ProjectRoot "src\__init__.py"
if (-not (Test-Path $InitFile)) { Write-Host "Ошибка: src\__init__.py не найден" -ForegroundColor Red; exit 1 }
$VersionLine = Get-Content $InitFile -Encoding UTF8 | Select-String -Pattern '__version__.*=.*"' | Select-Object -First 1
$Version = if ($VersionLine.Line -match '"([^"]+)"') { $Matches[1] } else { "unknown" }
Write-Host "=== Отладочная сборка dno-tool v$Version (Windows, PyInstaller, console) ===" -ForegroundColor Cyan

if (-not (Test-Path ".venv")) { Write-Host "Ошибка: нет .venv" -ForegroundColor Red; exit 1 }
& .\.venv\Scripts\Activate.ps1
& .\.venv\Scripts\python.exe scripts/ensure_qt.py --update
if ($LASTEXITCODE -ne 0) { throw "Ошибка проверки/обновления Qt" }
if (-not (Get-Command pyinstaller.exe -ErrorAction SilentlyContinue)) { pip install pyinstaller -q }

$BuildDir = Join-Path $ProjectRoot "build"
$DistDir = Join-Path $ProjectRoot "dist"
if (Test-Path $BuildDir) { Remove-Item -Recurse -Force $BuildDir }
if (Test-Path (Join-Path $DistDir "dno-tool-dbg")) { Remove-Item -Recurse -Force (Join-Path $DistDir "dno-tool-dbg") }

# Спецификация: копия release-spec с debug-флагами.
$SpecSource = Join-Path $ProjectRoot "dno-tool.spec"
# Спек в корне (не в build\): PyInstaller резолвит '__main__.py'
# относительно каталога спек-файла; из build\ путь ломается.
$SpecDebug = Join-Path $ProjectRoot "dno-tool-dbg.spec"
$specText = Get-Content $SpecSource -Raw
$specText = $specText -replace "name='dno-tool'", "name='dno-tool-dbg'"
$specText = $specText -replace "upx=True", "upx=False"
$specText = $specText -replace "console=False", "console=True"
[System.IO.File]::WriteAllText($SpecDebug, $specText, [System.Text.UTF8Encoding]::new($false))

# 2>&1: PyInstaller пишет INFO-логи в stderr - PS 5.1 при
# ErrorActionPreference=Stop трактует их как terminating errors.
# EAP=Continue на время вызова + вывод в файл (не хост).
New-Item -ItemType Directory -Path $BuildDir -Force | Out-Null
$ErrorActionPreference = "Continue"
& .\.venv\Scripts\pyinstaller.exe --noconfirm --workpath (Join-Path $BuildDir "pyi") --distpath $DistDir $SpecDebug > (Join-Path $BuildDir "pyinstaller.log") 2>&1
$PyInstallerRc = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($PyInstallerRc -ne 0) { Write-Host "Ошибка PyInstaller (см. build\pyinstaller.log)" -ForegroundColor Red; exit 1 }

$Binary = Join-Path $DistDir "dno-tool-dbg.exe"
if (-not (Test-Path $Binary)) { Write-Host "Ошибка: $Binary не создан" -ForegroundColor Red; exit 1 }
Write-Host "Готово: $Binary" -ForegroundColor Green
Write-Host ""
Write-Host "Запуск отладки (env обязателен - без него нет crash-файлов):" -ForegroundColor Cyan
Write-Host '  $env:DNO_TOOL_LOG_LEVEL = "DEBUG"'
Write-Host "  $Binary"
Write-Host ""
Write-Host "После падения:" -ForegroundColor Cyan
Write-Host "  ~\.dno-tool\crash_pydump.log      (Python-стеки всех потоков)"
Write-Host "  ~\.dno-tool\crash-dumps\*.dmp     (нативный minidump)"
