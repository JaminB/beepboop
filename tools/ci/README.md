# tools/ci

Small stand-alone checks run by [.github/workflows/ci.yml](../../.github/workflows/ci.yml).
Standard library only, so they run before or without the workspace.

| Script | Checks |
|---|---|
| `linkcheck.py` | Relative links, images and `#anchors` in every `*.md` resolve (GitHub slug rules). `docs/research/` is skipped as a source. Part of the doc lint in [CLAUDE.md §10](../../CLAUDE.md#10-testing-strategy). |
| `import_ban.py` | [CLAUDE.md hard rule 2](../../CLAUDE.md#4-hard-rules): nothing under `modules/` imports `mcp`, `homeassistant`, `boto3`/`botocore`, `openai`, `anthropic` or `beepboop.ha`. |

## Run

```bash
uv run python tools/ci/linkcheck.py           # or: linkcheck.py docs/ README.md
uv run python tools/ci/import_ban.py
uv run pytest tools/ci
```

Nothing here needs hardware.
