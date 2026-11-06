[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('mos12', 'mos13')][string]$Target,
    [switch]$DetailedOutput,
    [string]$Distribution = 'dno-build',
    [ValidateRange(1, 8)][int]$Jobs = 4
)

$ErrorActionPreference = 'Stop'
$BuildScript = Join-Path $PSScriptRoot 'build_linux.ps1'
& $BuildScript -Target $Target -Distribution $Distribution -Jobs $Jobs `
    -UpdateEnvironment -PrepareEnvironmentOnly -DetailedOutput:$DetailedOutput
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
