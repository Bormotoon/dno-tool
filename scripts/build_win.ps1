# build_win.ps1 — сборка dno-tool для Windows через PyInstaller.
#
# Запуск из корня проекта: .\scripts\build_win.ps1 [-MasterKey <файл .pub>]
#
# Артефакт в dist/:
#   - dno-tool-<ver>-windows.zip         (PyInstaller onefile + bundled Qt6/DLL)
#   - dno-tool-<ver>-windows-master.zip  (с -MasterKey: внутри открытая часть
#     служебного мастер-ключа нод из указанного файла, в сборке — master-key.pub)
#
# Требования:
#   - .venv с зависимостями из requirements.txt
#   - PyInstaller (pip install pyinstaller)

[CmdletBinding()]
param(
    # Файл открытой части служебного мастер-ключа нод; без него механизм выключен.
    [string]$MasterKey = ""
)
# Путь к ключу задаётся относительно каталога запуска, до смены каталога.
if ($MasterKey) {
    if (-not (Test-Path -LiteralPath $MasterKey -PathType Leaf)) {
        Write-Host "Ошибка: нет файла открытой части мастер-ключа: $MasterKey" -ForegroundColor Red; exit 1
    }
    $MasterKey = (Resolve-Path -LiteralPath $MasterKey).Path
}

$ErrorActionPreference = "Stop"

$ScriptsDir = if ($PSScriptRoot) { $PSScriptRoot } else { $PWD.Path }
$ProjectRoot = Split-Path $ScriptsDir
Set-Location $ProjectRoot

# ── Версия из src/__init__.py ──────────────────────────────────────────────
$InitFile = Join-Path $ProjectRoot "src\__init__.py"
if (-not (Test-Path $InitFile)) { Write-Host "Ошибка: src\__init__.py не найден" -ForegroundColor Red; exit 1 }
$VersionLine = Get-Content $InitFile -Encoding UTF8 | Select-String -Pattern '__version__.*=.*"' | Select-Object -First 1
$Version = if ($VersionLine.Line -match '"([^"]+)"') { $Matches[1] } elseif ($VersionLine.Line -match "'([^']+)'") { $Matches[1] } else { ($VersionLine.Line -split '=')[1].Trim().Trim('"', "'") }
if (-not $Version) { Write-Host "Ошибка: не удалось определить версию" -ForegroundColor Red; exit 1 }
Write-Host "=== Сборка dno-tool v$Version (Windows, PyInstaller) ===" -ForegroundColor Cyan
$MasterSuffix = if ($MasterKey) { "-master" } else { "" }
$MasterLabel = if ($MasterKey) { "со служебным мастер-ключом" } else { "без служебного мастер-ключа" }
Write-Host "Вариант: $MasterLabel" -ForegroundColor Cyan
if ($MasterKey) { Write-Host "Ключ: $MasterKey" -ForegroundColor Cyan }

# ── Виртуальное окружение и зависимости ────────────────────────────────────
if (-not (Test-Path ".venv")) {
    Write-Host "Создание виртуального окружения..."
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { Write-Host "Ошибка создания .venv" -ForegroundColor Red; exit 1 }
}
& .\.venv\Scripts\Activate.ps1
# Файл ключа проверяется до долгой установки зависимостей.
$env:DNO_MASTER_KEY = $MasterKey
& .\.venv\Scripts\python.exe packaging/master_key.py validate
if ($LASTEXITCODE -ne 0) { Remove-Item Env:DNO_MASTER_KEY -ErrorAction SilentlyContinue; exit 1 }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip -q
if ($LASTEXITCODE -ne 0) { throw "Ошибка обновления pip" }
& .\.venv\Scripts\python.exe scripts/ensure_qt.py --update
if ($LASTEXITCODE -ne 0) { throw "Ошибка проверки/обновления Qt" }
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt -q
if ($LASTEXITCODE -ne 0) { throw "Ошибка установки requirements.txt" }
& .\.venv\Scripts\python.exe -m pip install pyinstaller -q
if ($LASTEXITCODE -ne 0) { Write-Host "Ошибка установки зависимостей" -ForegroundColor Red; exit 1 }

# Общий комплект устанавливается после pip/Qt и до загрузки мультимедиа.
& .\.venv\Scripts\python.exe packaging/media/windows.py
if ($LASTEXITCODE -ne 0) { throw "Ошибка подготовки медиабиблиотек" }
& .\.venv\Scripts\python.exe packaging/media/bundle.py manifest
if ($LASTEXITCODE -ne 0) { throw "Ошибка манифеста медиабиблиотек" }

# ── Сборка через PyInstaller ────────────────────────────────────────────────
Write-Host "`n--- Preflight: ассеты иконок ---"
# Frozen-сборка едет без папок тем: рантайм читает zip на лету
# (src/config/icon_theme_loader.py). Отсутствующий zip = немые пустые
# иконки в бинаре, поэтому проверяем ДО pyinstaller и падаем сразу.
$IconsDir = Join-Path $ProjectRoot "assets\icons"
$ThemesJson = Join-Path $IconsDir "themes.json"
if (-not (Test-Path $ThemesJson)) { Write-Host "Ошибка: assets\icons\themes.json не найден" -ForegroundColor Red; exit 1 }
$ThemeIds = @()
try {
    Get-Content -LiteralPath $ThemesJson -Raw -Encoding UTF8 | ConvertFrom-Json | ForEach-Object { $ThemeIds += $_.id }
} catch { Write-Host "Ошибка: themes.json не читается: $_" -ForegroundColor Red; exit 1 }
if (-not $ThemeIds) { Write-Host "Ошибка: themes.json пуст" -ForegroundColor Red; exit 1 }
$Missing = @()
foreach ($tid in $ThemeIds) {
    if (-not (Test-Path (Join-Path $IconsDir "$tid.zip"))) { $Missing += "assets/icons/$tid.zip" }
}
foreach ($pk in @("assets\custom\mono.zip", "assets\custom\color.zip")) {
    if (-not (Test-Path (Join-Path $ProjectRoot $pk))) { $Missing += $pk }
}
if ($Missing) {
    Write-Host "Ошибка: нет zip-тем (собери их gen_icon_themes.py / fetch_custom_icons.py):" -ForegroundColor Red
    $Missing | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    exit 1
}
Write-Host "Темы и паки на месте ($($ThemeIds.Count) тем)." -ForegroundColor Green

