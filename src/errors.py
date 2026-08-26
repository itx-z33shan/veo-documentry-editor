"""Centralized error types for Veo Documentary Editor.

Every failure the tool can encounter maps to a human-readable exception.
The CLI turns these into clean messages instead of stack traces.
"""


class EditorError(Exception):
    """Base class for all editor errors."""

    def __init__(self, message, *args, hint=None):
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self):
        return self.message


class EnvironmentError_(EditorError):
    """FFmpeg / FFprobe binary problems, Python version, etc."""


class ConfigurationError(EditorError):
    """Invalid or conflicting configuration."""


class MediaNotFoundError(EditorError):
    """A required input file is missing (narration, music, script...)."""


class NoClipsError(EditorError):
    """The clips directory contains no usable footage."""


class MediaProbeError(EditorError):
    """A media file could not be probed (corrupt / unsupported)."""


class TimelineError(EditorError):
    """The timeline could not be built or is invalid."""


class RenderError(EditorError):
    """An FFmpeg render/encode step failed."""


class DiskSpaceError(EditorError):
    """Not enough free disk space for the operation."""


class OverrideError(EditorError):
    """A manual timeline override is invalid."""
