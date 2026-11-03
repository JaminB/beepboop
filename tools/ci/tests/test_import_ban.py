"""Tests for tools/ci/import_ban.py (CLAUDE.md hard rule 2)."""

from __future__ import annotations

from pathlib import Path

import pytest

import import_ban


@pytest.mark.parametrize(
    "source",
    [
        "import boto3",
        "import mcp.server",
        "from openai import OpenAI",
        "from anthropic.types import Message",
        "import homeassistant.core",
        "from beepboop.ha import client",
        "from beepboop import ha",
        "import beepboop.ha.cache as c",
    ],
)
def test_banned(source: str) -> None:
    assert list(import_ban.banned_imports(source))


@pytest.mark.parametrize(
    "source",
    [
        "import beepboop",
        "from beepboop import tool, ToolContext",
        "import hashlib",
        "from . import boto3",
        "# import boto3",
        '"""uses boto3 under the hood"""',
        "import mcpx",
    ],
)
def test_allowed(source: str) -> None:
    assert not list(import_ban.banned_imports(source))


def test_main_exit_codes(tmp_path: Path) -> None:
    assert import_ban.main([str(tmp_path / "absent")]) == 0
    (tmp_path / "good.py").write_text("import json\n", encoding="utf-8")
    assert import_ban.main([str(tmp_path)]) == 0
    (tmp_path / "bad.py").write_text("import boto3\n", encoding="utf-8")
    assert import_ban.main([str(tmp_path)]) == 1
