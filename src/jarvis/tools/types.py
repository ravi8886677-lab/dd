"""Common types and result classes for tools."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class ToolExecutionResult:
    """Result object for tool execution."""
    success: bool
    reply_text: Optional[str]
    error_message: Optional[str] = None
    #: What the tool checked after acting, if anything. ``"confirmed"``
    #: means it looked and the world had changed; ``"failed"`` means it
    #: looked and it had not. Left unset when there is nothing to check,
    #: which the action log records as ``not_checked`` rather than as
    #: success — a function returning is not evidence.
    verification: Optional[str] = None
