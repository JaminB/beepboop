# 0012. SvelteKit static SPA served by FastAPI; no Node in production

> Status: Accepted · 2026-11-01 · Applies to: v0.1, v0.2, v0.3+

The web UI is a SvelteKit (Svelte 5) application built with `adapter-static` in SPA mode and served as plain files by FastAPI. The same `/api` serves the UI, the CLI and tests; module settings forms are generated from manifest JSON Schema.

Related: [docs/WEB_UI.md](../WEB_UI.md) · [docs/MODULES.md](../MODULES.md) · [ADR 0002](0002-python-core.md) · [ADR 0013](0013-https-tiers.md)

## Context

The UI is a LAN settings app opened from a phone a few times a week, so SEO and first paint are irrelevant and RAM on the Pi is not. A Node SSR process would add an estimated 60–120 MB resident, a second auth and session boundary, and a third image to update. A static build is files. Every module has settings, and hand-writing a page per module would never keep up; the manifest already describes settings as JSON Schema. The `.local` hostname constrains auth (passkeys do not work there), which is handled by [ADR 0013](0013-https-tiers.md).

## Decision

SvelteKit with Svelte 5 runes and `adapter-static` (`fallback: 'index.html'`), built in CI and copied into the core image. FastAPI mounts the build directory with an `index.html` fallback and exposes a typed `/api` with an OpenAPI document; the TypeScript client is generated from it (`pnpm gen:api`), never hand-written. Components use Bits UI headless primitives plus CSS custom-property tokens; no Tailwind or component framework. Module settings forms render from `manifest.yaml`'s `settings` schema per scope. Auth is a server-side session (`__Host-bb_session`, `Secure; HttpOnly; SameSite=Strict`, 256-bit token, 12 h idle, 30 d absolute) with password (argon2id) plus optional TOTP, and passkeys only on Tier 1. UI principles: one column, at most seven sections (Home, People, Voice, Integrations, Activity, Device, Admin), sane defaults, progressive disclosure, undo over confirm dialogs except for destructive or high-risk actions, mobile first, dark mode, keyboard accessible.

## Consequences

- Zero extra processes and zero extra memory on the Pi for the UI.
- One API contract shared by UI, CLI, tests and future clients; breaking changes show up as TypeScript errors.
- New modules get a settings UI for free; custom UI per module is deliberately not supported.
- Node and pnpm are build-time dependencies only; contributors to core do not need them to run beepboop.

## Alternatives considered

- **SvelteKit `adapter-node` SSR**: duplicates the API layer and adds a process.
- **HA-style Lit panels or embedding in HA's frontend**: users are not meant to see HA.
- **Server-rendered templates from FastAPI**: workable but worse for the live meters, waterfalls and approval banners.
- **React or Vue**: fine too; Svelte 5 is smaller and the decision was made.
