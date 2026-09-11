$ErrorActionPreference = 'Stop'

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$buildDir = Join-Path $repoRoot 'fpga\build'
New-Item -ItemType Directory -Force $buildDir | Out-Null

$sources = @(
    (Join-Path $repoRoot 'fpga\rtl\reset\power_on_reset.sv'),
    (Join-Path $repoRoot 'fpga\rtl\fifo\async_packet_fifo.sv'),
    (Join-Path $repoRoot 'fpga\rtl\fifo\packet_fifo.sv'),
    (Join-Path $repoRoot 'fpga\rtl\protocol\protocol_pkg.sv'),
    (Join-Path $repoRoot 'fpga\rtl\spi\spi_slave.sv'),
    (Join-Path $repoRoot 'fpga\rtl\protocol\loopback_engine.sv'),
    (Join-Path $repoRoot 'fpga\rtl\top\trading_spi_top.sv'),
    (Join-Path $repoRoot 'fpga\tb\tb_trading_spi_top.sv')
)

$output = Join-Path $buildDir 'tb_trading_spi_top.vvp'
iverilog -g2012 -Wall -I (Join-Path $repoRoot 'fpga\rtl\protocol') -s tb_trading_spi_top -o $output @sources
if ($LASTEXITCODE -ne 0) { throw "iverilog compilation failed" }

vvp $output
if ($LASTEXITCODE -ne 0) { throw "RTL simulation failed" }

$fifoOutput = Join-Path $buildDir 'tb_packet_fifo.vvp'
iverilog -g2012 -Wall -I (Join-Path $repoRoot 'fpga\rtl\protocol') -s tb_packet_fifo -o $fifoOutput `
    (Join-Path $repoRoot 'fpga\rtl\fifo\packet_fifo.sv') `
    (Join-Path $repoRoot 'fpga\tb\tb_packet_fifo.sv')
if ($LASTEXITCODE -ne 0) { throw "FIFO simulation compilation failed" }

vvp $fifoOutput
if ($LASTEXITCODE -ne 0) { throw "FIFO simulation failed" }

$asyncOutput = Join-Path $buildDir 'tb_async_packet_fifo.vvp'
iverilog -g2012 -Wall -I (Join-Path $repoRoot 'fpga\rtl\protocol') -s tb_async_packet_fifo -o $asyncOutput `
    (Join-Path $repoRoot 'fpga\rtl\fifo\async_packet_fifo.sv') `
    (Join-Path $repoRoot 'fpga\tb\tb_async_packet_fifo.sv')
if ($LASTEXITCODE -ne 0) { throw "Async FIFO simulation compilation failed" }

vvp $asyncOutput
if ($LASTEXITCODE -ne 0) { throw "Async FIFO simulation failed" }
