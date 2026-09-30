"""Typed connection startup failures."""


class ConnectionStartupError(RuntimeError):
    """Base class for connection startup failures."""


class PortBindError(ConnectionStartupError):
    """Backend failed to bind a pre-reserved loopback port."""


__all__ = [
    "ConnectionStartupError",
    "PortBindError",
]
