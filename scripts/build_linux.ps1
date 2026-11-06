[CmdletBinding()]
param(
    [switch]$UpdateEnvironment,
    [switch]$PrepareEnvironmentOnly,
    [switch]$DetailedOutput,
    [ValidateSet('both', 'mos12', 'mos13')][string]$Target = 'both',
    [string]$Distribution = 'dno-build',
    [ValidateRange(1, 8)][int]$Jobs = 4,
    # Файл открытой части служебного мастер-ключа нод (RPM с суффиксом -master);
    # без него ключ на стенд не передаётся.
    [string]$MasterKey = ""
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path $PSScriptRoot -Parent
$CacheDir = Join-Path $ProjectRoot '.cache/wsl-build'
$RunId = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$Archive = Join-Path $CacheDir "source-$RunId.tar"
$Targets = if ($Target -eq 'both') { @('mos12', 'mos13') } else { @($Target) }
$RunnerFiles = [Collections.Generic.List[string]]::new()
$Utf8 = [Text.UTF8Encoding]::new($false)
$PreviousOutputEncoding = $OutputEncoding
$PreviousConsoleOutputEncoding = [Console]::OutputEncoding
$PreviousConsoleInputEncoding = [Console]::InputEncoding
$BuildLabel = ($Targets -join '/').ToUpperInvariant()
$CurrentStage = $null
$StageTimer = $null
$TargetName = $null
$LinuxRoot = $null
$BuildStreamStarted = $false
$LogFile = Join-Path $CacheDir 'prepare.log'
$LastOutput = [Collections.Generic.Queue[string]]::new()
$StageNames = @{
    sources = 'подготовка исходников'
    environment = 'проверка окружения'
    environment_update = 'обновление окружения'
    application = 'сборка приложения'
    pyinstaller = 'PyInstaller'
    media = 'проверки мультимедиа'
    elf = 'проверка ELF'
    rpm = 'упаковка RPM'
    rpm_verify = 'проверка RPM'
    environment_verify = 'проверка и сохранение окружения'
    cleanup = 'очистка временных файлов и кэшей'
    copy = 'копирование результатов'
}

function Complete-BuildStage {
    if ($script:StageTimer) {
        Write-Host ("[$BuildLabel] Готово: $CurrentStage ({0:N1} с)" -f $StageTimer.Elapsed.TotalSeconds)
        $script:StageTimer = $null
    }
}

function Start-BuildStage([string]$Name) {
    if ($script:StageTimer -and $script:CurrentStage -eq $Name) { return }
    Complete-BuildStage
    $script:CurrentStage = $Name
    $script:StageTimer = [Diagnostics.Stopwatch]::StartNew()
    Write-Host "[$BuildLabel] Этап: $Name"
}

function ConvertTo-NativeArgument([string]$Value) {
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    # Правила Windows argv: удваиваем слеши перед кавычкой и в конце аргумента.
    '"' + (($Value -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
}

function Invoke-Wsl([string[]]$Command, [switch]$CaptureOutput, [switch]$BuildOutput) {
    $Captured = [Collections.Generic.List[string]]::new()
    $script:LastOutput.Clear()
    $Process = [Diagnostics.Process]::new()
    $Started = $false
    try {
        # WSL меняет общий режим консоли даже при перенаправленном stdout.
        # Отдельный процесс без консоли сохраняет нормальные переводы строк хоста.
        $Process.StartInfo.FileName = (Get-Command wsl.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
        $Process.StartInfo.Arguments = ((@('-d', $Distribution, '-u', 'root', '--exec') + $Command | ForEach-Object { ConvertTo-NativeArgument $_ }) -join ' ')
        $Process.StartInfo.UseShellExecute = $false
        $Process.StartInfo.CreateNoWindow = $true
        $Process.StartInfo.RedirectStandardInput = $true
        $Process.StartInfo.RedirectStandardOutput = $true
        $Process.StartInfo.RedirectStandardError = $true
        $Process.StartInfo.StandardOutputEncoding = $Utf8
        $Process.StartInfo.StandardErrorEncoding = $Utf8
        $null = $Process.Start()
        $Started = $true
        $Process.StandardInput.Close()
        $Readers = @($Process.StandardOutput, $Process.StandardError)
        $Pending = @($Readers[0].ReadLineAsync(), $Readers[1].ReadLineAsync())
        # Оба pipe читаются одновременно: большой stderr не блокирует stdout.
        while ($null -ne $Pending[0] -or $null -ne $Pending[1]) {
            $Active = [Threading.Tasks.Task[]]@($Pending | Where-Object { $null -ne $_ })
            $null = [Threading.Tasks.Task]::WaitAny($Active, 100)
            for ($Index = 0; $Index -lt $Readers.Count; $Index++) {
                if ($null -eq $Pending[$Index] -or -not $Pending[$Index].IsCompleted) { continue }
                $Line = $Pending[$Index].GetAwaiter().GetResult()
                if ($null -eq $Line) {
                    $Pending[$Index] = $null
                    continue
                }
                $Pending[$Index] = $Readers[$Index].ReadLineAsync()
                $script:LastOutput.Enqueue($Line)
                if ($script:LastOutput.Count -gt 40) { $null = $script:LastOutput.Dequeue() }
                if (-not $BuildOutput) {
                    [IO.File]::AppendAllText($LogFile, $Line + [Environment]::NewLine, $Utf8)
                }
                if ($BuildOutput -and $Line -match '^@@DNO_BUILD_STAGE@@\|([a-z_]+)$' -and $StageNames.ContainsKey($Matches[1])) {
                    Start-BuildStage $StageNames[$Matches[1]]
                } elseif ($BuildOutput -and $Line -match '^@@DNO_BUILD_ARTIFACT@@\|([^/\\]+\.rpm)$') {
                    $script:Artifacts.Add((Join-Path $DistDir $Matches[1]))
                } elseif ($DetailedOutput -and -not $CaptureOutput) {
                    Write-Host $Line
                }
                if ($CaptureOutput) { $Captured.Add($Line) }
            }
        }
        $Process.WaitForExit()
        $WslExitCode = $Process.ExitCode
        $global:LASTEXITCODE = $WslExitCode
    } finally {
        try {
            if ($Started -and -not $Process.HasExited) { $Process.Kill() }
        } finally {
            $Process.Dispose()
        }
    }
    if ($WslExitCode -ne 0) {
        $Failure = [Exception]::new("Команда WSL завершилась с кодом ${WslExitCode}.")
        $Failure.Data['ExitCode'] = $WslExitCode
        throw $Failure
    }
    if ($CaptureOutput) { $Captured.ToArray() }
}

try {
    # Кодировки обмена с wsl.exe задаются и для 5.1, и для 7; журнал пишет Linux в UTF-8.
    $OutputEncoding = $Utf8
    [Console]::InputEncoding = $Utf8
    [Console]::OutputEncoding = $Utf8
    New-Item -ItemType Directory -Force $CacheDir | Out-Null
    [IO.File]::WriteAllText($LogFile, '', $Utf8)
    if ($UpdateEnvironment -and $Targets.Count -ne 1) {
        throw 'Обновляйте только один стенд за запуск: укажите -Target mos12 или -Target mos13.'
    }
    if ($PrepareEnvironmentOnly -and -not $UpdateEnvironment) {
        throw '-PrepareEnvironmentOnly требует -UpdateEnvironment.'
    }
    Start-BuildStage 'подготовка снимка исходников'
    # Проверяем также ассеты вне git: они нужны на стенде вместе с исходниками.
    $RequiredPaths = @(
        '__main__.py', 'src', 'requirements.txt', 'dno-tool-linux.spec',
        'scripts/build_rpm.sh', 'scripts/build_rpm_container.sh', 'scripts/ensure_qt.py',
        'packaging/media', 'packaging/linux/audit_elf.py', 'packaging/linux/verify_rpm.py',
        'packaging/linux/run-wsl-build.sh', 'packaging/linux/build-output.sh',
        'packaging/linux/cleanup-wsl-build.sh',
        'packaging/linux/Containerfile.mos13', 'packaging/rpm/dno-tool.spec',
        'packaging/policykit/com.dno-tool.policy', 'packaging/policykit/com.dno-tool.desktop',
        'packaging/policykit/com.dno-tool-admin.desktop', 'packaging/policykit/dno-tool-admin',
        'assets/themes', 'assets/icons/icon.png', 'assets/icons/themes.json',
        'assets/custom/mono.zip', 'assets/custom/color.zip', 'assets/remote',
        'src/web/static/lib/novnc.bundle.min.js', 'src/workers/gst_rtsp_server.py',
        'src/workers/remote_session_lock.py'
    )
    $RequiredPaths += @('badge', 'classic', 'flat', 'line', 'midnight', 'neon', 'paper', 'solid') | ForEach-Object { "assets/icons/$_.zip" }
    if ($MasterKey) {
        if (-not (Test-Path -LiteralPath $MasterKey -PathType Leaf)) {
            throw "Нет файла открытой части мастер-ключа: $MasterKey"
        }
        $MasterKey = (Resolve-Path -LiteralPath $MasterKey).Path
    }
    $MissingPaths = @($RequiredPaths | Where-Object { -not (Test-Path -LiteralPath (Join-Path $ProjectRoot $_)) })
    if ($MissingPaths.Count) { throw ("Нет необходимых файлов сборки: " + ($MissingPaths -join ', ')) }
    Invoke-Wsl -Command @('true')
    # Архив включает незакоммиченные изменения и внешние ассеты, но не локальный профиль.
    $NativePreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $PSNativeCommandUseErrorActionPreference = $false
        # Файлы мастер-ключа из корня проекта на стенд не попадают; ключ для
        # -MasterKey дописывается в снимок отдельно под именем master-key.pub.
        $MasterExclude = @("--exclude=./master-key.key", "--exclude=./master-key.pub")
        & tar.exe -C $ProjectRoot --exclude='./.git' --exclude='./.venv' --exclude='./.cache' --exclude='./dist' --exclude='./build' --exclude='./rpmbuild' --exclude='./tests' @MasterExclude --exclude='./.codex' --exclude='./.agents' --exclude='./.aws' --exclude='__pycache__' --exclude='*.pyc' --exclude='*.log' -cf $Archive . 2>&1 | ForEach-Object {
            [IO.File]::AppendAllText($LogFile, $_.ToString() + [Environment]::NewLine, $Utf8)
            if ($DetailedOutput) { Write-Host $_.ToString() }
        }
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось подготовить снимок исходников.' }
        if ($MasterKey) {
            $MasterStage = Join-Path $CacheDir "master-key-$RunId"
            New-Item -ItemType Directory -Force $MasterStage | Out-Null
            try {
                Copy-Item -LiteralPath $MasterKey (Join-Path $MasterStage 'master-key.pub')
                & tar.exe -C $MasterStage -rf $Archive 'master-key.pub' 2>&1 | ForEach-Object {
                    [IO.File]::AppendAllText($LogFile, $_.ToString() + [Environment]::NewLine, $Utf8)
                }
                if ($LASTEXITCODE -ne 0) { throw 'Не удалось добавить мастер-ключ в снимок исходников.' }
            } finally {
                Remove-Item -LiteralPath $MasterStage -Recurse -Force
            }
        }
    } finally {
        $ErrorActionPreference = $NativePreference
    }
    $LinuxArchive = (Invoke-Wsl -Command @('wslpath', '-a', '-u', $Archive.Replace('\', '/')) -CaptureOutput | Out-String).Trim()
    $DistDir = Join-Path $ProjectRoot 'dist'
    New-Item -ItemType Directory -Force $DistDir | Out-Null
    $LinuxDist = (Invoke-Wsl -Command @('wslpath', '-a', '-u', $DistDir.Replace('\', '/')) -CaptureOutput | Out-String).Trim()
    $ReportDir = Join-Path $ProjectRoot '.cache/build-reports'
    New-Item -ItemType Directory -Force $ReportDir | Out-Null
    $LinuxReports = (Invoke-Wsl -Command @('wslpath', '-a', '-u', $ReportDir.Replace('\', '/')) -CaptureOutput | Out-String).Trim()
    $Update = if ($UpdateEnvironment) { '1' } else { '0' }
    $MasterFlag = if ($MasterKey) { '1' } else { '0' }
    if ($MasterKey) { Write-Host "[$BuildLabel] Вариант: со служебным мастер-ключом ($MasterKey)" }
    Complete-BuildStage

    foreach ($TargetName in $Targets) {
        $BuildLabel = $TargetName.ToUpperInvariant()
        $BuildStreamStarted = $false
        $CurrentStage = $null
        $TargetTimer = [Diagnostics.Stopwatch]::StartNew()
        $Artifacts = [Collections.Generic.List[string]]::new()
        $BuildId = "$RunId-$TargetName"
        $LinuxRoot = "/srv/dno-build/runs/$BuildId"
        $RunnerFile = Join-Path $CacheDir "run-$BuildId.sh"
        $RunnerFiles.Add($RunnerFile)
        $LogFile = Join-Path $CacheDir "build-$TargetName.log"
        [IO.File]::WriteAllText($LogFile, '', $Utf8)
        Start-BuildStage $StageNames.sources
        $LinuxLog = (Invoke-Wsl -Command @('wslpath', '-a', '-u', $LogFile.Replace('\', '/')) -CaptureOutput | Out-String).Trim()
        $Runner = [IO.File]::ReadAllText((Join-Path $ProjectRoot 'packaging/linux/run-wsl-build.sh')).Replace("`r`n", "`n")
        [IO.File]::WriteAllText($RunnerFile, $Runner, $Utf8)
        $LinuxRunner = (Invoke-Wsl -Command @('wslpath', '-a', '-u', $RunnerFile.Replace('\', '/')) -CaptureOutput | Out-String).Trim()
        @{ run = $BuildId; target = $TargetName; targets = $Targets; linux_root = $LinuxRoot; update_environment = [bool]$UpdateEnvironment; stage = 'building'; log_file = $LogFile } | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $CacheDir 'build-state.json')
        $PrepareOnly = if ($PrepareEnvironmentOnly) { '1' } else { '0' }
        # Если WSL не запустит runner, в журнале не должны остаться только wslpath-пути.
        [IO.File]::WriteAllText($LogFile, '', $Utf8)
        $BuildStreamStarted = $true
        Invoke-Wsl -BuildOutput -Command @('bash', $LinuxRunner, $LinuxArchive, $LinuxRoot, $LinuxDist, $Update, [string]$Jobs, $LinuxLog, $TargetName, $LinuxReports, $PrepareOnly, $MasterFlag)
        Complete-BuildStage
        @{ run = $BuildId; target = $TargetName; targets = $Targets; linux_root = $LinuxRoot; stage = 'complete'; output_dir = $DistDir; log_file = $LogFile } | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $CacheDir "build-state-$TargetName.json")
        $Result = if ($PrepareEnvironmentOnly) { 'Окружение обновлено' } else { 'Сборка завершена' }
        Write-Host ("[$BuildLabel] ${Result} ({0:N1} с)." -f $TargetTimer.Elapsed.TotalSeconds)
        foreach ($Artifact in $Artifacts) { Write-Host "[$BuildLabel] Пакет: $Artifact" }
        Write-Host "[$BuildLabel] Журнал: $LogFile"
    }
    @{ run = $RunId; targets = $Targets; stage = 'complete'; output_dir = $DistDir } | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $CacheDir 'build-state.json')
} catch {
    $FailureMessage = $_.Exception.Message
    $FailureCode = 1
    if ($_.Exception.Data.Contains('ExitCode')) { $FailureCode = [int]$_.Exception.Data['ExitCode'] }
    $FailedStage = if ($CurrentStage) { $CurrentStage } else { 'подготовка запуска' }
    $Duration = if ($StageTimer) { ' ({0:N1} с)' -f $StageTimer.Elapsed.TotalSeconds } else { '' }
    Write-Host "[$BuildLabel] Ошибка: ${FailedStage}${Duration}. $FailureMessage"
    try {
        if (Test-Path -LiteralPath $CacheDir) {
            if (-not $BuildStreamStarted -or -not (Test-Path -LiteralPath $LogFile) -or (Get-Item -LiteralPath $LogFile).Length -eq 0) {
                if ($BuildStreamStarted) {
                    [IO.File]::AppendAllText($LogFile, ($LastOutput.ToArray() -join [Environment]::NewLine) + [Environment]::NewLine, $Utf8)
                }
                [IO.File]::AppendAllText($LogFile, $FailureMessage + [Environment]::NewLine, $Utf8)
            }
            $FailedRun = if ($TargetName) { $BuildId } else { $RunId }
            @{ run = $FailedRun; target = $TargetName; targets = $Targets; linux_root = $LinuxRoot; stage = 'failed'; current_stage = $FailedStage; error = $FailureMessage; exit_code = $FailureCode; log_file = $LogFile } | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $CacheDir 'build-state.json')
        }
    } catch {
        Write-Host "[$BuildLabel] Не удалось сохранить диагностику: $($_.Exception.Message)"
    }
    Write-Host "[$BuildLabel] Последние 40 строк журнала:"
    $Tail = @()
    try {
        if (Test-Path -LiteralPath $LogFile) { $Tail = @(Get-Content -LiteralPath $LogFile -Encoding UTF8 -Tail 40) }
    } catch {
        Write-Host "[$BuildLabel] Не удалось прочитать журнал: $($_.Exception.Message)"
    }
    if ($Tail.Count -eq 0) { $Tail = @($LastOutput.ToArray()) }
    foreach ($Line in $Tail) {
        if ($Line -notmatch '^@@DNO_BUILD_(STAGE|ARTIFACT)@@') { Write-Host $Line }
    }
    Write-Host "[$BuildLabel] Журнал: $LogFile"
    exit $FailureCode
} finally {
    try {
        foreach ($TemporaryFile in @($Archive) + $RunnerFiles) {
            if ($TemporaryFile -and (Test-Path -LiteralPath $TemporaryFile)) {
                Remove-Item -LiteralPath $TemporaryFile
            }
        }
    } finally {
        [Console]::InputEncoding = $PreviousConsoleInputEncoding
        [Console]::OutputEncoding = $PreviousConsoleOutputEncoding
        $OutputEncoding = $PreviousOutputEncoding
    }
}
