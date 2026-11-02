# Spec review log — 2026-11-01

Six reviewers (minimalism MIN, security SEC, extensibility/DX MOD, Pi/ops OPS, UX, consistency CON) filed 112 findings. The judge spot-checked them against the docs and confirmed the main contradictions. These include: the socket path and listener direction (ARCH vs VOICE vs OPS); two PIN designs (IDENTITY `pin.captured` vs ADR 0014 `pin.result`); "Is this Alex?" against IDENTITY's second-factor rule; duplicate DDL (`kids`/`children`, `inbox` vs `ha_entities.status`, core `timers`/`lists` tables against "modules never own tables"); double authorize in `PolicyHA`; the handler signature (CLAUDE/ADR 0007 vs MODULES); `x-ui` vs `x-*` and `test_connection`; a 12 h session against the "30-day session" approval promise; and `docker-compose.yml` vs `compose.yaml`.

Rulings favour one owner per fact, links instead of restatement, and cutting v0.1 surface. Specific cuts: the module event bus, the `system` principal, `min_confidence`/`kind`, three context classes, `VadProvider`, optional HA companion containers, and the live meter. Security fixes were accepted where they close real gaps: the plaintext DEKs in the pre-update snapshot, default-deny inbound, re-claim, test-as, the approval state machine, taint as conversation state, the egress allowlist, CA name constraints and cookie stripping.

Duplicate findings were merged into judge items J01–J17 plus themed items, and each item is split by writer group. Two product questions go to the user.

## Accepted

| Item | Lens(es) | Summary |
|---|---|---|
| J01 | MIN-1, CON-1, CON-2, SEC-11, OPS-6 | ARCH §4 is the only event protocol; `audio.sock`, audio listens; audio verifies the PIN (ADR 0014), lengths 4–8, `pin_hash` encrypted |
| J02 | MIN-2, SEC-12, CON-15 | No module event bus in v0.1; `ctx.announce` + `ctx.spawn`; `ctx.ha` read-only outside tool calls |
| J03 | MIN-3, CON-8 | One `HAClient` (MODULES §5.3); client in core; authorize + audit once, in `registry.call()` |
| J04 | MIN-4, CON-3, CON-15, MIN-16 | DATA §6 is the only DDL; groups `children`; `ha_entities.status` is the inbox; timers/lists in kv; one rate limiter |
| J05 | MIN-5, CON-9, CON-11 | One config file + DB rule; delete `audio.yaml`; LLM config in DB |
| J06 | MIN-6 | Drop `min_confidence`, `kind`, `prompt_block.position` |
| J07 | MIN-7 | Single `ToolContext`; defer `ctx.llm`, OAuth tokens, kv TTL |
| J08 | MIN-8, SEC-6/7/16/20, MOD-6 | v0.2+ sections reduced to invariants (external door tainted, bounce-page constraints, consent hosts, audio provider config file) |
| J09 | MIN-9, UX-10, CON-14 | v0.1 scope trimmed; Babysitter becomes "Guest access" grants with expiry; `persona.set` is `act` |
| J10 | MIN-10, CON-20 | One parental-visibility rule (parent = admin); drop the `activity` grant |
| J11 | MIN-11, SEC-1, OPS-12 | DATA §11 owns backups; no plaintext DEKs in the pre-update snapshot; staggered jobs; weekly test-restore |
| J12 | MIN-12, CON-11 | LLM §8 is the only prompt layout; protocols defined once |
| J13 | MIN-13, CON-10, SEC-18 | Reply window (8 s) vs conversation window (90 s, same speaker only); ADR 0015 owns the latency table |
| J14 | MIN-14, MOD-9, MOD-10, MOD-15 | Single module procedure, `fake_context`, template test, `deploy/compose.yaml` |
| J15 | MIN-15 | Delete resolved open questions; tag the rest by milestone |
| J16 | MIN-17 | Remove `VadProvider`, whisper fallback, MQTT/Z-Wave/Matter profiles, `beepboop evals` |
| J17 | MIN-18 | Glossary: definitions only |
| SEC-2 | SEC | Default-deny inbound firewall |
| SEC-3 | SEC | Re-claim never creates or resets an admin without a recovery code or factory reset |
| SEC-4 | SEC-4, UX-5, CON-4 | Personal tier below τ_strict uses a second factor; VOICE §8.4 owns the numbers; ADR 0005 amended |
| SEC-5 | SEC | Test-as limited to child/guest, forced dry-run |
| SEC-6 | SEC | Approval state machine; pure `authorize()`; `/approvals/<id>` |
| SEC-8 | SEC | Typed HA attribute allowlist; `untrusted_output` required |
| SEC-9 | SEC-9, CON-6 | One HA service table; `input_*`/button/select/template switches are indirect; `notify` blocked |
| SEC-10 | SEC | Name-constrained local CA; only root.crt mounted; fingerprint check |
| SEC-13 | SEC | Taint is conversation state; fast path exempt |
| SEC-14 | SEC, CON-20 | Strip Cookie to HA/MA; strict Origin; one forward_auth form |
| SEC-15 | SEC | `ctx.http` egress allowlist; honest SECURITY module row |
| SEC-17 | SEC | Honest storage-theft scope |
| SEC-19 | SEC | Critical audit pseudonymised, kept 365 days |
| MOD-1 | DX | Handler `(ctx, **args) -> dict`; ADR 0007 amended |
| MOD-2 | DX | HA-backed module section (weather example) |
| MOD-3 | DX | Overlap guidance + lint L16 |
| MOD-4 | DX | What the model sees on deny/confirm; template fixes |
| MOD-5 | DX | VOICE §17 owns audio protocols; CLAUDE §7.1 for audio providers |
| MOD-7 | DX, UX-8, CON-16, CON-7 | MODULES §6.1 dialect; Test calls `health()`; canonical caldav tool names; doc-lint rule |
| MOD-8 | DX | kv scoping rule |
| MOD-11 | DX | `read_guide` loads tools; slow-path only |
| MOD-13 | DX | Reference AGENT.md fixes |
| MOD-14 | DX | M0 task table with hardware gating |
| MOD-16/17 | DX | Nullable outputs; honest rubric automation |
| OPS-gates | OPS-1/2/3/7/9/10/15/16, CON-13 | Clock zero, p50/p90 rule, ROADMAP gates as the single source, thread table, cold-cache scenario, bench names, safe CI runner |
| OPS-4/18 | OPS, CON-19 | Host-side CLI subcommands, host.json, complete CLI registry |
| OPS-5 | OPS | HA-safe update rollback |
| OPS-8/11/13/14/17 | OPS | cgroups, logging, watchdog, USB power, preflight |
| CON-5 | CON | README trust claims corrected |
| CON-9 | CON, OPS-6 | OPERATIONS §3 is the canonical deploy shape |
| CON-12 | CON | One fast-path intent table |
| CON-20 | CON | Remaining writer rulings (images, domains, default voice, external HA out of scope) |
| UX-1 | UX-1, CON-18 | TOTP required; 30-day sessions; one-time 120 s extension; one approvals transport |
| UX-2 | UX | "Your home" onboarding step |
| UX-3 | UX | Normative system-phrase table (VOICE §11.6) |
| UX-4 | UX | Effect-labelled one-tap approve; separate "That wasn't me" |
| UX-6/7 | UX | Six-area IA; WEB_UI §3 owns screen names |
| UX-9 | UX | Invite acceptance + practice approval |
| UX-11/12/17 | UX | Percent chips, plain-sentence similarity, friendlier enrolment, status dot matches the LED |
| UX-14 | UX | Inbox radios + defaults |
| UX-15 | UX | One "That wasn't me" definition |
| UX-16 | UX | Shorter onboarding; Recovery kit |

