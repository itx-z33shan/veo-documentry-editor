"""Input-media discovery helpers.

The editor deliberately does not care whether an ElevenLabs narration is
stored as MP3, AAC, M4A, WAV, or another FFmpeg-readable audio format.  This
module centralises the predictable filename convention while still allowing a
configuration/CLI path override.
"""

import os

from .errors import MediaNotFoundError


# These are only used for discovery. FFmpeg remains the authority on whether a
# particular file can actually be decoded.
AUDIO_EXTENSIONS = (
    ".mp3", ".aac", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".aif",
    ".aiff", ".wma",
)


def _resolve_explicit(path, base_dir=None):
    """Return an existing explicit path, resolving relative paths safely.

    Config paths are normally relative to the repository working directory,
    while a bare filename is convenient relative to ``input/`` or ``music/``.
    Accept both forms rather than turning ``input/narration.aac`` into the
    accidental path ``input/input/narration.aac``.
    """
    if not path:
        return None
    path = os.path.expanduser(str(path))
    candidates = [path]
    if not os.path.isabs(path) and base_dir:
        candidates.append(os.path.join(base_dir, path))
    for candidate in candidates:
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None


def find_audio(directory, stems, explicit_path=None, required=False,
               label="audio"):
    """Find an audio input using an explicit path or conventional stems.

    ``stems`` are checked in order for every supported extension.  For
    example, ``find_audio(input_dir, ("narration", "voice"))`` accepts
    ``narration.aac`` without requiring an artificial rename to ``.mp3``.
    A deterministic case-insensitive fallback is included for manually named
    files such as ``ElevenLabs Narration.m4a``.
    """
    directory = os.path.abspath(directory)
    explicit = _resolve_explicit(explicit_path, directory)
    if explicit:
        return explicit
    if explicit_path:
        if required:
            raise MediaNotFoundError(
                "%s file configured at %r was not found." % (label, explicit_path),
                hint="Check the path or place the file in %s." % directory)
        return None

    for stem in stems:
        for ext in AUDIO_EXTENSIONS:
            candidate = os.path.join(directory, stem + ext)
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)

    if os.path.isdir(directory):
        wanted = tuple(str(stem).lower() for stem in stems)
        matches = []
        for name in os.listdir(directory):
            root, ext = os.path.splitext(name)
            if ext.lower() not in AUDIO_EXTENSIONS:
                continue
            lowered = root.lower()
            if lowered in wanted or any(stem in lowered for stem in wanted):
                matches.append(name)
        if matches:
            matches.sort(key=lambda n: n.lower())
            return os.path.abspath(os.path.join(directory, matches[0]))

    if required:
        expected = ", ".join("%s%s" % (stems[0], ext)
                             for ext in AUDIO_EXTENSIONS[:5])
        raise MediaNotFoundError(
            "%s file not found in %r." % (label.capitalize(), directory),
            hint="Place it there as one of: %s (or set its path in config)."
                 % expected)
    return None


def find_narration(input_dir, explicit_path=None, required=True):
    """Find the primary narration / voice-over file."""
    return find_audio(input_dir, ("narration", "voice", "elevenlabs"),
                      explicit_path=explicit_path, required=required,
                      label="narration")


def find_music(music_dir, explicit_path=None, required=False):
    """Find an optional external music stem."""
    return find_audio(music_dir, ("background", "music", "bed"),
                      explicit_path=explicit_path, required=required,
                      label="background music")
