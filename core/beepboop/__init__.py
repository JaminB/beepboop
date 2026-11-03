"""beepboop core service.

Agent loop, policy, tool registry, Home Assistant client, LLM providers, API
and storage live in subpackages (layout: CLAUDE.md §3). M0 ships only the
scaffold plus the pieces listed in CLAUDE.md §12.
"""

from importlib import metadata

try:
    __version__ = metadata.version("beepboop")
except metadata.PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
