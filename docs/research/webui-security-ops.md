# beepboop: research on web UI, security and ops for a LAN-only Pi 4 appliance

## 1. Options compared

### 1a. Web UI delivery

| Option | Extra processes | RAM on Pi 4 (my estimate, not measured) | Notes |
|---|---|---|---|
| **SvelteKit `adapter-static` SPA, files served by FastAPI** | 0 | about 0 MB extra; FastAPI is already running | The build is plain files. SPA mode only needs `fallback: 'index.html'`. No Node runtime ships on the Pi. Current releases are `@sveltejs/kit` 3.1.0 (2026-10-14) and adapter-static 4.0.0 (2026-10-01). |
| SvelteKit `adapter-node` (SSR) | +1 Node process | about 60–120 MB resident (my estimate, no benchmark found) | You get form actions and server `load`, but that duplicates the FastAPI API layer. It also adds a second auth and session boundary and another image to update. |
| HA-style Lit/Polymer panel | 0 | – | Doesn't fit the SvelteKit decision. |

SvelteKit's docs warn that SPA fallback hurts SEO and first paint. Neither matters for a LAN settings app opened a few times a week.

### 1b. HTTPS and identity on the LAN (passkeys need a secure context)

| Approach | Browser trust | Passkeys work? | Setup friction | Dependency |
|---|---|---|---|---|
| `http://beepboop.local` | No secure context | No | None | – |
| `https://beepboop.local` with a self-signed cert or local CA | Only after the CA is installed on every phone and laptop | Likely no. Chrome's RP-ID check uses the Public Suffix List, and `.local` has no PSL entry, so creation throws `SecurityError` (third-party report, 2026). | High | – |
| **Project-run DNS zone, `<id>.bb.example.net` → LAN IP, wildcard cert via ACME DNS-01** (the Plex `*.plex.direct` pattern) | Yes | Yes | None for the user | Project runs a DNS/ACME helper. Routers with DNS-rebinding protection block private-IP answers. |
| **User's own domain + ACME DNS-01** (Cloudflare and others via lego/certbot plugins) | Yes | Yes | Medium (API token) | User's DNS provider |
| **Tailscale `tailscale cert`** | Yes, at `name.tailnet.ts.net` | Yes | Low if they already use Tailscale | Tailscale. The hostname is published in CT logs. Renewal of file-based certs is up to you; Caddy integration auto-renews. |

Let's Encrypt is shortening certificate lifetimes. The default profile moves to 64 days on 2027-02-10 and to 45 days on 2028-02-16, so beepboop must renew using ARI or at about two-thirds of the lifetime. A hard-coded 60-day renewal interval will break.

### 1c. OAuth for third-party integrations when the device is LAN-only

| Pattern | How it works | Limits |
|---|---|---|
| **Static "my" bounce page** (Home Assistant's approach) | HA signs a JWT `state` containing `flow_id` and the local `redirect_uri` (HS256, with a random secret kept on the instance). The provider redirects to `https://my.home-assistant.io/redirect/oauth`. That page redirects the *browser* back to the local callback, which needs no login because it validates `state`. HA also ships a PKCE variant (`LocalOAuth2ImplementationWithPkce`). | Needs a static page on a project domain. The Pi never has to be reachable from the internet, because the browser does the hop. |
| Device-code flow (RFC 8628) | The user enters a code on their phone. | Google allows it only for `openid/email/profile`, `drive.file/appdata` and `youtube*`. **No Calendar or Gmail.** Many providers don't support it at all (I believe Spotify doesn't; not verified). |
| Loopback redirect (`http://127.0.0.1`) | Desktop-app client type. | The browser isn't running on the Pi, so this doesn't work. |
| Bring-your-own client ID/secret ("Application Credentials") | The user registers their own app with the provider. | Painful, but it avoids the project having to pass Google's restricted-scope verification. |

### 1d. Secrets at rest (threat: someone steals the SD card)

