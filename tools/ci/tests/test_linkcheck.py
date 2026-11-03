"""Tests for tools/ci/linkcheck.py."""

from __future__ import annotations

from pathlib import Path

import pytest

import linkcheck


@pytest.mark.parametrize(
    ("heading", "slug"),
    [
        ("4. Audio ↔ core event protocol", "4-audio--core-event-protocol"),
        ("17. Provider interfaces", "17-provider-interfaces"),
        ("`beepboop bench` and the M0 gates", "beepboop-bench-and-the-m0-gates"),
        ("[ADR 0015](adr/0015.md): latency", "adr-0015-latency"),
        ("sherpa_campp provider", "sherpa_campp-provider"),
        ("Hey beepboop!", "hey-beepboop"),
    ],
)
def test_github_slug(heading: str, slug: str) -> None:
    assert linkcheck.github_slug(heading) == slug


def test_anchors_dedupe_and_html_and_skip_fences() -> None:
    text = '# Intro\n## Intro\n<a id="custom"></a>\n```\n# not a heading\n```\n'
    assert linkcheck.anchors_in(text) == {"intro", "intro-1", "custom"}


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_check_files_reports_only_broken_links(tmp_path: Path) -> None:
    _write(tmp_path / "docs" / "B.md", "# Title\n## Section two\n")
    (tmp_path / "docs" / "adr").mkdir()
    source = _write(
        tmp_path / "docs" / "A.md",
        "\n".join(
            [
                "[ok](B.md) [ok anchor](B.md#section-two) [dir](adr/) [self](#local)",
                "## Local",
                "[web](https://example.com) [mail](mailto:a@b.c)",
                "`[in code](missing.md)`",
                "```",
                "[in fence](missing.md)",
                "```",
                "[bad file](nope.md)",
                "[bad anchor](B.md#nope)",
                "[ref]: ../missing-ref.md",
                "![img](img.png)",
            ]
        ),
    )
    problems = linkcheck.check_files([source], root=tmp_path)
    assert [(p.link.line, p.message, p.link.target) for p in problems] == [
        (8, "missing file", "nope.md"),
        (9, "missing anchor", "B.md#nope"),
        (10, "missing file", "../missing-ref.md"),
        (11, "missing file", "img.png"),
    ]


def test_research_is_excluded_as_a_source() -> None:
    files = linkcheck.markdown_files([linkcheck.REPO_ROOT / "docs"])
    assert files, "expected the spec docs to be found"
    assert not any("research" in p.relative_to(linkcheck.REPO_ROOT).parts for p in files)
