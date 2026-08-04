"""gdb-mcp: drive GDB from an AI agent over GDB/MI."""

from .session import CommandResult, GdbError, GdbSession, GdbStartupError, StopEvent
from .tools import Debugger

__version__ = "0.1.0"

__all__ = [
    "CommandResult",
    "Debugger",
    "GdbError",
    "GdbSession",
    "GdbStartupError",
    "StopEvent",
    "__version__",
]