| Option | Pi 4 cost | Unattended boot? |
|---|---|---|
| LUKS with `aes-xts` | about 52 MiB/s (Pi 4 has no AES instructions) | No; needs a passphrase |
| LUKS with `xchacha12,aes-adiantum` | about 160 MiB/s, faster than the SD card (about 45 MB/s) | No, unless the key comes from OTP |
| **Pi OTP device private key** (`rpi-otp-private-key`, BCM2711/2712) | Write once, 256-bit | Yes. Read via `/dev/vcio` (video group). The official `rpi-sb-provisioner` uses it to unlock LUKS without a password. |
| App-level envelope encryption (AES-GCM or libsodium secretbox) with a key derived from OTP | Negligible | Yes |

Raspberry Pi's own docs say Pi computers have no hardware-protected key store. OTP protects against theft of the card alone, not theft of the whole Pi.

### 1e. Database, updates, audio

- **Database.** SQLite in WAL mode uses about 0 MB extra and is one file, so backups are trivial. Postgres idles at about 30–60 MB (my estimate) and adds another container and another migration story. With WAL, `synchronous=NORMAL` can lose the last transaction on power loss. Card wear depends on write volume, not journal mode.
- **Updates.** Watchtower (containrrr) was archived on 2025-12-17, and its old Docker API client is reportedly rejected by newer engines. Alternatives are the nicholas-fedor fork, WUD, Diun (notify only) and Cup.
- **Home Assistant Core.** Users report 1 GB Pi 4s running out of memory in 2026 and a possible leak since 2026.3. That points to a **4 GB Pi 4 minimum** for beepboop plus HA plus local STT/TTS. Discovery needs host networking.
- **Audio in containers.** Use `devices: [/dev/snd]` plus `group_add: [audio]` for raw ALSA. Sharing a PipeWire socket needs matching UID and `XDG_RUNTIME_DIR`.

## 2. Recommendations

1. **UI.** Use `adapter-static` SPA served by FastAPI `StaticFiles` with an `index.html` fallback, behind one reverse proxy (Caddy) that handles TLS. There is one API (`/api`, OpenAPI-typed, with a TS client generated from it) and no Node in production. The same API serves the UI, a CLI and tests.
2. **Auth in tiers.**
   - Tier 0 is the default: HTTPS with a device-generated local CA, plus password login with an optional TOTP code.
   - Tier 1 adds passkeys once a real domain exists, either via Tailscale cert, the user's own domain with DNS-01, or an optional project relay `*.<hash>.beepboop.direct`.
   - Sessions are server-side rows in SQLite. The cookie is `__Host-bb_session` with `Secure; HttpOnly; SameSite=Strict`, the token is 256-bit random, idle timeout is 12 h and absolute is 30 d. CSRF uses double-submit or an Origin-header check.
   - Use `webauthn` (duo-labs/py_webauthn, 2.7.x) for passkeys.
3. **First-run claim.** On first boot the device speaks and blinks a 6-digit claim code, for example: "Hi, I'm beepboop. Open beepboop.local and enter 4-7-1-9-2-2". The code is single-use, expires in 15 minutes, is rate-limited, and is the only way to create the first admin. After that, the wizard goes: admin account, Wi-Fi/locale, Bedrock credentials (or skip), voice enrollment, then optionally invite household members. Each invite produces a one-time link or QR code.
4. **OAuth.** Copy HA's design: a static `oauth.beepboop.dev/redirect` page on GitHub Pages with no backend, a signed JWT `state` carrying `flow_id`, `profile_id`, the local redirect and a nonce, and PKCE. Default to bring-your-own client credentials. Use device-code flow wherever the provider supports it, because it is the simplest. HA-backed integrations reuse HA's own flows.
5. **Secrets.** Use an envelope scheme. The data key is encrypted by a KEK derived from the OTP device key using HKDF, with a fallback to a passphrase-wrapped KEK on dev boards. Encrypt OAuth tokens, HA long-lived tokens, Bedrock keys and voiceprints. Optionally offer LUKS with Adiantum for the data partition.
6. **Compose layout.**

   | Service | Network | Devices | Approx. memory limit |
   |---|---|---|---|
   | `beepboop-core` (FastAPI, agent, MCP) | – | – | 768 MB |
   | `beepboop-audio` (wake word, speaker ID, AEC I/O) | – | `/dev/snd` | 512 MB, `cpuset` pinned to cores 2–3 for latency |
   | `homeassistant` | host | – | 1 GB |
   | `caddy` | – | – | 64 MB |
   | optional `stt` / `tts` (Wyoming) | – | – | – |

   All images are pinned by digest. Updates are **manual from the UI**: Diun-style notify, then a one-click "update", which snapshots first, pulls, health-checks, and rolls back automatically on failure. Don't run silent auto-updates on a device that controls locks. Use zram swap and `log2ram`, and boot from SSD over USB 3 where possible.
