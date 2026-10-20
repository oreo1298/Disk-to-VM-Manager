"""User-facing errors.

Every error Rehome shows has three parts:

* ``message`` – what went wrong, in plain English.
* ``fix`` – what the user can do about it (optional but strongly preferred).
* ``details`` – technical details (command lines, stderr, tracebacks) shown
  in an expandable section in the GUI and with ``--verbose`` in the CLI.
"""

from __future__ import annotations


class RehomeError(Exception):
    def __init__(self, message: str, *, fix: str | None = None,
                 details: str | None = None, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.fix = fix
        self.details = details
        self.code = code

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict:
        return {"message": self.message, "fix": self.fix,
                "details": self.details, "code": self.code}


class Cancelled(RehomeError):
    """A long-running operation was cancelled by the user."""

    def __init__(self, message: str = "The operation was cancelled."):
        super().__init__(message, code="cancelled")


class UnsafeSource(RehomeError):
    """A disk or image was rejected by the source safety checks."""


class NotFound(RehomeError):
    pass
