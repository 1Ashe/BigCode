"""Compatibility exports for the terminal UI package.

New code should import from bigcode.ui.
"""
from __future__ import annotations

from bigcode.ui import BigCodePromptUI, BigCodeStreamRenderer, BigCodeTUI, parse_yes_no, read_yes_no_plain

__all__ = [
    "BigCodePromptUI",
    "BigCodeStreamRenderer",
    "BigCodeTUI",
    "parse_yes_no",
    "read_yes_no_plain",
]
