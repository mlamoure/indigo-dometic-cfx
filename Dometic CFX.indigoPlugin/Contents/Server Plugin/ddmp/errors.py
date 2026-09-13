"""Exception hierarchy for the ddmp package."""


class DdmpError(Exception):
    """Base class for every error raised by ddmp."""


class DecodeError(DdmpError):
    """A frame or payload could not be decoded."""


class NotWritable(DdmpError):
    """The topic is not in the write allow-list (default deny)."""


class StateUnknown(DdmpError):
    """A write needs the current value (per-compartment arrays) but none has been published yet."""


class ConnectionClosed(DdmpError):
    """The client is not connected."""


class WriteRejected(DdmpError):
    """The cooler answered a SET with NAK, or published a different value."""


class WriteTimeout(DdmpError):
    """The cooler did not echo a SET within the deadline."""
