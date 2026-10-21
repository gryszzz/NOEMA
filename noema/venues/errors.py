"""Secret-safe venue failures shared by adapters and the execution gateway."""

from __future__ import annotations


class VenueRejected(RuntimeError):
    """The venue refused the order before acceptance. The code is safe to log."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class VenueUnavailable(RuntimeError):
    """The venue could not confirm an outcome. Do not assume the order is absent."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)
