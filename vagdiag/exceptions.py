"""Exception hierarchy for VAGDIAG.

Every exception carries a human-readable message and an optional ``hint``
that the user interface shows instead of a raw traceback.
"""

from __future__ import annotations

__all__ = [
    "VagdiagError",
    "TransportError",
    "KWPError",
    "KWPTimeout",
    "KWPProtocolError",
    "KWPConnectionError",
    "KWPRejected",
]

_DEFAULT_HINT = (
    "Check that the ignition is ON, that the KKL cable is seated in the OBD "
    "port, and that the FTDI latency timer is set to 1 ms. Switch the ignition "
    "off for five seconds and try again."
)


class VagdiagError(Exception):
    """Base class for every VAGDIAG error."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class TransportError(VagdiagError):
    """Physical layer failure (serial port could not be opened, and so on)."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(
            message,
            hint or "Check the port with --list-ports and make sure no other "
            "software (VCDS, for example) is holding it open.",
        )


class KWPError(VagdiagError):
    """Base class for KWP1281 protocol failures."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message, hint or _DEFAULT_HINT)


class KWPTimeout(KWPError):
    """The control module did not answer in time. The session is dead."""


class KWPProtocolError(KWPError):
    """Bad acknowledgement byte, malformed block, or unexpected block title."""


class KWPConnectionError(KWPError):
    """The 5-baud init failed - no control module answered."""


class KWPRejected(KWPError):
    """The control module refused the command (answered with another title)."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(
            message,
            hint or "This control module probably does not support the "
            "function, or it requires a login first.",
        )
