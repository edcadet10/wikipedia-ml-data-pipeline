"""Domain-specific failures with actionable messages."""


class WikiMLError(Exception):
    """Base class for expected pipeline failures."""


class SourceError(WikiMLError):
    """A source artifact was unavailable or violated its declared contract."""


class FormatError(WikiMLError):
    """An input or output artifact had an invalid format."""


class ValidationError(WikiMLError):
    """A completed artifact failed one or more integrity checks."""