7. **Data.** Use SQLite in WAL mode with `synchronous=FULL` for the auth and ACL tables and `NORMAL` for traces, through a single writer task. Run a nightly `VACUUM INTO` snapshot with age encryption to USB, SMB or S3. Restoring a backup is part of onboarding ("restore from backup?").
8. **Observability.** Use `structlog` JSON logs to journald with a `turn_id` on every log line. Each turn is a trace row with spans: wake, speaker ID (with score), STT, LLM calls, tool calls (with risk level and 2FA outcome), TTS, and total latency. The UI's Activity view shows each user's own turns as a waterfall, and admins see all. Wire up OpenTelemetry export as an optional sink.

## 3. Gotchas and risks specific to beepboop

- **Passkeys on `.local` don't work in Chrome.** Don't make passkeys the primary login without a real domain. IP-address RP IDs are also invalid.
- **DNS rebinding protection** in home routers (MikroTik, pfSense/Unbound) blocks a plex.direct-style approach. You need a fallback to `https://beepboop.local` with the local CA, plus clear docs.
- **The OAuth bounce page is a phishing target.** Validate the `state` signature, keep it short-lived and single-use, and bind it to the session cookie. The callback must not require login, as in HA PR #254, or users who didn't tick "remember me" break.
- **Google restricted scopes** (Gmail, Calendar write) need CASA verification for a shared client ID. Bring-your-own credentials or an HA integration avoids this.
- **Voiceprints are biometric data** under GDPR Art. 9 and Illinois BIPA. Get explicit consent at enrollment, store them encrypted, and make deletion real.
- **Speaker ID is not authentication.** A replayed recording can impersonate a user, which is why the risk tiers and second factor matter. The audit log should flag high-risk actions where the speaker score is low.
- **SD cards.** Constant trace and log writes plus power loss risk corruption. Write in batches, keep audio in tmpfs, and recommend an SSD.
- **HA host networking** means HA's port 8123 is exposed on the LAN. Bind it or firewall it, and make beepboop the only intended UI.
- **Chrome Local Network Access** (Chrome 142+, opt-out policy removed in 156) blocks public pages from calling `beepboop.local` via fetch or iframe. Top-level redirects are reportedly fine, but never have the bounce page `fetch()` the device.
- **Memory.** HA plus Python plus local Whisper on a 4 GB Pi is tight. Without hard `mem_limit`s, the kernel's out-of-memory killer may kill the audio service in the middle of an utterance.

## 4. Ideas you haven't asked for yet

- **Privacy defaults.** Audio retention is off by default, with transcripts only. Per user, there's an opt-in "keep audio 24 h for debugging". The UI shows a privacy ledger ("what beepboop stored about you"), one-click export as JSON plus WAV in a zip, and per-user delete that cascades to memory, traces, voiceprint and tokens.
- **Physical mic-mute state** shown in the UI, plus a spoken "forget the last thing I said" command.
- **"Why did you do that?" view.** Click any turn to see the transcript, the tools exposed to the model (filtered by ACL), the tool calls, and the decision about whether a second factor was needed. This also doubles as the main debugging tool for module authors.
- **Dry-run / sandbox mode per module,** plus a "test as user X" chat box in the admin UI, so engineers can try MCP tools without speaking.
- **Recovery.** A printed recovery code at onboarding, and a physical button held for 10 s to re-enter claim mode without wiping data.
- **Health page.** CPU temperature and throttling flags (`vcgencmd get_throttled`), SD wear, disk free, cert expiry, latency p50 and p95 per stage, and HA connection status.
- **Guest and kid profiles** in the UI as presets rather than special code paths: a preset is just an ACL template plus a risk ceiling.
- **UI principles:**
  - One column, at most about 7 top-level sections: Home, People, Voice, Integrations, Activity, Device, Admin.
  - Every setting has a sane default, so nothing is required after onboarding.
  - Progressive disclosure: an "Advanced" fold per module.
  - Module settings forms are generated from the JSON Schema in `manifest.yaml`, so no per-module UI code is needed.
  - Undo instead of confirm dialogs, except for destructive or high-risk actions.
  - Plain-language copy, with the spoken persona matching the visual one.
  - Instant feedback: the voice enrollment meter animates and shows a "sounds like you: 94%" test.
  - Dark mode, keyboard accessible, mobile first (most users will set it up from a phone).
  - Small component kit with no heavy UI framework, e.g. Bits UI or Melt headless primitives plus your own CSS tokens.

