"""Historical replay and paper-backtest components for the FPGA strategy."""

__all__ = [
    "canonical",
    "data_quality",
    "execution_simulator",
    "portfolio_simulator",
    "pipeline",
]
"""Historical replay and paper-backtesting toolkit."""

from .canonical import NormalizedEvent
from .execution_simulator import ExecutionConfig, ExecutionSimulator
from .pipeline import BacktestConfig, BacktestEngine
from .portfolio_simulator import PortfolioConfig, PortfolioSimulator

__all__ = [
    "BacktestConfig",
    "BacktestEngine",
    "ExecutionConfig",
    "ExecutionSimulator",
    "NormalizedEvent",
    "PortfolioConfig",
    "PortfolioSimulator",
]
