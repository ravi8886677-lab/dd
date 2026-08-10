# Jarvis — outstanding requirements

Companion to `PROGRESS.md`. That file says where things stand; this one
says what is left, in the order it matters, with a way to tell when each
is done.

Written for an agent picking the work up cold. Every item states what
"finished" looks like, because several of the things on this list can be
made to *look* finished without being it.

---

## P0 — Verify on real hardware

Nothing on this list needs code written. It needs someone to run the
software on a machine with a screen, a keychain and a browser, because
none of it has ever run on one. Two of these paths execute for every
existing user on their first launch after upgrading.

**Run all of it from an installed artifact, not a dev checkout.** Most of
what is untested is packaging-specific: `npx.cmd` resolution, the
login-shell PATH fallback, keyring access, tray behaviour, PyInstaller
imports. A dev-checkout pass proves nothing about the thing a user runs.

### R0.1 — Credential migration on a real OS 🔴

The highest risk item, because it touches the user's only copy of their
API key.

- Start with a real key in `config.json`, launch, send one message.
- **Done when:** `llm_api_key` in `config.json` is empty, the key is
  visible in Keychain Access / Credential Manager under service
  `jarvis`, and Jarvis still reaches the LLM.
- **Then test the failure deliberately:** deny the keychain prompt
  mid-migration. **Done when:** the key is *still in `config.json`* and
  *no entry was left behind* in the keychain. Both halves matter — the
  plaintext surviving is the anti-data-loss property, and the absent
  orphan is the anti-stale-key property.

### R0.2 — Server re-pinning on a real existing install 🔴

- Take a `config.json` from an older install with wizard-added MCP
  servers (unpinned `@latest` args).
- **Done when:** `📌 Pinned MCP server …` lines appear on startup and
  **every server still starts**. If this is wrong, a user loses their
  entire MCP tool set behind one error line each.

### R0.3 — The dashboard on a real screen 🟠

Kimi's UI polish and the CSS fixes have only ever been checked by tests
and static analysis.

- **Done when:** Connections tab borders are visible, a failed server's
  red pill is clearly distinct from a working green one, the orb shrinks
  while Jarvis speaks, and the YOLO slider drags smoothly and shows a
  live countdown once started.

### R0.4 — OAuth against a real provider 🟠

- Configure a hosted MCP server with `"auth": "oauth"`, click Connect.
- **Done when:** the browser opens at the provider, approval stores a
  token, and Jarvis reconnects without re-prompting.
- **A restart is not a refresh test.** If the access token is still
  valid, restarting only proves it was re-read. Force it: shorten the
  expiry server-side or expire the stored token by hand, *then* restart.

### R0.5 — Real catalogue servers 🟡

Only `@modelcontextprotocol/server-everything` has been driven.

- **Done when:** `chrome-devtools`, `macos-automator` and `whatsapp`
  (the uvx path) each start and list tools at their pinned versions.

### R0.6 — YOLO in a real conversation 🟡

- **Done when:** with YOLO off, asking for something destructive gets a
  refusal naming the action; turning it on from the tray or the
  dashboard slider makes the same request succeed; and it stops working
  again when the window lapses.

---

## P1 — The Connections tab

The dashboard's MCP tab is a config editor: three text boxes for name,
command and args. Since pinning became mandatory, typing a package
without an exact version produces a config the launcher then refuses —
so the tab actively steers people into a broken state.

**The dashboard never sees `mcp_catalogue.py` at all.** It is wired only
into the Qt setup wizard and settings window.

### R1.1 — Serve the catalogue

- New endpoint returning the nine curated entries.
- **Done when:** the dashboard can render name, emoji icon, description,
  category and whether an API key is needed, without importing
  `desktop_app` (which pulls in Qt — see `_load_catalogue_by_name` in
  `config.py` for the file-loading pattern that avoids it).

### R1.2 — Directory grid with one-click add

