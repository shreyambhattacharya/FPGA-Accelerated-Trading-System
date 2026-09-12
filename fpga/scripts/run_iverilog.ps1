$ErrorActionPreference = 'Stop'

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$buildDir = Join-Path $repoRoot 'fpga\build'
New-Item -ItemType Directory -Force $buildDir | Out-Null

$sources = @(
    (Join-Path $repoRoot 'fpga\rtl\reset\power_on_reset.sv'),
    (Join-Path $repoRoot 'fpga\rtl\fifo\async_packet_fifo.sv'),
    (Join-Path $repoRoot 'fpga\rtl\fifo\packet_fifo.sv'),
    (Join-Path $repoRoot 'fpga\rtl\protocol\protocol_pkg.sv'),
    (Join-Path $repoRoot 'fpga\rtl\protocol\crc8_engine.sv'),
    (Join-Path $repoRoot 'fpga\rtl\protocol\packet_dispatcher.sv'),
    (Join-Path $repoRoot 'fpga\rtl\spi\spi_slave.sv'),
    (Join-Path $repoRoot 'fpga\rtl\protocol\loopback_engine.sv'),
    (Join-Path $repoRoot 'fpga\rtl\math\unsigned_divider.sv'),
    (Join-Path $repoRoot 'fpga\rtl\math\feature_normalizer.sv'),
    (Join-Path $repoRoot 'fpga\rtl\market\market_state_engine.sv'),
    (Join-Path $repoRoot 'fpga\rtl\strategy\strategy_config.sv'),
    (Join-Path $repoRoot 'fpga\rtl\strategy\signal_engine.sv'),
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

$crcOutput = Join-Path $buildDir 'tb_crc8_engine.vvp'
iverilog -g2012 -Wall -I (Join-Path $repoRoot 'fpga\rtl\protocol') -s tb_crc8_engine -o $crcOutput `
    (Join-Path $repoRoot 'fpga\rtl\protocol\protocol_pkg.sv') `
    (Join-Path $repoRoot 'fpga\rtl\protocol\crc8_engine.sv') `
    (Join-Path $repoRoot 'fpga\tb\tb_crc8_engine.sv')
if ($LASTEXITCODE -ne 0) { throw "CRC engine simulation compilation failed" }

vvp $crcOutput
if ($LASTEXITCODE -ne 0) { throw "CRC engine simulation failed" }

$dividerOutput = Join-Path $buildDir 'tb_unsigned_divider.vvp'
iverilog -g2012 -Wall -s tb_unsigned_divider -o $dividerOutput `
    (Join-Path $repoRoot 'fpga\rtl\math\unsigned_divider.sv') `
    (Join-Path $repoRoot 'fpga\tb\tb_unsigned_divider.sv')
if ($LASTEXITCODE -ne 0) { throw "Divider simulation compilation failed" }

vvp $dividerOutput
if ($LASTEXITCODE -ne 0) { throw "Divider simulation failed" }

$configOutput = Join-Path $buildDir 'tb_strategy_config.vvp'
iverilog -g2012 -Wall -I (Join-Path $repoRoot 'fpga\rtl\protocol') -s tb_strategy_config -o $configOutput `
    (Join-Path $repoRoot 'fpga\rtl\strategy\strategy_config.sv') `
    (Join-Path $repoRoot 'fpga\tb\tb_strategy_config.sv')
if ($LASTEXITCODE -ne 0) { throw "Strategy configuration simulation compilation failed" }

vvp $configOutput
if ($LASTEXITCODE -ne 0) { throw "Strategy configuration simulation failed" }

$signalOutput = Join-Path $buildDir 'tb_signal_engine.vvp'
iverilog -g2012 -Wall -I (Join-Path $repoRoot 'fpga\rtl\protocol') -s tb_signal_engine -o $signalOutput `
    (Join-Path $repoRoot 'fpga\rtl\strategy\signal_engine.sv') `
    (Join-Path $repoRoot 'fpga\tb\tb_signal_engine.sv')
if ($LASTEXITCODE -ne 0) { throw "Signal-engine simulation compilation failed" }

vvp $signalOutput
if ($LASTEXITCODE -ne 0) { throw "Signal-engine simulation failed" }

$marketOutput = Join-Path $buildDir 'tb_market_state_engine.vvp'
iverilog -g2012 -Wall -I (Join-Path $repoRoot 'fpga\rtl\protocol') -s tb_market_state_engine -o $marketOutput `
    (Join-Path $repoRoot 'fpga\rtl\math\unsigned_divider.sv') `
    (Join-Path $repoRoot 'fpga\rtl\math\feature_normalizer.sv') `
    (Join-Path $repoRoot 'fpga\rtl\market\market_state_engine.sv') `
    (Join-Path $repoRoot 'fpga\tb\tb_market_state_engine.sv')
if ($LASTEXITCODE -ne 0) { throw "Market-state simulation compilation failed" }

vvp $marketOutput
if ($LASTEXITCODE -ne 0) { throw "Market-state simulation failed" }

$latencyOutput = Join-Path $buildDir 'tb_market_latency.vvp'
iverilog -g2012 -Wall -I (Join-Path $repoRoot 'fpga\rtl\protocol') -s tb_market_latency -o $latencyOutput `
    (Join-Path $repoRoot 'fpga\rtl\math\unsigned_divider.sv') `
    (Join-Path $repoRoot 'fpga\rtl\math\feature_normalizer.sv') `
    (Join-Path $repoRoot 'fpga\rtl\market\market_state_engine.sv') `
    (Join-Path $repoRoot 'fpga\rtl\strategy\signal_engine.sv') `
    (Join-Path $repoRoot 'fpga\tb\tb_market_latency.sv')
if ($LASTEXITCODE -ne 0) { throw "Market-latency simulation compilation failed" }

vvp $latencyOutput
if ($LASTEXITCODE -ne 0) { throw "Market-latency simulation failed" }

$parameterSource = @(
    (Join-Path $repoRoot 'fpga\rtl\math\unsigned_divider.sv'),
    (Join-Path $repoRoot 'fpga\rtl\math\feature_normalizer.sv'),
    (Join-Path $repoRoot 'fpga\rtl\market\market_state_engine.sv'),
    (Join-Path $repoRoot 'fpga\tb\tb_market_parameter.sv')
)
foreach ($symbolCount in @(1, 4, 8, 16, 32)) {
    $parameterOutput = Join-Path $buildDir ("tb_market_parameter_{0}.vvp" -f $symbolCount)
    iverilog -g2012 -Wall -I (Join-Path $repoRoot 'fpga\rtl\protocol') `
        "-Ptb_market_parameter.NUM_SYMBOLS=$symbolCount" `
        -s tb_market_parameter -o $parameterOutput @parameterSource
    if ($LASTEXITCODE -ne 0) { throw "NUM_SYMBOLS=$symbolCount elaboration failed" }

    vvp $parameterOutput
    if ($LASTEXITCODE -ne 0) { throw "NUM_SYMBOLS=$symbolCount simulation failed" }
}

$n32StressOutput = Join-Path $buildDir 'tb_market_n32_stress.vvp'
iverilog -g2012 -Wall -I (Join-Path $repoRoot 'fpga\rtl\protocol') -s tb_market_n32_stress -o $n32StressOutput `
    (Join-Path $repoRoot 'fpga\rtl\math\unsigned_divider.sv') `
    (Join-Path $repoRoot 'fpga\rtl\math\feature_normalizer.sv') `
    (Join-Path $repoRoot 'fpga\rtl\market\market_state_engine.sv') `
    (Join-Path $repoRoot 'fpga\tb\tb_market_n32_stress.sv')
if ($LASTEXITCODE -ne 0) { throw "N32 stressbench compilation failed" }

vvp $n32StressOutput
if ($LASTEXITCODE -ne 0) { throw "N32 stressbench failed" }

$differentialScript = Join-Path $repoRoot 'tools\reference_model\differential_test.py'
python $differentialScript
if ($LASTEXITCODE -ne 0) { throw "Python versus RTL differential test failed" }

python $differentialScript --count 10000
if ($LASTEXITCODE -ne 0) { throw "10k Python versus RTL differential stress failed" }
