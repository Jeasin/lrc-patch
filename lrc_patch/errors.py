"""Exception types shared across modules."""
from __future__ import annotations


class ToolError(RuntimeError):
    """User-facing failure with a one-line explanation."""


class VerificationError(ToolError):
    """A post-write check did not match; the original file must stay intact."""


class SourceError(ToolError):
    """A lyrics provider could not answer.

    Kept separate from a genuine "no match" so the plan can retry later instead
    of freezing a transient 406/429/503 into the database as a verdict.
    """

    def __init__(self, source: str, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.source = source
        self.retryable = retryable