## Rejected

| ID | Lens | Summary | Reason |
|---|---|---|---|
| SEC-21 | SEC | Document the `activity` grant type | The type is removed instead (J10) |
| MOD-12 | DX | Event fan-out semantics | Moot: no module bus in v0.1 |
| UX-13 | UX | Guardianship grant for co-parents | One kind-based rule; non-admin guardians come in v0.3 |
| MOD-2 (part) | DX | Doorbell template, `camera.*` tier | Needs v0.3 proactive announcements; cameras out of scope |
| MOD-3 (part) | DX | `claims:` field | No v0.1 user |
| SEC-8 (part) | SEC | Other members' list items untrusted | Household members are principals; too much friction |
| MIN-16 (part) | MIN | Defer `ha_config_entry` grants | Free in the schema; listed in ADR 0005 |
| CON-14 (part) | CON | Live meter in v0.1 | Overruled by MIN-9 |
| CON-15 (part) | CON | `system` on Principal.via | Moot after J02 |
| MIN-15 (part) | MIN | New OPEN_QUESTIONS.md | Conflicts with parallel revision; tag in place |
| OPS-12 (part) | OPS | Stop HA for nightly backup | Disruptive; the update snapshot covers it |
| OPS-13 (part) | OPS | fstrim/SMART on the health page | fstrim is on by default; SMART over USB is unreliable |
| OPS-14 (part) | OPS | Doctor stress preflight | Covered by the V4 variant |
| UX-15 (part) | UX | Undo offer in the reply window | New mechanism; toast instead |
| UX-16 (part) | UX | "What's the code?" intent | Button re-speaks the code |
| MOD-13 (part) | DX | Run AGENT.md examples in the harness | Brittle; rubric rule instead |
| SEC-17 (part) | SEC | LUKS on by default | ADR 0011 makes it optional |
| SEC-7 (part) | SEC | Per-client `act` toggle now | v0.2; invariant only |

## Needs user

| ID | Lens | Question | Recommendation |
|---|---|---|---|
| CON-17 | CON | Does "No audio kept" allow opt-in encrypted exceptions (enrollment clips, false-wake queue, debug retention)? | Keep it absolute in v0.1; revisit the false-wake queue in v0.2 via an ADR |
| MIN-19 | MIN | Adopt a size rule (one owner per fact; v0.2+ as invariants; ~5k words per topic doc, agent-teaching docs exempt)? | Accept; this review already applies the first two parts |
