# Contributing to beepboop

> Status: Draft spec · 2026-11-01 · Applies to: M0, v0.1, v0.2, v0.3+

Thank you for helping build a voice assistant that knows who is talking and keeps audio at home. This guide covers the development setup (planned), the pull-request checklist, the docs-first rule, how to contribute a module, and how architecture decisions are recorded. It applies equally to humans and to Claude Code sessions; the detailed build conventions live in [CLAUDE.md](CLAUDE.md).

Related: [CLAUDE.md](CLAUDE.md) · [README.md](README.md) · [docs/MODULES.md](docs/MODULES.md) · [docs/adr/README.md](docs/adr/README.md) · [SECURITY.md](SECURITY.md) · [docs/ROADMAP.md](docs/ROADMAP.md)

---

## 1. Where we are

The project is in the **spec phase**. The most valuable contributions right now are:

- Reading a doc in `docs/` and opening an issue for anything unclear, contradictory or missing.
- Reviewing the M0 plan in [docs/ROADMAP.md](docs/ROADMAP.md) and volunteering for a task.
- Pi 4 measurements. If you have a Pi 4 (4 GB or 8 GB) with a USB SSD and a ReSpeaker XVF3800, you can help validate the latency contract before anyone writes a feature.

Code contributions are welcome once M0 scaffolding lands; watch the `m0` label.

## 2. Development setup (planned)

Nothing below is runnable yet. It is the intended workflow so tooling converges on it.

**Prerequisites**

| Tool | Version | Why |
|---|---|---|
| Python | 3.12 | Core and audio services |
| `uv` | current | Workspace, lockfile, `uv run` |
| Node + `pnpm` | Node 22 LTS, pnpm 10 | `ui/` only; not needed in production |
| Docker + Compose v2 | current | Contract tests against a pinned HA container |
| A Raspberry Pi 4 | 4 GB or 8 GB, SSD, fan | Only for `beepboop bench` and audio work; everything else runs on a laptop |

**First-time setup**

```bash
git clone https://github.com/JaminB/beepboop.git
cd beepboop
uv sync                              # core, audio, tools from uv.lock
pnpm --dir ui install
uv run pre-commit install            # ruff, pyright, manifest lint on commit
uv run beepboop db migrate           # creates ./dev.sqlite
uv run beepboop chat --as admin      # text chat through the real policy path
```

**Day-to-day**

```bash
uv run pytest                        # unit tests
ruff check --fix . && ruff format . && pyright
uv run beepboop module check modules/<id>
pnpm --dir ui dev                    # UI dev server proxying /api to core
uv run beepboop-audio serve --fake-mic fixtures/kitchen_timer.wav   # no hardware
```

On a Pi: `uv run beepboop bench all --gates`. Please include the output JSON (`tools/bench/results/`) in any PR that touches `audio/` or `core/beepboop/agent/`.

## 3. The docs-first rule

beepboop is documentation-first because the agent itself reads module docs (`AGENT.md`) to learn how to use integrations, and because a Claude Code session should be able to start work from the docs alone.

Concretely:

- **Design changes land as docs before, or with, code.** If a PR changes an interface, event, schema, risk-tier rule, default, or user-visible behaviour, it updates the relevant `docs/*.md` in the same PR. "Will document later" PRs are not merged.
- **Decisions get an ADR.** Anything that a future contributor might reasonably ask "why?" about gets a short record in `docs/adr/`. See §6.
- **Numbers carry provenance.** Any latency, RAM or accuracy number in a doc says whether it was measured (Pi variant, date, command) or is a target.
- **One owner per fact; docs sized for the current milestone.** Restating a schema, table or procedure in a second doc is a bug; link to the owner instead. Topic docs fully specify M0 and v0.1 only; v0.2+ gets a short block of invariants plus a ROADMAP link. Soft budget about 5,000 words per topic doc (agent-teaching docs exempt). The full rule is [CLAUDE.md §4 rule 12](CLAUDE.md).
- **`docs/research/` is read-only.** It is the provenance of the original design. Do not edit it; supersede it with an ADR.
- **Authority order** when docs disagree: `docs/research/DECISIONS.md` > `docs/adr/*` > topic doc in `docs/` > `docs/research/SYNTHESIS.md`. Fix the lower-authority doc.

## 4. Pull-request checklist

Copy this into your PR description and tick what applies. CI enforces the mechanical ones.