## 5. Sources

- SvelteKit releases: https://releases.sh/svelte/kit.md
- SPA vs SSG: https://blog.siddjain.com/2026/04/11/sveltekit-spa-vs-ssg/
- `.local` WebAuthn failure: https://gist.github.com/o-az/8186cc726c1182776a1b039d408ad2b1
- Passkeys need a secure context: https://www.corbado.com/blog/test-passkeys-localhost-ngrok
- Tailscale HTTPS: https://tailscale.com/kb/1153/enabling-https
- Let's Encrypt lifetimes: https://letsencrypt.org/docs/cert-lifetimes/ and https://isrg.org/post/from-90-to-45/
- HA OAuth flow code: https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/helpers/config_entry_oauth2_flow.py
- HA PR #254 (callback without login): https://github.com/home-assistant/my.home-assistant.io/pull/254
- HA Application Credentials: https://home-assistant.io/integrations/application_credentials
- HA "Link account" hang report: https://community.home-assistant.io/t/link-account-step-hangs-does-nothing-affects-multiple-cloud-integrations-nest-tesla-fleet/1022667
- Google device-flow scopes: https://developers.google.com/identity/protocols/oauth2/limited-input-device
- RFC 8628: https://www.rfc-editor.org/rfc/rfc8628
- Chrome Local Network Access: https://developer.chrome.com/blog/local-network-access and https://www.superchargebrowser.com/library/chrome-156-local-network-access/
- plex.direct and DNS rebinding: https://discourse.pi-hole.net/t/add-plex-direct-to-dns-rebinding/74780 and https://forums.plex.tv/t/plex-cannot-install-ssl-plex-direct/714656
- Pi OTP key and secure boot provisioner: https://www.raspberrypi.com/documentation/security/secure-boot-provisioner.html and https://github.com/raspberrypi/documentation/pull/2672/files
- Adiantum on Pi 4: https://blog.urbackup.org/?p=358, https://github.com/rr-developer/LUKS-on-Raspberry-Pi and https://discourse.osmc.tv/t/enable-adiantum-on-raspberry-pi-kernels/90634
- Watchtower end of maintenance: https://linuxiac.com/docker-update-tool-watchtower-reaches-end-of-maintenance/ and https://linuxhandbook.com/blog/watchtower-like-docker-tools/
- HA RAM on Pi: https://community.home-assistant.io/t/minimum-ram-requirement-1gb-is-not-enough/1003104/1 and https://github.com/linuxserver/docker-homeassistant/issues/146
- HA host networking: https://shop.zimaspace.com/blogs/support-tips/should-home-assistant-use-host-or-bridge-network
- SQLite on Pi: https://spin.atomicobject.com/sqlite-raspberry-pi/, https://github.com/thp/urlwatch/issues/690 and https://oneuptime.com/blog/post/2026-09-08-sqlite-power-loss-sd-cards-flash-storage/view
- Container audio: https://github.com/mviereck/x11docker/wiki/Container-sound:-ALSA-or-Pulseaudio and https://gitlab.freedesktop.org/pipewire/pipewire/-/issues/1612
- py_webauthn version: https://depscope.dev/pkg/pypi/webauthn

**Unverified (estimates or single sources):**
- The RAM figures for Node SSR, Postgres and HA are my estimates; I found no benchmarks.
- That Spotify lacks device-code flow is from memory.
- The `.local` passkey rejection comes from one third-party gist that reads Chromium's code. The official Chromium WebAuthn origins page returned a 503, so I couldn't check it, and the gist didn't test an RP ID equal to the exact `.local` host.