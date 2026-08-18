"""What the console refuses, before any Host is contacted."""

from __future__ import annotations


class ConsoleError(ValueError):
    """A request this console will not turn into an operation.

    Carries the HTTP status the API answers with, so that "you did not confirm
    an irreversible operation" and "there is no such Host" are not both 400.
    """

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status