```
- [ ] One logical change; title uses a conventional prefix (feat/fix/docs/adr/module/bench/chore)
- [ ] `ruff check`, `ruff format --check`, `pyright` (strict) pass
- [ ] `uv run pytest` passes; new behaviour has tests
- [ ] If touching modules/: `beepboop module check` passes for each module touched
- [ ] If touching providers/: the conformance suite passes
- [ ] If touching audio/ or core/beepboop/agent/: `beepboop bench all --gates` output from a real Pi attached
- [ ] Docs updated in the same PR (which files: ...)
- [ ] ADR added or amended if this is a design decision (docs/adr/NNNN-...)
- [ ] No new dependency without a pinned version and a one-line justification
- [ ] Hard rules in CLAUDE.md §4 respected (policy in middleware, no SDK imports in modules,
      no audio outside the audio service, no secrets/PINs in logs or LLM context, voice ID is not auth)
- [ ] User-facing strings (speech and UI) are plain, short, and locale-ready
- [ ] Commit messages carry the attribution lines required by the repo's tooling, if any
```

Reviewers look for, in this order: a hard-rule violation, a missing doc update, an unnecessary addition to core that should be a module, then everything else.

## 5. Contributing a module

Modules are the main way beepboop grows. The contract is [docs/MODULES.md](docs/MODULES.md), the skeleton is [docs/module-template/](docs/module-template/README.md), and the short version is [CLAUDE.md §6](CLAUDE.md).

Before you start, check two things:

1. **Does Home Assistant already do it?** If HA has an integration for the service and it can hold one config entry per person, write a `kind: ha` module. Native modules are for services HA cannot model per person (for example, a personal CalDAV calendar with an app password). This is [ADR 0001](docs/adr/0001-home-assistant-backbone.md).
2. **Is it one module or two?** A module is one service, one `SETUP.md`. If the setup instructions fork ("if you use X do this, if Y do that"), it is probably two modules.

**Module PR checklist** = `beepboop module check modules/<id>` passes (lint L1–L15, which includes ≥ 3 eval cases per tool and ≥ 1 denial) + an HA-first justification in the PR + the privacy section of `SETUP.md` + a catalog row in [docs/MODULES.md §16](docs/MODULES.md). Any bundled data or model licence goes in `modules/<id>/LICENSES.md`.

Modules in this repository are MIT like everything else. Third-party modules arrive as Python entry points in v0.3; until then, copy the module into `modules/`.

## 6. Architecture decision records

An ADR is a one-page note that says what was decided and why, so nobody has to re-litigate it. **When to write one:** you change a default, add or remove a dependency category, add a second mechanism where one existed, change an interface used by more than one package, change a risk-tier rule, or reverse an earlier ADR. For small clarifications that do not change a decision, edit the topic doc and skip the ADR.

The template, numbering, statuses and process live in one place: [docs/adr/README.md](docs/adr/README.md).

## 7. Issues, labels and discussion

- **Bugs and spec gaps:** open an issue. For spec gaps, quote the doc and line, say what is unclear, and propose wording if you can.
- **Feature ideas:** open an issue with the `proposal` label. State which milestone it fits and whether it is a module, a provider or a core change. Core changes need a strong reason; see [CLAUDE.md §4 rule 7](CLAUDE.md).
- **Security issues:** do not open an issue. Follow [SECURITY.md](SECURITY.md).
- **Labels:** `m0`, `v0.1`, `v0.2`, `v0.3`, `module`, `provider`, `docs`, `adr`, `good-first-issue`, `needs-pi` (requires real hardware to verify).

## 8. Licence and attribution

By contributing you agree that your contribution is licensed under the MIT licence in [LICENSE](LICENSE). Do not contribute code or models you do not have the right to license under MIT; speech models with other licences are downloaded at install time and listed in `models/LICENSES.md`, never vendored.

If a commit was produced with an AI coding assistant, keep the attribution trailer the tooling adds. We care that the work is correct and reviewed, not who or what typed it.

## 9. Code of conduct

This project follows the [Contributor Covenant, version 2.1](https://www.contributor-covenant.org/version/2/1/code_of_conduct/). Be kind, assume good faith, and keep disagreements about the design, not the person. Report conduct concerns to the maintainers through the contact in [SECURITY.md](SECURITY.md); they will be handled privately.
