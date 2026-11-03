"""Read the Raspberry Pi customer OTP private key through ``/dev/vcio``.

The BCM2711 has a 256-bit, write-once "customer private key" in OTP. Raspberry
Pi's ``rpi-otp-private-key`` tool reads it with the firmware mailbox property
tag ``0x00030081``; this module sends the same request straight to the
``vcio`` driver with one ``ioctl``, so the core container needs only
``devices: ["/dev/vcio:/dev/vcio"]`` (OPERATIONS §3) and no userland tools.

Byte order: the key is the eight 32-bit response words, each written
big-endian, in order. That is exactly ``bytes.fromhex()`` of what
``rpi-otp-private-key`` prints, so the two can be cross-checked (V5 probe).

An all-zero key means "never programmed"; :func:`read_otp_key` returns ``None``
for it and the caller falls back to the passphrase KEK (``kek.py``). Writing
the key is the installer's job and is irreversible; nothing here writes.

The key is never logged or printed. Use :func:`beepboop.secrets.kek.Kek.fingerprint`
on the derived KEK when you need to compare runs.
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path
from typing import Protocol

VCIO_PATH = Path("/dev/vcio")

#: Firmware mailbox property tag: get customer OTP private key.
TAG_GET_PRIVATE_KEY = 0x00030081
#: Mailbox status words.
STATUS_REQUEST = 0x00000000
STATUS_SUCCESS = 0x80000000
STATUS_ERROR = 0x80000001
#: Bit set in a tag's length word once the firmware has answered it.
TAG_RESPONSE_BIT = 0x80000000

KEY_WORDS = 8
_VALUE_BYTES = (2 + KEY_WORDS) * 4  # offset word + count word + 8 key words = 40


class OtpUnavailableError(RuntimeError):
    """``/dev/vcio`` cannot be used (not a Pi, not mapped into the container, no permission)."""


class MailboxTransport(Protocol):
    """Sends one property-interface buffer and returns the firmware's answer."""

    def property_call(self, words: list[int]) -> list[int]:
        """Send ``words`` (a complete mailbox buffer) and return the response buffer."""
        ...


def _iowr(type_: int, nr: int, size: int) -> int:
    """Linux ``_IOWR``: direction read|write, argument ``size`` bytes."""
    return (3 << 30) | (size << 16) | (type_ << 8) | nr


#: ``IOCTL_MBOX_PROPERTY = _IOWR(100, 0, char *)`` from the vcio driver.
IOCTL_MBOX_PROPERTY = _iowr(100, 0, struct.calcsize("P"))


class VcioMailbox:
    """The real transport: ``ioctl(fd, IOCTL_MBOX_PROPERTY, buffer)`` on ``/dev/vcio``."""

    def __init__(self, path: Path = VCIO_PATH) -> None:
        self.path = path

    def property_call(self, words: list[int]) -> list[int]:
        """Blocking (microseconds). Call via ``asyncio.to_thread`` from async code."""
        if sys.platform == "win32":
            msg = "/dev/vcio does not exist on Windows"
            raise OtpUnavailableError(msg)
        import fcntl

        buffer = bytearray(struct.pack(f"<{len(words)}I", *words))
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except FileNotFoundError as exc:
            msg = f"{self.path} not found (not a Raspberry Pi, or not mapped into this container)"
            raise OtpUnavailableError(msg) from exc
        except PermissionError as exc:
            msg = f"no permission to open {self.path} (needs the video group or root)"
            raise OtpUnavailableError(msg) from exc
        try:
            fcntl.ioctl(fd, IOCTL_MBOX_PROPERTY, buffer, True)
        except OSError as exc:
            msg = f"mailbox ioctl on {self.path} failed: {exc.strerror}"
            raise OtpUnavailableError(msg) from exc
        finally:
            os.close(fd)
        return list(struct.unpack(f"<{len(words)}I", bytes(buffer)))


def build_request() -> list[int]:
    """The 16-word property buffer that asks for all eight key words.

    Mirrors ``vcmailbox 0x00030081 40 40 0 8 0 0 0 0 0 0 0 0`` from
    ``rpi-otp-private-key``: tag, value buffer size, request length, then the
    value (row offset 0, row count 8, eight words of space for the answer).
    """
    value = [0, KEY_WORDS] + [0] * KEY_WORDS
    tag = [TAG_GET_PRIVATE_KEY, _VALUE_BYTES, _VALUE_BYTES, *value]
    body = [STATUS_REQUEST, *tag, 0]  # trailing 0 is the end tag
    return [(len(body) + 1) * 4, *body]


def parse_response(words: list[int]) -> bytes:
    """Extract the 32-byte key from a mailbox response, checking every status word."""
    if len(words) != len(build_request()):
        msg = f"mailbox response has {len(words)} words"
        raise OtpUnavailableError(msg)
    if words[1] != STATUS_SUCCESS:
        msg = f"firmware rejected the request (status 0x{words[1]:08x})"
        raise OtpUnavailableError(msg)
    if words[2] != TAG_GET_PRIVATE_KEY or not words[4] & TAG_RESPONSE_BIT:
        msg = "firmware did not answer the OTP private-key tag (firmware too old?)"
        raise OtpUnavailableError(msg)
    offset, count = words[5], words[6]
    if offset != 0 or count != KEY_WORDS:
        msg = f"unexpected OTP answer (offset={offset}, count={count})"
        raise OtpUnavailableError(msg)
    return b"".join(word.to_bytes(4, "big") for word in words[7 : 7 + KEY_WORDS])


def read_otp_key(transport: MailboxTransport | None = None) -> bytes | None:
    """Return the 32-byte OTP key, or ``None`` if the OTP was never programmed.

    Raises :class:`OtpUnavailableError` if the mailbox cannot be used at all.
    """
    mailbox = transport if transport is not None else VcioMailbox()
    key = parse_response(mailbox.property_call(build_request()))
    return None if key == bytes(len(key)) else key
