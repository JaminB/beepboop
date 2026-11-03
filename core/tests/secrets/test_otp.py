"""OTP private-key reads over the vcio mailbox, with a fake firmware."""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

from beepboop.secrets.otp import (
    IOCTL_MBOX_PROPERTY,
    STATUS_ERROR,
    STATUS_SUCCESS,
    TAG_GET_PRIVATE_KEY,
    TAG_RESPONSE_BIT,
    OtpUnavailableError,
    VcioMailbox,
    build_request,
    read_otp_key,
)

KEY_WORDS = [0x01020304, 0x05060708, 0x090A0B0C, 0x0D0E0F10, 0x11121314, 0x15161718,
             0x191A1B1C, 0x1D1E1F20]  # fmt: skip


class FakeFirmware:
    """Answers the property call the way the VideoCore firmware does."""

    def __init__(
        self, key_words: list[int], *, status: int = STATUS_SUCCESS, answered: bool = True
    ) -> None:
        self.key_words = key_words
        self.status = status
        self.answered = answered
        self.seen: list[int] | None = None

    def property_call(self, words: list[int]) -> list[int]:
        self.seen = list(words)
        reply = list(words)
        reply[1] = self.status
        if self.answered:
            reply[4] = TAG_RESPONSE_BIT | 40
            reply[7:15] = self.key_words
        return reply


def test_request_matches_rpi_otp_private_key() -> None:
    # vcmailbox 0x00030081 40 40 0 8 0 0 0 0 0 0 0 0, wrapped in the mailbox header.
    assert build_request() == [64, 0, TAG_GET_PRIVATE_KEY, 40, 40, 0, 8, *([0] * 8), 0]


def test_reads_key_in_rpi_otp_private_key_byte_order() -> None:
    firmware = FakeFirmware(KEY_WORDS)
    key = read_otp_key(firmware)
    # rpi-otp-private-key prints the words as %08x, concatenated.
    assert key == bytes.fromhex("".join(f"{word:08x}" for word in KEY_WORDS))
    assert key == bytes(range(1, 33))
    assert firmware.seen == build_request()


def test_unprogrammed_otp_reads_as_none() -> None:
    assert read_otp_key(FakeFirmware([0] * 8)) is None


def test_firmware_error_status() -> None:
    with pytest.raises(OtpUnavailableError, match="rejected"):
        read_otp_key(FakeFirmware(KEY_WORDS, status=STATUS_ERROR))


def test_unanswered_tag_means_old_firmware() -> None:
    with pytest.raises(OtpUnavailableError, match="firmware"):
        read_otp_key(FakeFirmware(KEY_WORDS, answered=False))


def test_ioctl_number_matches_the_vcio_driver() -> None:
    expected = 0xC0086400 if struct.calcsize("P") == 8 else 0xC0046400
    assert expected == IOCTL_MBOX_PROPERTY


def test_missing_device_is_unavailable(tmp_path: Path) -> None:
    mailbox = VcioMailbox(tmp_path / "no-vcio")
    with pytest.raises(OtpUnavailableError):
        mailbox.property_call(build_request())


@pytest.mark.skipif(sys.platform == "win32", reason="ioctl needs a POSIX fd")
def test_non_vcio_file_fails_the_ioctl(tmp_path: Path) -> None:
    fake_device = tmp_path / "vcio"
    fake_device.write_bytes(b"")
    with pytest.raises(OtpUnavailableError, match="ioctl"):
        VcioMailbox(fake_device).property_call(build_request())


@pytest.mark.hardware
def test_real_vcio_reads_on_a_pi() -> None:
    """On a Pi (``-m hardware``): the mailbox answers. Never prints the key."""
    key = read_otp_key()
    assert key is None or len(key) == 32
