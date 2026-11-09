"""Exceptions translated into denied or failed tool observations."""


class Denied(Exception):
    """A path or permission restriction prevented the requested operation."""


class ToolError(Exception):
    """A tool request was malformed or could not be completed."""
