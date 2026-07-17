"""voxnode — mass-deployable audio recording agent for Raspberry Pi.

Captures audio in 1-minute Opus segments, buffers to RAM, and uploads to a
central server when connectivity is available. Designed for the dialytica
project and the Seeed ReSpeaker XVF3800 USB microphone array.
"""

from voxnode.version import get_version

__version__ = get_version()
__all__ = ["get_version"]
