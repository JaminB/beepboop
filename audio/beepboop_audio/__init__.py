"""beepboop audio service.

Owns the microphone and speaker: wake word, VAD, speaker ID, STT, TTS and
earcons. It talks to core only through the text event socket
(ARCHITECTURE §4); no audio ever leaves this package (CLAUDE.md hard rule 4).
Provider Protocols are specified in VOICE_PIPELINE §17.
"""

from importlib import metadata

try:
    __version__ = metadata.version("beepboop-audio")
except metadata.PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