Write-Host "`n--- PyInstaller ---"
$BuildDir = Join-Path $ProjectRoot "build"
$DistDir = Join-Path $ProjectRoot "dist"
$ReportDir = Join-Path $ProjectRoot '.cache/build-reports/windows'
New-Item -ItemType Directory -Force $ReportDir | Out-Null
foreach ($TargetPath in @($BuildDir, (Join-Path $DistDir 'dno-tool'))) {
    $AbsoluteTarget = [IO.Path]::GetFullPath($TargetPath)
    if (-not $AbsoluteTarget.StartsWith([IO.Path]::GetFullPath($ProjectRoot) + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Каталог очистки выходит за пределы проекта: $AbsoluteTarget"
    }
}
$tmpDir = $null
try {
    if (Test-Path $BuildDir) { Remove-Item -Recurse -Force $BuildDir }
    if (Test-Path (Join-Path $DistDir "dno-tool")) { Remove-Item -Recurse -Force (Join-Path $DistDir "dno-tool") }

    $ErrorActionPreference = 'Continue'
    $env:DNO_MASTER_KEY = $MasterKey
    & .\.venv\Scripts\pyinstaller.exe --noconfirm --workpath $BuildDir --distpath $DistDir (Join-Path $ProjectRoot "dno-tool.spec")
    $ErrorActionPreference = 'Stop'
    if ($LASTEXITCODE -ne 0) { Write-Host "Ошибка PyInstaller" -ForegroundColor Red; exit 1 }

    $Binary = Join-Path $DistDir "dno-tool.exe"
    if (-not (Test-Path $Binary)) {
        $Binary = Get-ChildItem -Path $DistDir -Filter "dno-tool.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
    }
    if (-not $Binary -or -not (Test-Path $Binary)) {
        Write-Host "Ошибка: dno-tool.exe не найден в dist/" -ForegroundColor Red; exit 1
    }
    Write-Host "Бинарь: $Binary" -ForegroundColor Green
    & .\.venv\Scripts\python.exe packaging/media/verify.py audit $Binary --report (Join-Path $ReportDir 'media-package-report-windows.json')
    if ($LASTEXITCODE -ne 0) { throw "Ошибка аудита медиабиблиотек в пакете" }
    & .\.venv\Scripts\python.exe packaging/master_key.py check $Binary
    if ($LASTEXITCODE -ne 0) { throw "Вариант мастер-ключа в exe не совпадает с запрошенным" }

    # ── Упаковка в zip ──────────────────────────────────────────────────────────
    # dno-tool.spec собирает onefile (single .exe со всеми bundled файлами внутри).
    $ZipPath = Join-Path $DistDir "dno-tool-$Version-windows$MasterSuffix.zip"
    if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }
    Write-Host "Упаковка в $ZipPath ..."
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $tmpDir = Join-Path $env:TEMP "dno-tool-win-$(Get-Random)"
    New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null
    Copy-Item $Binary $tmpDir
    [System.IO.Compression.ZipFile]::CreateFromDirectory($tmpDir, $ZipPath)
    $AbsoluteTemporary = [IO.Path]::GetFullPath($tmpDir)
    if (-not $AbsoluteTemporary.StartsWith([IO.Path]::GetFullPath($env:TEMP) + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Временный каталог выходит за пределы TEMP: $AbsoluteTemporary"
    }
    Remove-Item -Recurse -Force $tmpDir
    & .\.venv\Scripts\python.exe packaging/media/verify.py zip $ZipPath --report (Join-Path $ReportDir 'zip-report-windows.json')
    if ($LASTEXITCODE -ne 0) { throw "Ошибка API-проверки извлечённого ZIP" }
    Write-Host "Артефакт: $ZipPath" -ForegroundColor Green

    Write-Host "`n============================================" -ForegroundColor Green
    Write-Host "  Сборка завершена" -ForegroundColor Green
    Write-Host "============================================" -ForegroundColor Green
    Write-Host "  - $(Split-Path $ZipPath -Leaf) ($MasterLabel)"
    Write-Host ""
} finally {
    Remove-Item Env:DNO_MASTER_KEY -ErrorAction SilentlyContinue
    # Каталог проверен выше; готовые пакеты и отчёты остаются в dist/.cache.
    if (Test-Path -LiteralPath $BuildDir) { Remove-Item -LiteralPath $BuildDir -Recurse -Force }
    if ($tmpDir -and (Test-Path -LiteralPath $tmpDir)) {
        $AbsoluteTemporary = [IO.Path]::GetFullPath($tmpDir)
        if (-not $AbsoluteTemporary.StartsWith([IO.Path]::GetFullPath($env:TEMP) + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Временный каталог выходит за пределы TEMP: $AbsoluteTemporary"
        }
        Remove-Item -LiteralPath $AbsoluteTemporary -Recurse -Force
    }
}
