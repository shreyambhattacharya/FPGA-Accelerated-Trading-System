$ErrorActionPreference = 'Stop'

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$gowinShell = 'C:\Gowin\Gowin_V1.9.11.03_Education_x64\IDE\bin\gw_sh.exe'
if (-not (Test-Path $gowinShell)) { throw "Gowin shell not found at $gowinShell" }

foreach ($symbolCount in @(4, 8, 16, 32)) {
    $env:GOWIN_MATRIX_SYMBOLS = "$symbolCount"
    & $gowinShell (Join-Path $repoRoot 'fpga\scripts\run_gowin_matrix.tcl')
    if ($LASTEXITCODE -ne 0) { throw "Gowin matrix run failed for NUM_SYMBOLS=$symbolCount" }
}

Remove-Item Env:GOWIN_MATRIX_SYMBOLS -ErrorAction SilentlyContinue
Write-Host "Gowin NUM_SYMBOLS matrix complete: 4, 8, 16, 32"
