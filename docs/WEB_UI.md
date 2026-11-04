# Web UI

> Status: Draft spec · 2026-11-01 · Applies to: v0.1 (Tier 0, approvals, enrolment, activity). v0.2+ (Tier 1, passkeys, OAuth connect flows, HA flow renderer, budgets, one-click update) appears here only as invariants; detail lives in [ROADMAP.md](ROADMAP.md#v02-your-accounts-your-devices).

The web UI is the only screen beepboop has. It is a LAN-only SvelteKit static SPA served by the core's FastAPI process behind Caddy, opened mostly from phones: to claim the device on first boot, to enrol a voice, to approve a `critical` action the device just asked about, and to answer "why did you do that?". Everything the UI does goes through the same `/api` the CLI and tests use; the UI has no privileged path of its own.

Related: [README.md](../README.md) · [ARCHITECTURE.md](ARCHITECTURE.md) · [IDENTITY_AND_TRUST.md](IDENTITY_AND_TRUST.md) · [DATA_AND_PRIVACY.md](DATA_AND_PRIVACY.md) · [MODULES.md](MODULES.md) · [MCP.md](MCP.md) · [OPERATIONS.md](OPERATIONS.md) · [VOICE_PIPELINE.md](VOICE_PIPELINE.md) · [GLOSSARY.md](GLOSSARY.md) · ADRs [0012](adr/0012-sveltekit-static-spa.md), [0013](adr/0013-https-tiers.md), [0014](adr/0014-second-factor-web-confirm.md), [0018](adr/0018-no-audio-retention.md)

---

## 1. Design principles

1. **Minimal and calm.** One column, six areas, no widgets that need tending: a fuse box, not a cockpit.
2. **One primary action per screen,** named for its effect (*Unlock front door*). If a screen needs two, it is two screens.
3. **Progressive disclosure.** One *Advanced* fold per module and profile; raw JSON, UUIDs, scores and token counts live behind *Details*.
4. **Everything is explainable.** Who beepboop thought was speaking, which tools it offered, why it asked or refused: one tap away in Activity. Never "something went wrong" without what and what next.
5. **Mobile first.** The approve path is designed at 360 px with the primary button under the thumb; desktop adds margin and two-pane Activity and Access.
6. **Accessible (WCAG 2.2 AA).** Keyboard reachable, visible focus, 24×24 px targets, no drag-only interactions, paste allowed in the TOTP field, live regions for approvals and enrolment.
7. **Dark and light by system preference,** manual override per browser; both themes contrast-checked in CI.
8. **Undo over confirm.** Reversible actions get an *Undo* toast; confirm dialogs only for destructive actions. Approving a `critical` action is one tap on a dedicated page (§5.7), never a dialog.
9. **Same voice as the device.** The persona that speaks also writes the UI copy (§11).
10. **Nothing per module.** Settings forms are rendered from `manifest.yaml` JSON Schema; if the renderer cannot express a setting, the renderer is extended, not the module.

---

## 2. Technology

| Concern | Pick | Notes |
|---|---|---|
| Framework | SvelteKit 3.1 + Svelte 5 (runes), `@sveltejs/adapter-static` 4.0, `fallback: 'index.html'` | Pure SPA. No Node in production. Kit 3 reads its config from `ui/vite.config.ts`; `src/lib` is imported as `#lib/*` (package subpath imports). |
| Serving | FastAPI `StaticFiles(html=True)` at `/`, `index.html` fallback for every non-`/api`, non-`/mcp`, non-`/auth` path | Hashed assets `Cache-Control: public, max-age=31536000, immutable`; `index.html` `no-cache`. |
| Components | Bits UI headless primitives + our own CSS tokens (`--bb-*`) | No utility-class framework. No chart library: waterfall and similarity grid are HTML/CSS. |
| API client | `openapi-typescript` + `openapi-fetch`, generated from `/api/openapi.json` into `ui/src/lib/api/` | CI runs `pnpm --dir ui gen:api` and fails on diff. |
| Live data | Server-Sent Events at `/api/events` | One stream per session, filtered by §6.4. |
| Forms | `SchemaForm.svelte`: the JSON Schema subset in [MODULES.md §6.1](MODULES.md#61-supported-json-schema-subset) → form | Module settings, profile settings, System tabs, onboarding (§9). |
| i18n | Compile-time messages in `ui/messages/<locale>.json` through a typed `t(key)` (`ui/src/lib/i18n.ts`): a missing key fails `pnpm check`; ICU `{n, plural, …}` and `{name}` only | English only in v0.1; keys from day one (§10). Paraglide JS replaces the helper, same files and keys, when a second locale lands. |
| Build size | ≤ 250 kB gzipped JS, first paint < 1 s on a mid-range phone over Wi-Fi | Target, enforced as a CI bundle budget. |
| Security headers | CSP `default-src 'self'; img-src 'self' data:; connect-src 'self'` (`csp.mode: 'hash'`, written by SvelteKit as a `<meta>` in the static shell, so its script hashes match; `style-src` adds `'unsafe-hashes'` for exactly one hash, the inline style of SvelteKit's route announcer, and Playwright fails on any CSP violation), plus HTTP headers from FastAPI on every response: `Content-Security-Policy: frame-ancestors 'none'` (a meta tag cannot carry it), `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`, `Permissions-Policy: microphone=()` | The browser never gets microphone access: enrolment uses the Pi's mic (§5.3). |

The `ui/` tree is part of the repository layout in [CLAUDE.md §3](../CLAUDE.md#3-repository-layout). `pnpm --dir ui build` is the only Node step; it runs on the developer machine or in CI, never on the Pi, and the output is copied into the `beepboop-core` image.

---

## 3. Information architecture

Six areas. This section is the only source of screen names and `›` paths; other docs link here rather than naming screens themselves. Routes are stable so spoken instructions ("open the People page") and docs can name them.

```
/claim                      first boot: enter the spoken claim code (no session)
/login                      password → TOTP → session
/invite/<token>             accept an invite (§5.1b)
/onboarding/*               wizard, admin only, runs once (resumable)

/                           Home
/activity                   Activity  →  /activity/<turn_id>   ·   tab /activity/try  ("Try a request")
/people                     People    →  /people/new   /people/guest   /people/<id>  (tabs Profile · Voice · Access · Privacy)
/me                         shortcut to /people/<your id>
/integrations               Integrations  →  /integrations/<module_id>   ·   /integrations/ha/inbox  (new devices)
/system                     System  (tabs Health · LLM · Voice · Backups · Updates · HTTPS · Logs)
/approvals/<id>             Approve page, reached from the banner only; not a nav item

/ca                         local CA download + fingerprint + per-OS install help (Tier 0, no session needed)
/auth/external/callback     (Caddy → Home Assistant, HA OAuth flows only; not a UI route)
```

**Navigation.** On phones a bottom bar: Home, Activity, People, More (Integrations, System, sign out). On ≥ 900 px a left rail with all six areas. The approval banner (§5.7) sits above the content on every page while something is pending for the signed-in profile.

**Who sees what.** Web roles come from `profiles.kind`; this is the only place code branches on `kind` (voice policy never does). The parental rule is [IDENTITY_AND_TRUST.md §10](IDENTITY_AND_TRUST.md#10-parental-view-of-child-history).

| Area | admin | member | child | guest |
|---|---|---|---|---|
| Home | all household status | own timers, own approvals, household status | own timers | no login |
| Activity | own turns; all child and guest turns including transcripts; other adults' tool calls without transcripts. *Try a request* as self or as a child or guest | own turns. *Try a request* as self | own turns. *Try a request* as self | — |
| People | manage all, invite, enrol anyone, edit everyone's Access, export or delete anyone | own page (Profile, Voice, Privacy); own Access read-only | own Profile and Voice; no PIN, no approval method; Access and Privacy read-only | — |
| Integrations | household settings, own connections, the new-device inbox | own connections | none (an adult connects for them) | — |
| System | all tabs | Health read-only | none | — |
| Approve page | own approvals only | own only | none (ceiling `act`, never asked) | — |

A child's web login is optional and created by an admin; it never has a PIN or approval method because the Child preset's ceiling is `act`.

---

## 4. Shell and shared elements

```
┌──────────────────────────────────────┐  360 px phone, light theme
│ ● beepboop                 Alex ▾    │  ← header: status dot (mirrors the device ring)
├──────────────────────────────────────┤
│ ▲ Unlock the front door?             │  ← ApprovalBanner, only when pending for me
│   asked 12 s ago · 48 s left   [Open]│
├──────────────────────────────────────┤
│                                      │
│         (page content)               │
│                                      │
├──────────────────────────────────────┤
│  Home     Activity     People    More
│   ◉          ○           ○        ○
└──────────────────────────────────────┘
```

- **Status dot** mirrors the LED ring table in [VOICE_PIPELINE.md §13.1](VOICE_PIPELINE.md#131-state-table), fed by the `device` SSE event: blue listening/thinking/speaking, grey idle, red muted, amber waiting for a PIN or error, struck-through "offline" when the audio socket is down. Tapping it opens System › Health.
- **Toasts** bottom-centre, 6 s, with *Undo* where applicable; one at a time.
- **Live region** (`aria-live="polite"`) announces banner changes, enrolment steps and toasts.
- **Empty states** say what the page is for and offer the one action that makes it non-empty.
- **Certificate banner** (Tier 0): until the local CA is trusted by this browser, a non-blocking banner says "Your browser doesn't trust me yet. [Install my certificate]" and links `/ca`.

---

## 5. Screens

Wireframes are phone width. `[ ]` is a button, `( )` a radio, `[x]` a checkbox, `◉` the filled primary button.

### 5.1 First-run onboarding

The device has no password yet, so trust is bootstrapped by physical presence: on first boot the device speaks and blinks a six-digit **claim code**, then its certificate fingerprint: *"Hi, I'm beepboop. Open beepboop dot local and enter 4 7 1 9 2 2. My certificate fingerprint starts 3A 7F 09 C2 1B E4."* The code is single-use, expires after 15 minutes, allows 5 attempts, and is the only way to create the first admin. After onboarding, a claim code alone never creates or resets an admin: re-entering claim mode (button 10 s or `beepboop claim --reopen`) accepts only an admin recovery code or *Factory reset*, per [OPERATIONS.md §6](OPERATIONS.md#6-first-boot).

```
┌──────────────────────────────────────┐   ┌──────────────────────────────────────┐
│ beepboop                             │   │ ‹ Set up beepboop          step 1 of 6│
│                                      │   │                                      │
│ Hi. I just said a six-digit code     │   │ Your account                         │
│ out loud. Type it here.              │   │ You'll be the admin of this house.   │
│                                      │   │                                      │
│   [ 4 ] [ 7 ] [ 1 ] [ 9 ] [ 2 ] [ 2 ]│   │ Name        [ Alex              ]    │
│                                      │   │ Username    [ alex              ]    │
│ Didn't hear it? Hold the button on   │   │ Password    [ ●●●●●●●●●●●●      ]    │
│ the device for 10 seconds and I'll   │   │             strength ▰▰▰▱  good      │
│ say a new one.                       │   │ Authenticator app (TOTP)             │
│                                      │   │   [QR]  key ABCD-EFGH…  code [      ]│
│ Not secure? My certificate starts    │   │                                      │
│ 3A 7F 09 C2 1B E4 → Install it       │   │              ◉ Continue              │
│            ◉ Continue                │   └──────────────────────────────────────┘
└──────────────────────────────────────┘
```

Wizard steps (resumable; reopens at the first incomplete step until done):

| # | Step | Notes |
|---|---|---|
| 0 | Claim code | Creates a `claim` session that can only call `/api/onboarding/*`. Shows the CA link and fingerprint (§7). Offers *Restore from backup* ([DATA_AND_PRIVACY.md §11](DATA_AND_PRIVACY.md#11-backups)). |
| 1 | Admin account | Password and TOTP (QR + manual key + one verification code). TOTP is required for admins and members (§6.1). |
| 2 | House | Name, locale (`en-US` only in v0.1, list ready), timezone (from browser), units. |
| 3 | LLM | Bedrock region + credentials (or an AWS profile mounted at `/run/secrets/aws`), model Haiku 5.5 default. *Skip for now* is allowed: the fast path works offline. |
| 4 | Your home | Device grants, below. |
| 5 | Your voice | Enrolment (§5.3), consent screen first. Skippable; without it you are Guest when speaking. |
| 6 | Household | Add people now or later; each admin/member gets an invite link + QR (one-time, 24 h). |

**Your home** (step 4). With no HA devices yet: "I use Home Assistant to talk to your lights, locks and speakers. [Open Home Assistant to add devices] Come back here when you're done." plus *Skip for now*. With devices found:

```
┌──────────────────────────────────────┐
│ ‹ Set up beepboop          step 4 of 6│
│ Your home                            │
│ Found 23 devices in 6 rooms.         │
│                                      │
│ (●) Let everyone in the house use    │
│     lights, switches, media and      │
│     climate in every room            │
│     (recommended)                    │
│ ( ) I'll choose one by one           │
│                                      │
│ Locks, garage doors and alarms       │
│ always go to adults only. You can    │
│ change any of this later.            │
│                                      │
│              ◉ Continue              │
└──────────────────────────────────────┘
```

The recommended choice writes `ha_area` grants for `household` at each domain's default risk and grants critical-domain entities to `adults`; "one by one" sends every entity to the inbox (§5.5). Either way it is an explicit admin decision and default-deny is unchanged.

**Finish screen**, in order: the **Recovery kit** (one printable page with the 10 TOTP recovery codes and the backup recovery key, plus an "I saved it" tick), a **practice approval** (add-to-home-screen hint plus *Try it*, which makes the device say "This is a practice. Approve it on your phone." and raises a no-op approval through the real banner path: `POST /api/approvals/practice`; nothing executes it), and "Say *Hey beepboop, what can you do?*". The certificate banner (§4) stays until the CA is trusted; nothing blocks on it.

### 5.1b Accepting an invite

`/invite/<token>` (one-time, 24 h) is a three-step flow: **1.** "Hi Sam, Alex added you to beepboop." Choose a password; set up TOTP. **2.** Your voice: "Go stand near beepboop and tap Start", or *Do this later* (leaves a Needs-attention item on Sam's Home). **3.** Get approvals on this phone: the same hint and practice approval as the admin's Finish screen, so the first real approval is not the first one Sam sees.

### 5.2 Home

```
┌──────────────────────────────────────┐
│ ● beepboop                   Alex ▾  │
├──────────────────────────────────────┤
│ Good evening, Alex.                  │
│ Listening · Home Assistant connected │
│ Last heard: Sam · 88 %, 4 min ago    │
│                                      │
│ Your timers                          │
│  ⏱ Pasta           4:12 left  [Stop] │
│  ⏰ Tomorrow 06:30  weekdays   [Edit] │
│                                      │
│ Needs attention                      │
│  ▲ 3 new devices found        [Review]│
│  ▲ Calendar needs sign-in     [Fix]   │
│  ▲ Guest access on until 23:30 [End now]
│                                      │
│ Today                                │
│  18:41 Sam  "turn off the garden…"   │
│  18:12 You  "set a timer for 10 m…"  │
│  17:55 Guest "what's the weather"    │
│                           See all →  │
└──────────────────────────────────────┘
```

Home is a status page, not a control panel: device state line, the signed-in profile's live timers and alarms (*Stop*/*Snooze* via `timer.cancel`/`timer.set`), a *Needs attention* list (inbox count, integrations in `reauth|error`, guest access active, update available, backup failed or single-destination, cert expiring, voice enrolment missing or needing a redo), and the last few turns visible to this profile. Admins also see a one-line cost meter.

### 5.3 People and voice enrolment

```
┌──────────────────────────────────────┐   ┌──────────────────────────────────────┐
│ ‹ People                      [+ Add]│   │ ‹ Alex › Voice                       │
│                                      │   │                                      │
│ Alex        admin    voice · 94 %    │   │        ◠◠◠◠◠◠◠◡◡◡                   │
│ Sam         member   voice · 88 %    │   │       (progress ring)                │
│ Robin       child    re-enrol ⚠      │   │                                      │
│ Guest       (anyone I don't recognise)│  │   Say it from the kitchen doorway:   │
│                                      │   │                                      │
│ Invites                              │   │          “Hey beepboop”              │
│  Jo  member  link expires in 19 h    │   │                                      │
│      [Copy link] [Show QR] [Revoke]  │   │   ▰▰▰▰▰▰▰▰▱▱  listening…            │
│                                      │   │                                      │
└──────────────────────────────────────┘   │   Clip quality  loudness good ·      │
                                           │   noise quiet · clipping none        │
                                           │                                      │
                                           │          [Redo last]   ◉ Next        │
                                           └──────────────────────────────────────┘
```

**Add person** → preset (Admin, Member, Child, Guest) → name → invite link for Admin/Member, optional login for Child. Presets only pre-fill grants, ceiling and schedule ([IDENTITY_AND_TRUST.md §9](IDENTITY_AND_TRUST.md#9-presets)); everything is editable afterwards on the Access tab.

**Guest page** (`/people/guest`) owns time-boxed access with one card:

```
┌──────────────────────────────────────┐
│ Guest access                         │
│ Let anyone I don't recognise use:    │
│  [x] Living room  [x] Kitchen        │
│  [ ] Kids' rooms                     │
│ until  [ Tonight 23:30        ▾ ]    │
│          ◉ Turn on guest access      │
└──────────────────────────────────────┘
```

While active, every adult's Home shows "Guest access on until 23:30 [End now]". Under the hood it is scheduled `act` grants for the `guest` profile, nothing more (IDENTITY_AND_TRUST §9 owns the grant list).

**Enrolment** runs on the device's own microphone, never the phone's. The phone is the teleprompter; the audio service records, gates quality and embeds, and only embeddings and quality numbers cross to core ([VOICE_PIPELINE.md §9.2](VOICE_PIPELINE.md#92-flow) owns clip counts and gates). **No recording is ever stored** ([ADR 0018](adr/0018-no-audio-retention.md)): a speaker-model upgrade therefore means re-enrolling (about 2 minutes per person), which the People list flags as "re-enrol ⚠" and Home lists under Needs attention. Flow:

1. **Consent** (full screen, must scroll): what a voiceprint is, that it is stored encrypted on this device only, never sent anywhere, derived from audio that is discarded, and deletable in one tap. For a child, the consenting admin is recorded. Text and version are stored with the voiceprint ([DATA_AND_PRIVACY.md §9](DATA_AND_PRIVACY.md#9-consent-export-and-delete)).
2. **Clips** with room-position prompts ("from the couch", "closer"), one at a time under a progress ring; no "step 3 of 10" counters. Child copy: "Robin's turn! Say *Hey beepboop* when the ring turns blue."
3. **Per-clip quality gates** from the audio service; a failing clip says why in one line ("too much background noise") and is redone.
4. **Result**: one sentence about the household, numbers under Details:

```
   Everyone sounds distinct.
                                   — or —
   ▲ Alex and Robin sound alike. I'll ask for a quick OK on your phone before
     personal requests for either of them when I'm not sure.   [Re-enrol Robin]
   ▸ Details (similarity grid, τ, margin, model id, clip count)
```

The "sound alike" rule is owned by [VOICE_PIPELINE.md §9.3](VOICE_PIPELINE.md#93-similarity-check-and-feedback). The Voice tab also offers *Re-enrol* (replaces the voiceprint) and *Delete voiceprint*. v0.2 invariant: the live "say anything, see the score" meter, adaptation status and re-enrol prompts live on this tab and add no new data class ([ROADMAP.md](ROADMAP.md#v02-your-accounts-your-devices)).

### 5.4 Profile (`/people/<id>`, tab Profile; `/me` for yourself)

```
┌──────────────────────────────────────┐
│ ‹ Alex     Profile · Voice · Access · Privacy
│                                      │
│ How I talk to you                    │
│ Voice        [ en_US-lessac-medium ▾]│  ▶ preview
│ Speed        slower ○──●──── faster  │
│ Verbosity    ( ) brief (●) normal ( ) chatty
│ Persona      [ household default  ▾ ]│  → Integrations › persona
│ Language     [ English (US)       ▾ ]│
│                                      │
│ Approving sensitive actions          │
│ I'll always ask you to approve on    │
│ your phone for things like unlocking │
│ doors.                               │
│ [x] Also let me say a spoken PIN     │
│     when my phone isn't around       │
│     PIN: set ·  [Change] [Remove]    │
│                                      │
│ Sign-in                              │
│ Password  [Change]   TOTP  on [Manage]│
│ Sessions  3 devices  [See all]       │
│                                      │
│ Your connections → Integrations      │
│ ▸ Advanced (STT vocabulary, thresholds)
└──────────────────────────────────────┘
```

- **Voice preview** synthesises one sentence on the device; it plays from the speaker, since the browser has no audio path.
- **PIN** is set here and nowhere else (never by voice). Enabling it shows "A spoken PIN can be overheard. Phone approval is safer." Admins can set or clear a PIN for others but never see it. Length, storage, capture and lockout rules: [IDENTITY_AND_TRUST.md §8](IDENTITY_AND_TRUST.md#8-second-factor).
- **Your connections** is a link: profile-scoped integrations are configured in Integrations › `<module>` › *Your connection* (§5.6).
- **Advanced**: personal STT hotwords (contact names, room nicknames) and the identity thresholds that apply to this person, read-only with "why" text.

### 5.5 Access (grants) and the new-device inbox

Access is a tab on the person page; the inbox lives under the Home Assistant card.

```
┌──────────────────────────────────────┐   ┌──────────────────────────────────────┐
│ ‹ Robin (child) › Access             │   │ ‹ Home Assistant › New devices (3)   │
│ Ceiling: act  · Schedule: 07:00–20:00│   │ Found since Tue. Nobody can use      │
│ [Edit preset]                        │   │ these until you decide.              │
│                                      │   │                                      │
│ Living room                          │   │ Garage door        cover  critical ▾ │
│  Ceiling light         act   [●  ]   │   │   ( ) Everyone  (●) Adults           │
│  TV                    act   [●  ]   │   │   ( ) Only: [Alex ✓][Sam  ]  ( ) Ignore
│  Thermostat            read  [●  ]   │   │ Kitchen plug       switch  act ▾     │
│ Robin's room                         │   │   (●) Everyone  ( ) Adults           │
│  Lamp                  act   [●  ]   │   │   ( ) Only: [      ]         ( ) Ignore
│ Front door                           │   │ Weather station    sensor  read ▾    │
│  Lock          critical → above ceiling   │   (●) Everyone  ( ) Adults           │
│                                      │   │   ( ) Only: [      ]         ( ) Ignore
│ Modules                              │   │                                      │
│  timers.*  lists.*  weather.*  act ✓ │   │             ◉ Apply                  │
│  caldav.*  music.*       not granted │   └──────────────────────────────────────┘
│                                      │
│ ▸ Advanced: raw grants, YAML export  │
└──────────────────────────────────────┘
```

- Every toggle writes or deletes one `grants` row ([IDENTITY_AND_TRUST.md §5](IDENTITY_AND_TRUST.md#5-grants)); resources are HA areas and entities (keyed by UUID, shown by friendly name) and module tool globs. Group grants show as inherited rows with a lock icon and a link to the group.
- The risk chip is the risk the `ha` module derived from the domain. It can be raised; it can be lowered only for indirect services, and only after the chip's menu has shown what the service does ([IDENTITY_AND_TRUST.md §6.3](IDENTITY_AND_TRUST.md#63-ha-service-classes)).
- **Inbox** (`/integrations/ha/inbox`, [IDENTITY_AND_TRUST.md §6.2](IDENTITY_AND_TRUST.md#62-new-device-inbox)): one radio group per row, one *Apply* for the page. Default *Everyone* for `read`/`act` domains, *Adults* for `critical`. Each decision is undoable from a toast for 6 s.
- **YAML export** (Advanced) renders the effective policy in the form of IDENTITY_AND_TRUST §5.2. Import and diff are v0.2.

### 5.6 Integrations

```
┌──────────────────────────────────────┐   ┌──────────────────────────────────────┐
│ ‹ Integrations                       │   │ ‹ Calendar (caldav)        v0.1.0    │
│                                      │   │ Read and add events on the user's    │
│ ┌ Home Assistant ──────── household ┐│   │ own CalDAV calendar.                 │
│ │ Connected · 142 entities · ok     ││   │                                      │
│ │ 3 new devices → New devices       ││   │ Your connection          ● reauth    │
│ └───────────────────────────────────┘│   │ CalDAV URL   [ https://cloud.exam…]  │
│ ┌ Timers & alarms ─────── household ┐│   │ Username     [ alex               ]  │
│ │ ok                                ││   │ App password [ ●●●●●  set · Replace] │
│ └───────────────────────────────────┘│   │ Calendar     [                    ]  │
│ ┌ Calendar (CalDAV) ─────── per person┐  │   blank = your default calendar      │
│ │ You: reauth · Sam: ok             ││   │                                      │
│ └───────────────────────────────────┘│   │            ◉ Test and save           │
│ ┌ Music ───────────────── household ┐│   │   Health: "Signed in; 2 calendars"   │
│ │ Music Assistant via HA · ok       ││   │                                      │
│ └───────────────────────────────────┘│   │ ▸ Advanced                           │
│ ┌ Weather ─────────────── household ┐│   │   Default lookahead  [ 7 ] days      │
│ │ ok · Home                         ││   │ ▸ Setup guide (SETUP.md)             │
│ └───────────────────────────────────┘│   │ ▸ What it can do (2 tools)           │
│ ┌ Persona ─────────────── household ┐│   │   caldav.events        personal      │
│ │ "beepboop" · 1 override (Robin)   ││   │   caldav.create        personal      │
│ └───────────────────────────────────┘│   └──────────────────────────────────────┘
└──────────────────────────────────────┘
```

- **Cards** come straight from `manifest.yaml` (`summary`, `scope` badge, aggregated status); no per-module UI code.
- **Settings forms** are `SchemaForm` over `manifest.settings` (§9): one form for `household` scope (admins); for `profile` scope the same page shows *Your connection* for the signed-in person (admins: per person). `x-per-profile` fields appear under *Your settings* here; the person page only links. The heading is the manifest `title` ("Calendar"); the id, scope and category sit under *Details*. `x-entity` fields are a select over the entities granted to the signed-in person (`GET /api/integrations/<id>/entities?domains=`). The **Setup guide** fold shows the module's `SETUP.md`. Clearing a secret is a confirm dialog whose button names the secret ("Clear App password").
- **Test and save** saves, then calls `Module.health(ctx)` (a real read-only call) and shows `Health.detail` inline; there is no separate test hook. Status words are `Health.status` ([MODULES.md §5.1](MODULES.md#51-module-base-class-and-hooks)) plus `off` for a disabled integration (a UI state, not a Health value).
- **Connect flows** in v0.1 are credential forms. v0.2 invariant: the HA data-entry-flow renderer and OAuth via the bounce page reuse this card, status model and *Your connection* placement ([MODULES.md §8](MODULES.md#8-oauth-v02-invariants-that-bind-v01)).
- **Home Assistant card** owns the inbox (§5.5) and links to HA's own UI through the admin-gated Caddy route (§5.11). **Persona** is a module like any other; the Profile tab's selector points at its settings form.

### 5.7 Approvals (second factor)

Design rule: the approve page must be reachable and actionable in **under 60 seconds** from "approve on your phone" by someone not yet looking at their phone. Hence a 30-day session, the banner on every page, a home-screen icon and practice approval at onboarding, and an SSE push to all of that person's open sessions.

```
┌──────────────────────────────────────┐   ┌──────────────────────────────────────┐
│ ‹ Approve                            │   │ ‹ Approve                            │
│                                      │   │                                      │
│ Unlock the front door?               │   │ This request expired.                │
│                                      │   │ Say it again and I'll ask you once   │
│ Asked by voice in the hallway        │   │ more.                                │
│ 14 s ago · sounded like you · 91 %   │   │                                      │
│                                      │   │                   [Back]             │
│ ⏱ 46 s left   ▰▰▰▰▰▰▰▰▱▱▱▱           │   └──────────────────────────────────────┘
│                                      │
│ ▸ Details                            │
│   tool  home.set                     │
│   lock.front_door → unlocked         │
│   turn 01J…   device main            │
│                                      │
│         ◉ Unlock front door          │
│         [ Don't unlock ]             │
│          That wasn't me              │
└──────────────────────────────────────┘
```

The approval record, state machine, expiry and nonce rules are owned by [IDENTITY_AND_TRUST.md §8](IDENTITY_AND_TRUST.md#8-second-factor) and ADR [0014](adr/0014-second-factor-web-confirm.md); the registry side is [MCP.md §9.3](MCP.md#93-second-factor). The UI adds:

- The page shows only approvals whose `profile_id` equals the session's profile. Admins cannot approve for others; the device said *your* phone.
- **One tap, no dialog, no hold.** The primary button names the effect; the friction is the page itself. *Don't unlock* records `denied` with no speaker-ID effect. The quieter *That wasn't me* link also denies and records a misattribution per [VOICE_PIPELINE.md §8.8](VOICE_PIPELINE.md#88-that-wasnt-me).
- Approve is `POST /api/approvals/{id}/approve {nonce}`, the nonce having arrived in the SSE `approval` event; a second tap gets `409`. If a login for the approval's profile succeeds, or one of its sessions opens the approvals stream (`/api/events`), while it is live, `exp` extends once to 120 s and the device speaks "still waiting". The nonce is stored only as a hash, so core keeps the plaintext in memory until the decision or `exp` and replays it to that profile's (re)connecting streams; the polling fallback never carries it.
- **Transport**: SSE is primary; while the page is visible the UI also polls `GET /api/approvals?state=pending` every 10 s in case the stream is reconnecting. Nothing polls in the background.
- The device speaks the outcome ("Unlocked.", or that it was denied or expired); the approval appears in Activity with its `factor`. If the profile has a PIN, whichever factor arrives first wins and the page says "approved by PIN" if the voice path won.
- The phone's own lock screen is the barrier against someone else approving from an open session: an accepted residual risk (§6.2) in exchange for a 30-day session.
- v0.2 invariant: external MCP clients and push notifications land on this same `/approvals/<id>` page with the same profile check.

### 5.8 Activity ("why did you do that?")

```
┌──────────────────────────────────────┐   ┌──────────────────────────────────────┐
│ ‹ Activity   Timeline · Try a request│   │ ‹ 18:41:07  Sam · hallway            │
│          [Everyone ▾][Filter]        │   │                                      │
│ Today                                │   │ “turn off the garden lights”         │
│ 18:41 Sam · 88 %  ⚡ fast path        │   │ ↳ “Done.”                            │
│   "turn off the garden lights"       │   │                                      │
│ 18:12 Alex · 93 %  tool              │   │ Who                                   │
│   "set a timer for ten minutes"      │   │  Sam · 88 %           [That wasn't me]│
│ 17:55 Guest ○ 52 %  denied           │   │  ▸ Details: score 0.88 · margin 0.31 ·│
│   "unlock the back door"             │   │    threshold 0.70 (act) · stage final │
│ 17:40 Alex · 91 %  approved ✓        │   │                                      │
│   "unlock the front door"            │   │ What I did                            │
│ 17:02 Robin · 79 %  ⚠ tainted        │   │  ⚡ fast path matched "turn off <area>"│
│   "read my calendar"                 │   │  home.turn_off {area: garden}  act   │
│                                      │   │   allowed by grant household→Garden   │
│ Yesterday …                          │   │   HA: light.garden_1, light.garden_2 │
│                                      │   │                                      │
│ [Load more]                          │   │ Timing  total 0.46 s                  │
└──────────────────────────────────────┘   │  endpoint   ▰▰▰▰▰ 250                 │
                                           │  stt tail   ▰▰ 100                    │
                                           │  match+acl  ▏ 20                      │
                                           │  ha call    ▰ 40                      │
                                           │  tts        ▏ 0 (cached clip)         │
                                           │  output     ▰ 50                      │
                                           │                                      │
                                           │ ▸ Details (tools offered: 12, tokens 0,
                                           │   cost $0, provider none, locale en-US)
                                           │                                      │
                                           │ [Report false wake]  [Delete this turn]
                                           └──────────────────────────────────────┘
```

- **List** rows: time, speaker chip (§11.1), outcome badge (`fast path`, `tool`, `approved`, `denied`, `tainted`, `error`, `offline`), transcript where this viewer may see it (§3). Filters: person (admins), date, outcome, module; search is a substring match over visible transcripts.
- **Detail** sections, always in this order: *Who* (chip; score, margin, threshold and stage under Details), *What I did* (fast path or LLM; every tool call with args, risk, decision and the grant or denial reason; approval factor; untrusted-output markers), *Timing* (waterfall from `stages_ms_json`, stage names from [ADR 0015](adr/0015-latency-contract-and-fast-path.md)), *Details* (tools offered, tokens, cost, providers, locale, turn id).
- **That wasn't me** (owner of the attributed profile, or an admin): semantics in [VOICE_PIPELINE.md §8.8](VOICE_PIPELINE.md#88-that-wasnt-me). It never undoes the action; the toast lists what the turn did in plain words ("Turned off 2 garden lights") so you can undo it yourself.
- **Report false wake** marks the turn `false_wake` and drops its transcript. No audio exists to review ([ADR 0018](adr/0018-no-audio-retention.md)); an opt-in false-wake queue may be proposed for v0.2 via a new ADR.
- **Delete this turn** removes the turn and its tool calls; safety records stay, pseudonymised, per [DATA_AND_PRIVACY.md §9.3](DATA_AND_PRIVACY.md#93-delete-delete-me-and-delete-this-turn), and the page says so first.

### 5.9 Try a request (Activity tab)

```
┌──────────────────────────────────────┐
│ ‹ Activity › Try a request           │
│ As [ Robin (child) ▾ ]  admin only; child or guest profiles
│ Device [ main ▾ ]  [x] dry-run tools │
│                                      │
│ you › read my calendar               │
│                                      │
│ beepboop ›                           │
│  ⛔ denied  caldav.events             │
│     personal > ceiling act (Robin)   │
│  “Robin can't read calendars.”       │
│                                      │
│ you › turn on the lamp in my room    │
│ beepboop ›                           │
│  ⚡ fast path  home.turn_on           │
│     {name: "lamp", area: "Robin's room"}  act · allowed · dry-run
│  “Done.”                             │
│                                      │
│ [ Type a message…              ][Send]│
└──────────────────────────────────────┘
```

- Same agent loop, policy path and `turns` table as voice, with `via: web` and `confidence: None` ([IDENTITY_AND_TRUST.md §3](IDENTITY_AND_TRUST.md#3-the-principal)). Turns made here are badged *typed* in Activity, excluded from voice statistics, and counted in cost tracking. The reply streams over SSE `turn` events with the same chips as Activity.
- **Test as** is allowed only for child and guest profiles, and only for admins. The principal carries `actor_profile_id = <admin>` and the audit shows both; `personal` and `critical` tools run in forced dry-run with fixture outputs, and any approval is routed to the acting admin, never to the impersonated profile.
- **Dry-run** (default on) asks modules to log intended side effects instead of executing them, exactly like `beepboop module check` ([MODULES.md §12.3](MODULES.md#123-dry-run-semantics)); `critical` calls still exercise the approval flow so the banner can be tested end to end. Members and children can type as themselves without dry-run; it is a keyboard for the assistant.

### 5.10 Privacy ledger

A tab on the person page (`/people/<id>`, Privacy).

```
┌──────────────────────────────────────┐
│ ‹ Alex › Privacy                     │
│                                      │
│ What I keep about you (on this device)│
│  Voiceprint      1 · encrypted · no recordings  [Delete]
│  Transcripts     212 turns · kept 30 days   [Change] [Delete all]
│  Calendar login  CalDAV app password · encrypted [Clear]
│  Timers & lists  3 timers · 2 lists          
│  Sign-in         password · TOTP · 3 sessions [Sign out everywhere]
│  Audio           none. I never keep recordings.
│                                      │
│ What left this house (last 30 days)  │
│  Amazon Bedrock (eu-central-1)       │
│    212 transcripts + tool results    │
│    for answering. No audio, no       │
│    voiceprint, no PIN.         [Why?]│
│  cloud.example.com  58 calendar reads│
│    (your own server)           [Why?]│
│  Cloud speech  off                   │
│                                      │
│ ◉ Export my data (zip)               │
│ [ Delete me and everything above ]   │
└──────────────────────────────────────┘
```

The ledger is derived, not a separate log: counts come from `turns`, `voiceprints`, `secrets`, `integrations`, `sessions` and each module's `egress` declarations ([DATA_AND_PRIVACY.md §10](DATA_AND_PRIVACY.md#10-what-leaves-the-device)). Each *Why?* opens the module's or provider's plain-language egress note. *Delete me* requires typing the profile name, explains the cascade ([DATA_AND_PRIVACY.md §9](DATA_AND_PRIVACY.md#9-consent-export-and-delete)), and is refused for the last admin with instructions to transfer admin first.

### 5.11 System

Tabs: **Health · LLM · Voice · Backups · Updates · HTTPS · Logs**. Admin only (members see Health). On **Voice** the household thresholds and "ask on phone when unsure" are editable (`PATCH /api/system/voice`, audited; a person's own calibration from enrolment still overrides them, [VOICE_PIPELINE §8.4](VOICE_PIPELINE.md#84-accept-rule-and-per-tier-thresholds)); the wake-word sensitivity is shown from `/config/beepboop.yaml` and not editable here in v0.1, because provider configuration reaches the audio service only as that mounted file (CLAUDE.md hard rule 4). "Who's talking" lists people who sound alike once the enrolment similarity check exists (a v0.1 gap, [ROADMAP](ROADMAP.md#v01-one-house-that-knows-your-voice)).

```
┌──────────────────────────────────────┐   ┌──────────────────────────────────────┐
│ ‹ System › Health                    │   │ ‹ System › Voice                     │
│ Pi 4 8 GB · up 6 d · 61 °C · no throttle   │ Wake word  "Hey beepboop"            │
│ RAM  core 410 M  audio 590 M  HA 720 M│   │ Sensitivity  fewer misses ○──●── fewer false wakes
│ Disk 18 / 118 GB SSD  · WAL 3 MB     │   │   False wakes reported this week: 2  │
│ Mic  XVF3800 ok · not muted          │   │                                      │
│ HA   connected · 142 entities         │   │ Who's talking                        │
│ LLM  Bedrock ok · 540 ms TTFT p50    │   │ Everyone sounds distinct.            │
│                                      │   │ Household thresholds                 │
│ Latency, last 24 h (p50 / p95)       │   │   act       0.70   personal  0.82    │
│  fast path     0.44 / 0.61 s  ✓       │   │   ask on phone when unsure  [x]      │
│  no-tool       1.21 / 1.58 s  ⚠ p95   │   │ ▸ Details (τ, margin, model id,      │
│  tool turns    1.69 / 2.10 s  ⚠ p95   │   │   similarity grid)                   │
│  first audible 0.72 / 0.94 s  ✓       │   │                                      │
│  speaker id    0.11 / 0.17 s  ✓       │   │ Speech                               │
│ [Run bench]                          │   │ STT local zipformer · TTS Piper      │
└──────────────────────────────────────┘   │ Cloud speech  off  (v0.2)            │
                                           └──────────────────────────────────────┘
┌──────────────────────────────────────┐   ┌──────────────────────────────────────┐
│ ‹ System › Backups                   │   │ ‹ System › HTTPS                     │
│ ▲ Backups are only on this disk.     │   │ Tier 0 · https://beepboop.local      │
│   Add a USB drive or a network share │   │ Local certificate authority           │
│   so a dead SSD isn't a dead house.  │   │  fingerprint 3A 7F 09 C2 1B E4       │
│ Destinations                         │   │  [Download CA] [Install help by OS]   │
│  this disk   nightly 03:00 · ok      │   │ Admin gates (admins only, logged)     │
│  [+ Add USB path]  [+ Add SMB share] │   │  Home Assistant UI  :8443             │
│ Last backup  today 03:00 · 41 MB     │   │  Music Assistant UI :8445            │
│ Recovery key  saved at onboarding    │   │                                      │
│  [Show once more]  (disabled)        │   │ Upgrade to Tier 1 for passkeys, phone │
│           [Back up now]              │   │ notifications and external MCP:       │
└──────────────────────────────────────┘   │ ( ) Own domain + DNS-01               │
                                           │ ( ) Tailscale                         │
                                           │             [Check and switch]        │
                                           └──────────────────────────────────────┘
```

- **Health** numbers map 1:1 to `beepboop bench` and the M0 gates in [ROADMAP.md](ROADMAP.md#m0-prove-the-pi); thresholds colour the row. Fields and sources: [OPERATIONS.md §11](OPERATIONS.md#11-observability).
- **LLM**: the provider's settings form (region, write-only credentials, model, effort), cache hit rate, cost this month per profile ([LLM_PROVIDERS.md](LLM_PROVIDERS.md)). Budgets and the OpenAI-compatible form are v0.2.
- **Voice**: wake-word sensitivity and the count of turns reported as false wakes (nothing is recorded, so there are no clips to play); household thresholds per tier and *ask on phone when unsure* (numbers owned by [VOICE_PIPELINE.md §8.4](VOICE_PIPELINE.md#84-accept-rule-and-per-tier-thresholds)); speech providers. Per-person voice lives on People › `<name>` › Voice.
- **Backups**: destinations, schedule, last result, *Back up now*, recovery-key status (*Show once more* disabled after onboarding; a new key re-encrypts future backups), and the standing amber "only on this disk" state. Mechanics: [DATA_AND_PRIVACY.md §11](DATA_AND_PRIVACY.md#11-backups).
- **Updates** are **notify-only in v0.1**: current and available release, changelog link, and the host command `beepboop update` ([OPERATIONS.md §9](OPERATIONS.md#9-updates)). v0.2 invariant: the one-click button runs the same host procedure and streams progress over the `update` SSE event.
- **HTTPS**: tier, canonical URL, CA download and fingerprint, the admin-gated ports `:8443` (Home Assistant UI) and `:8445` (Music Assistant UI), which need an admin session and are logged as admin actions, and the Tier 1 switch (§7).
- **Logs**: last 500 structlog lines with `turn_id` links into Activity, download as JSON.

---

## 6. Authentication and sessions

### 6.1 Credentials

| Factor | Implementation | Notes |
|---|---|---|
| Password | argon2id `m=64 MiB, t=3, p=1` (target ≈300 ms on a Pi 4, validated in M0), `logins.pw_hash` | Min 12 characters, no composition rules, common-password list. |
| TOTP | RFC 6238 on the standard library's `hmac` (`beepboop.api.security`), SHA-1, 6 digits, 30 s step, ±1 drift, secret encrypted at rest, last-used counter blocks replay. Replacing the secret (`POST /api/auth/totp`) re-authenticates: the current password plus the current code or a recovery code, so a stolen or unattended session cannot swap the factor; changing the password signs out every other session. | **Required** for admin and member logins at every tier; optional for a child. |
| Recovery codes | 10 × 8-char, hashed, single-use | In the Recovery kit (§5.1); the only credential the post-onboarding claim session accepts. |
| Claim code | 6 digits, spoken + blinked, 15 min, 5 attempts | Only path to the first admin (§5.1). |
| Invite | 32-byte token in a link/QR, 24 h, single-use | Creates a login for an existing profile (§5.1b). |
| Passkeys | v0.2, Tier 1 only | Invariant: RP ID is the canonical domain; `.local` and IP RP IDs are rejected by browsers. |

Rate limits: 5 failed logins per username per 15 min → exponential backoff and an `audit` row; 10 per IP; 5 wrong TOTP codes → 5 min lock. Lockouts are stated honestly ("Too many tries. Try again in 4 minutes."); login responses never distinguish "no such user" from "wrong password".

### 6.2 Sessions and cookies

- Server-side sessions `sessions(id_hash, profile_id, created, last_seen, ua)`; the cookie is a 256-bit random token and only its SHA-256 is stored.
- Cookie `__Host-bb_session=<token>; Secure; HttpOnly; SameSite=Strict; Path=/`; `__Host-` pins it to this host and forbids `Domain`.
- Idle timeout 30 days (renewed on use), absolute 90 days; "Sign out everywhere" deletes all rows for the profile. The long session is what makes a 60-second approval realistic; the phone's lock screen is the accepted residual barrier.
- Cross-site top-level navigations do not carry a `Strict` cookie, so the SPA shell loads without one and then calls `/api/me` same-origin, which does; this is why the shell is static and the approve page works from any link.
- CSRF: non-GET `/api` requires `Sec-Fetch-Site: same-origin` **and** `Origin` exactly equal to `BB_PUBLIC_URL`; `same-site` is rejected. No CSRF tokens.
- The `claim` session (§5.1) is a separate cookie `__Host-bb_claim`, 15 min, deleted when the admin is created. `__Host-` forces `Path=/`, so the scoping is server-side: only `/api/onboarding/*` accepts it, and no other dependency reads it.

### 6.3 Authorisation in the API

Every request resolves to a `Principal` (`profile_id, via: web, confidence: None, device: web:<ua-hash>, scopes, tainted: false`). Web visibility is the §3 role table keyed on `profiles.kind`, implemented as one `web_scope(principal) -> set[str]` function. Tool calls from the web go through `policy.authorize` like every other door. There is no second permission system.

### 6.4 Visibility rules for data

| Data | Rule |
|---|---|
| Turns, tool calls, approvals | own; admin: every child's and guest's in full, other adults' tool calls without transcripts (§3) |
| Voiceprint metadata | own; admin: all. Raw embeddings: nobody, no endpoint exists |
| Secrets | nobody reads back; write-only, status only |
| Grants | own read; admin read/write |
| SSE stream | filtered with the same rules before events are written to the socket |

---

## 7. HTTPS tiers

Browsers require a secure context for `Secure` cookies, the clipboard, installable apps and WebAuthn, and `.local` names cannot get public certificates. So beepboop ships two tiers and is honest about what each unlocks (ADR [0013](adr/0013-https-tiers.md); Caddy configuration in [OPERATIONS.md §4](OPERATIONS.md#4-caddy)).

| | Tier 0 (default, v0.1) | Tier 1 (v0.2) |
|---|---|---|
| URL | `https://beepboop.local` (mDNS), also `https://<lan-ip>` | `https://home.example.com` or `https://beepboop.<tailnet>.ts.net` |
| Certificate | Caddy `tls internal`: device-generated local CA, leaf renewed automatically | Public cert via ACME **DNS-01** or `tailscale cert` |
| Browser trust | Warning until the CA root is installed on each device (`/ca` helper) | Trusted everywhere |
| Login | Password + TOTP | Password + TOTP + passkeys |
| Unlocks | Everything in v0.1 | Phone push confirm, external MCP clients at `/mcp`, OAuth bounce flows |
| Reachability | LAN only. Nothing is port-forwarded, ever | Still LAN or tailnet only |

**`/ca` install helper** shows the **fingerprint**, the first 6 byte-groups of the root certificate's SHA-256 (`3A 7F 09 C2 1B E4`), at the top; the device speaks the same six groups right after the claim code, so the user compares out of band before trusting anything (`beepboop ca fingerprint` prints it too). Below that, one install path detected from the user agent (iOS, Android, macOS, Windows, Linux, Firefox). The page works without a session and over the untrusted connection.

The canonical URL is `household.canonical_url` (`BB_PUBLIC_URL`): cookies, the CSRF origin check, redirects, QR codes and, later, OAuth `redirect_uri` and the `/mcp` resource identifier all derive from it.

Tier 1 invariants that bind v0.1: *Check and switch* changes only the canonical URL and Caddy site block, never the session model; ACME renewal honours ARI; the tab documents router DNS-rebind protection and Chrome Local Network Access as known gotchas; changing the canonical URL keeps sessions valid but invalidates passkeys. Detail: [ROADMAP.md v0.2](ROADMAP.md#v02-your-accounts-your-devices).

---

## 8. API conventions

One API, `/api`, served by FastAPI, documented at `/api/openapi.json` (and `/api/docs` for admins). The UI, the `beepboop` CLI and the test suite all use it; OpenAPI is the endpoint list. Internals (voice loop ↔ registry) are in-process and do not go through HTTP.

| Convention | Rule |
|---|---|
| Shape | JSON, `snake_case`, plural nouns (`/api/profiles/{id}`, `/api/turns/{id}`). No URL versioning; UI and server ship together, breaking changes bump `info.version`. |
| IDs, times | ULID strings as in the database (clients never construct them); ISO 8601 UTC with `Z`, millisecond precision. |
| Partial update | `PATCH` with JSON Merge Patch; `PUT` only for whole-value settings forms. |
| Errors | RFC 9457 `application/problem+json` `{type, title, status, detail, code, fields?}`; `code` is stable and translated by the UI, `detail` is for logs. |
| Status codes | 401 no session, 403 policy denied (body carries `reason`, `risk`, `ceiling`), 404 not visible to you (same as not existing), 409 state conflict, 422 validation, 429 with `Retry-After`. |
| Pagination | Cursor based: `?limit=50&cursor=…` → `{items, next_cursor}`. Never offsets. |
| Idempotency | Approvals by nonce; everything else is naturally idempotent (`PUT`) or not retried. |
| Write-only fields | Secrets are accepted on write and returned as `{"set": true}`. |
| Auth | Session cookie only. v0.2 invariant: `Authorization: Bearer <pat>` for the off-device CLI and `/mcp` builds the same `Principal`. |

**SSE: `GET /api/events`** (`text/event-stream`, `Last-Event-ID` resume, `: ping` every 15 s). Each event is filtered by §6.4 before it is written; a member's stream never carries another adult's `turn` events.

**Fake core for UI work.** `python -m beepboop.api.devserver [--seed]` serves the built SPA and the real `/api` (routers, policy, approvals) over a temporary SQLite file, plus a dev-only `/api/_dev` router (`voice`, `speech`, `clock`, `claim`) that plays the device. It is never part of `create_app` or the OpenAPI document; Playwright (`pnpm --dir ui test:e2e`) runs the onboarding and approval flows against it.

```
event: turn            data: {"id","profile_id","state":"listening|thinking|speaking|done|error","transcript?","reply_delta?","tool_call?"}
event: approval        data: {"id","nonce","summary","tool","exp","state":"pending|approved|denied|expired","factor?"}
event: inbox           data: {"count"}
event: device          data: {"state":"idle|listening|thinking|speaking|muted|pin|error|offline","last_speaker?"}
event: enroll          data: {"session_id","step","clip_quality":{"snr","clipping","speech_ratio","duration_ms"},"accepted":bool,"progress","total","done"}
event: health          data: {"temp_c","throttled","ram":{...},"ha":"ok|down","llm":"ok|down"}
event: update          data: {"phase":"snapshot|pull|health|rollback|done","detail"}
```

---

## 9. Forms from JSON Schema

`SchemaForm` is the one form engine: module `settings`, the LLM, Voice and HTTPS tabs (whose schemas are declared the same way in core) and the onboarding steps. Its dialect, the `x-*` keywords and the secrets rule are owned by [MODULES.md §6.1](MODULES.md#61-supported-json-schema-subset); nothing else is honoured.

Rendering: `title` is the label, `description` the help text, `default` pre-fills, `required` marks; `x-advanced` goes under the fold, `x-order` sorts, `x-per-profile` adds the field to *Your settings* with "use household default" first, `x-entity` renders a picker limited to granted entities and stores the UUID. Validation runs client-side with the same schema and server-side in core; server errors return as `fields` in the problem body and show inline. `beepboop module check` fails on any construct the renderer does not support, so a module never ships a form it cannot render.

---

## 10. Internationalisation readiness

- All UI strings live in `ui/messages/en.json`, referenced by key (a missing key is a build error); plurals and interpolation use ICU message syntax, never concatenation.
- The UI locale follows `profiles.locale` (falling back to the browser), the same field that selects speech models and the spoken persona. Dates, numbers and relative times use `Intl.*`; the API never formats text for display.
- Layout uses CSS logical properties and `dir="auto"` on user text so RTL locales need no layout work.
- Spoken copy that the UI also shows (fast-path acks, approval prompts, "still waiting") is the system-phrase table in [VOICE_PIPELINE.md §11.6](VOICE_PIPELINE.md#116-system-phrases-normative), kept in core's locale files, not in the UI, so voice and screen never disagree.

---

## 11. Component principles and tone of voice

### 11.1 Components

- **Tokens first.** Every colour, space, radius and font is a `--bb-*` token with light and dark values; contrast is checked in CI (≥ 4.5:1, ≥ 3:1 for large text and chrome).
- **Few, boring components.** Button, Field, Card, Banner, Chip (risk, status, speaker), Meter, Waterfall, Table, Dialog, Toast, EmptyState, SchemaForm. A new component needs a reason in the PR.
- **Risk chips** use the same four words everywhere: `read` (grey), `act` (blue), `personal` (purple), `critical` (red); the word is always printed, never colour alone.
- **Speaker chips, one rule everywhere**: name + percentage ("Sam · 88 %"), a hollow dot when below the threshold that applied. The percentage is never hidden, because trust in voice ID depends on seeing it; raw scores, margins and τ live under Details.
- **Loading** uses skeletons and an inline spinner in the pressed button; no full-page spinners. **Motion** ≤ 150 ms and respects `prefers-reduced-motion`. **Touch targets** ≥ 44×44 px for primary actions on phones.

### 11.2 Tone of voice (shared with the spoken persona)

beepboop is calm, plain and brief. It speaks as "I" and addresses the user as "you". It says what happened, why, and what to do next. It does not apologise reflexively, does not use exclamation marks, and does not pretend to be a person. Spoken system phrases are normative in [VOICE_PIPELINE.md §11.6](VOICE_PIPELINE.md#116-system-phrases-normative); the UI shows the same words.

| Do | Don't |
|---|---|
| "Unlock front door" (button copy names the exact effect) | "Confirm" / "OK" |
| "I couldn't reach Home Assistant. I'll keep trying; timers and lights still work." | "Oops! Something went wrong." |
| "This request expired. Say it again and I'll ask you once more." | "Error 410: approval nonce expired." |
| "Robin can't read calendars. Children can control things at home, not personal accounts. [Change in Access]" | "Access denied." |
| "I never keep recordings." | "Audio retention: disabled." |
| "3 new devices found. Nobody can use them until you decide." | "Pending entity registry entries: 3" |

Rules: sentence case; numbers as digits; times relative then absolute on hover; people by name, never by id; module names as humans say them ("Calendar") with the id in Details; never "simply", "just" or "easily"; destructive buttons say what is destroyed ("Delete Alex's voiceprint").

---

## 12. Open questions

1. [M0 measurement] The 250 kB JS budget and the < 1 s first paint are targets until measured on a mid-range phone against the Pi over Wi-Fi.
2. [v0.2] Whether the live "say anything, see the score" meter (§5.3) is worth an extra enrolment event on the socket.
