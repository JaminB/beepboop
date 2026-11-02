# 0013. HTTPS in two tiers: local CA by default, real domain unlocks passkeys and external MCP

> Status: Accepted · 2026-11-01 · Applies to: v0.1 (Tier 0), v0.2 (Tier 1)

Tier 0, the default, is `https://beepboop.local` with a Caddy-generated local CA, password plus TOTP, and a helper page for installing the CA on each device. Tier 1 is a real hostname (own domain via ACME DNS-01, or Tailscale) and is required for passkeys, phone push and external MCP clients. The canonical URL is configurable from day one.

Related: [docs/WEB_UI.md](../WEB_UI.md) · [docs/OPERATIONS.md §4](../OPERATIONS.md#4-caddy) · [docs/MCP.md](../MCP.md) · [ADR 0004](0004-beepboop-is-an-mcp-server.md) · [ADR 0014](0014-second-factor-web-confirm.md)

## Context

A LAN-only device cannot get a publicly trusted certificate for `.local`. Passkeys need a secure context and Chrome rejects `.local` and IP-address RP IDs. The Plex-style project DNS zone with a wildcard cert breaks on routers with DNS-rebinding protection (MikroTik, pfSense/Unbound) and would make the project run infrastructure. Many MCP clients require HTTPS and reject private CAs; Client ID Metadata Documents also need the Pi to fetch client metadata from the internet. Chrome's Local Network Access policy blocks public pages from fetching `beepboop.local`, so a bounce page must redirect, never fetch. Let's Encrypt lifetimes drop to 64 days in February 2027 and 45 in 2028, so renewal cannot be hard-coded.

## Decision

Caddy terminates TLS for everything. Tier 0 uses `tls internal`; the first-run wizard and `http://beepboop.local/ca` walk each OS through installing the root. Auth at Tier 0 is password (argon2id) plus optional TOTP. Tier 1 replaces `tls internal` with ACME DNS-01 against the user's DNS provider, or uses Tailscale's `*.ts.net` certificates through Caddy; renewal follows ARI. Tier 1 unlocks passkeys (`webauthn`), phone confirm, the OAuth bounce flow for native modules and the external `/mcp` door. `BB_PUBLIC_URL` is the single canonical URL used in cookies, OAuth metadata, elicitation URLs and spoken instructions. HA's `/auth/external/callback` is the only HA path Caddy exposes, on the same host.

## Consequences

- Day one works on any router with no project-hosted infrastructure.
- Installing a CA on every phone is friction; the helper page and QR code reduce it, and Tier 1 removes it.
- External MCP is a v0.2 feature gated on Tier 1, which is honest about what clients accept.
- The project must never ship a `.local`-only assumption; every URL goes through `BB_PUBLIC_URL`.

## Alternatives considered

- **Plain HTTP on the LAN**: no secure context, cookies and WebAuthn unusable, credentials in the clear.
- **Project-run `*.beepboop.direct` zone**: DNS-rebinding failures and hosted infrastructure; may return as an optional relay later.
- **Passkeys as the primary login**: impossible at Tier 0.
- **Self-signed certificate without a CA**: the same install friction with worse UX on renewal.
