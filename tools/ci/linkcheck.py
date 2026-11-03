"""Check relative links and heading anchors in the repository's Markdown.

Implements the "relative links resolve" part of the doc lint (CLAUDE.md §10).
For every ``*.md`` file (except ``docs/research/``, which is read-only
provenance, and tool/venv directories) it checks that each relative link,
image and reference definition points at an existing file or directory and,
when the link has a ``#fragment`` into a Markdown file, that the target file
has a heading (GitHub slug rules) or an explicit ``<a id/name>`` with that
anchor. External links (``https:``, ``mailto:`` ...) are not fetched.

Usage::

    uv run python tools/ci/linkcheck.py            # whole repo
    uv run python tools/ci/linkcheck.py docs/ README.md

Exit status is 1 if any link is broken. Output is ``path:line: message``.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Directories never scanned as link *sources* (links *into* them are still checked).
EXCLUDED_DIRS: frozenset[str] = frozenset(
    {"docs/research", ".git", ".venv", "node_modules", ".pytest_cache", ".ruff_cache"}
)

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_CODE_SPAN = re.compile(r"(`+)(.+?)\1")
# [text](target "title") and ![alt](target); text may contain one level of [...].
_INLINE_LINK = re.compile(
    r"!?\[(?:[^\[\]]|\[[^\[\]]*\])*\]\(\s*(<[^>]*>|[^)\s]+)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)"
)
_REF_DEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(<[^>]*>|\S+)")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
_HTML_ANCHOR = re.compile(r"<a\s+[^>]*?(?:id|name)\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


@dataclass(frozen=True, slots=True)
class Link:
    """A link found in a Markdown source file."""

    source: Path
    line: int
    target: str


@dataclass(frozen=True, slots=True)
class Problem:
    """A broken link."""

    link: Link
    message: str

    def __str__(self) -> str:
        try:
            shown = self.link.source.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            shown = self.link.source.as_posix()
        return f"{shown}:{self.link.line}: {self.message} ({self.link.target})"


def github_slug(heading: str) -> str:
    """Return the anchor GitHub generates for a heading's text (before de-duplication)."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # links/images -> their text
    text = re.sub(r"<[^>]+>", "", text)  # inline HTML tags
    text = text.replace("`", "").strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)  # keep letters, digits, _, -, space
    return text.replace(" ", "-")


def _lines_outside_fences(text: str) -> Iterator[tuple[int, str]]:
    """Yield ``(line_number, line)`` for lines not inside fenced code blocks."""
    fence: str | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        match = _FENCE.match(line)
        if match:
            marker = match.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            continue
        if fence is None:
            yield number, line


def anchors_in(text: str) -> frozenset[str]:
    """All anchors a Markdown document defines: heading slugs and ``<a id/name>``."""
    anchors: set[str] = set()
    seen: dict[str, int] = {}
    for _, line in _lines_outside_fences(text):
        anchors.update(_HTML_ANCHOR.findall(line))
        heading = _HEADING.match(line)
        if heading is None:
            continue
        slug = github_slug(heading.group(2))
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}-{count}")
    return frozenset(anchors)


@cache
def _anchors_of_file(path: Path) -> frozenset[str]:
    return anchors_in(path.read_text(encoding="utf-8"))


def links_in(source: Path, text: str) -> Iterator[Link]:
    """Yield every inline link, image and reference definition outside code."""
    for number, line in _lines_outside_fences(text):
        visible = _CODE_SPAN.sub("code", line)
        for match in _INLINE_LINK.finditer(visible):
            yield Link(source, number, match.group(1).strip("<>"))
        ref = _REF_DEF.match(visible)
        if ref:
            yield Link(source, number, ref.group(1).strip("<>"))


def check_link(link: Link, root: Path = REPO_ROOT) -> Problem | None:
    """Return a :class:`Problem` if ``link`` is broken, else ``None``."""
    target = link.target
    if not target or _SCHEME.match(target):
        return None
    path_part, _, fragment = target.partition("#")
    path_part = unquote(path_part.split("?", 1)[0])
    if not path_part:
        resolved = link.source
    elif path_part.startswith("/"):
        resolved = root / path_part.lstrip("/")
    else:
        resolved = link.source.parent / path_part
    resolved = resolved.resolve()
    if not resolved.exists():
        return Problem(link, "missing file")
    is_markdown = resolved.is_file() and resolved.suffix.lower() == ".md"
    if fragment and is_markdown and unquote(fragment) not in _anchors_of_file(resolved):
        return Problem(link, "missing anchor")
    return None


def _is_excluded(path: Path, root: Path) -> bool:
    relative = path.relative_to(root).as_posix()
    return any(relative == d or relative.startswith(f"{d}/") for d in EXCLUDED_DIRS)


def markdown_files(paths: Iterable[Path], root: Path = REPO_ROOT) -> list[Path]:
    """Expand files and directories into the Markdown files to scan."""
    found: set[Path] = set()
    for path in paths:
        path = path.resolve()
        candidates = path.rglob("*.md") if path.is_dir() else [path]
        found.update(p for p in candidates if not _is_excluded(p, root))
    return sorted(found)


def check_files(files: Iterable[Path], root: Path = REPO_ROOT) -> list[Problem]:
    """Check every link in ``files``."""
    problems: list[Problem] = []
    for source in files:
        for link in links_in(source, source.read_text(encoding="utf-8")):
            problem = check_link(link, root)
            if problem is not None:
                problems.append(problem)
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("paths", nargs="*", type=Path, help="files or directories (default: repo)")
    args = parser.parse_args(argv)
    paths: list[Path] = args.paths or [REPO_ROOT]
    files = markdown_files(paths)
    problems = check_files(files)
    for problem in problems:
        print(problem)
    print(f"linkcheck: {len(files)} files, {len(problems)} broken links")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