- **Done when:** clicking Add writes the catalogue's **pinned** args, so
  a user cannot create an entry the launcher will refuse. The manual
  three-box form is demoted to an "Advanced" disclosure. Entries needing
  a key get an inline field using `api_key_hint`.

### Explicitly not wanted

**No "✓ verified" badge.** On other directories that tick means the
vendor vetted the server. Jarvis does not vet anything, and the whole
security layer exists because a server that *looks* fine may not be. If
a badge is wanted it must mean something checkable — "pinned version",
"in the curated catalogue" — and be labelled as that.

---

## P2 — MCP Registry

Nine hardcoded entries is not a directory. `registry.modelcontextprotocol.io`
is a metadata-only API (no auth needed for reads) that would replace them.

- **Done when:** the dashboard lists servers from the registry, cached
  locally so it works offline, with namespace verification surfaced as
  the trust signal — and **labelled clearly as proof of namespace
  ownership, not safety**. Registry inclusion is not vetting.
- Keep the curated catalogue as the default view.

---

## P3 — Security gaps that are known and open

### R3.1 — SSRF guard on `fetchWebPage`

It has no loopback or private-IP blocking, unlike `webSearch`. Not
currently a privilege-escalation path — the dashboard token is not in
the daemon's environment and `/api/yolo` grants only on POST — but it
should not rest on that.

- **Done when:** `fetchWebPage` refuses loopback, link-local and private
  addresses, with a test covering each.

### R3.2 — RFC 9207 `iss` validation and audience checks

Belongs to the 2026 MCP spec revision. The pinned SDK's OAuth provider
does not implement them, so a malicious *authorisation server* response
is not fully defended against. Recorded in the spec's non-goals.

- **Done when:** either the SDK gains them and Jarvis picks them up, or
  Jarvis validates `iss` itself.

### R3.3 — Dashboard rate limiting

`/api/mcp` remains a code-execution path gated only by the session
token. No rate limiting anywhere.

---

## P4 — Housekeeping

- **Flaky tests.** `test_piper_tts::test_429_retried_then_succeeds` and
  `test_voice_listener::test_429_gives_up_after_max_retries` pass in
  isolation and fail depending on order. They caused a false alarm this
  session. **Done when:** they pass in any order.
- **Duplicate test classes.** `tests/tools/builtin/test_computer_use.py`
  has two near-identical coordinate-validation classes
  (`TestCoordinatesAreValidated`, `TestCoordinatesAreChecked`) that
  predate this work. Harmless, but one should go.
- **Missing optional deps** are the entire 45-failure baseline. Listing
  them in a `requirements-dev.txt` would stop every future agent having
  to rediscover that the suite is not actually broken.
- **`jarvis.chat` has no YOLO control.** The tray and dashboard can
  grant; a plain terminal session cannot. A `/yolo 30` REPL command
  would close that, and `chat.spec.md` would need updating with it.

---

## Rules that constrain any of the above

From `CLAUDE.md`, and they are not negotiable:

- **Offline-first.** No integration that requires shipping user data to
  a specific vendor's cloud, not even opt-in. A backend is acceptable
  only if it can run fully locally. This already ruled out `mcp-scan`
  (now Snyk's, analysis only runs against their hosted server) and
  Composio.
- **No hardcoded language patterns.** The assistant must work in any
  language. This is why `mcp_audit.py` checks structure — invisible
  characters, prompt-shaped markup, credential paths — and never English
  phrases, and why ZWNJ/ZWJ are exempt (they are orthographically
  required in Persian, Hindi, Bengali and Malayalam, and join every
  multi-person emoji).
- **TDD, and tests verify behaviour not implementation.** Assert against
  what the system does, not mock call counts.
- **Spec files live next to the code** and describe the present, never
  the history. Update `CLAUDE.md`'s Spec File Registry when adding one.
- **British English**, emoji CLI output with indentation, `debug_log` at
  the load-bearing points.
- **Conventional Commits.** Default branch is `main` in this fork.
