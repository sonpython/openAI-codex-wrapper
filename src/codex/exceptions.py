"""
Custom exceptions for the Codex runner subsystem.

Hierarchy:
    CodexRunnerError          — base for all codex runner errors
        WorkspaceTraversalError  — path escapes workspace boundary (C6 fix)
        CodexSessionUnhealthy    — codex auth session invalid / expired
"""

from __future__ import annotations


class CodexRunnerError(Exception):
    """Base exception for Codex runner subsystem."""


class WorkspaceTraversalError(CodexRunnerError):
    """Raised when a target path resolves outside the workspace boundary.

    Primary defense is Codex --sandbox (Landlock/Seatbelt); this is
    application-layer defense-in-depth guarding against logic bugs.
    TOCTOU between realpath and actual use is acknowledged and accepted.
    """


class CodexSessionUnhealthy(CodexRunnerError):
    """Raised when the Codex auth session is missing or expired."""
