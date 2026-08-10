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
- **This now also exercises RFC 9207 `iss` validation**, which has only
  been unit-tested. `verify_issuer` reads the issuer out of the SDK's
  discovered metadata, and that read reaches into SDK internals. It is
  written to degrade to "cannot verify" rather than refuse, but a real
  provider is the only thing that proves the happy path still connects.
  **Done when:** a real provider authorises successfully, and the debug
  log does not show `iss unverified` for a provider that publishes an
  issuer (which would mean the metadata read silently found nothing).

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

### R0.7 — The connections directory on a real screen 🟠

The Connections tab is now a directory rather than a config editor, and
none of it has been seen on a display.

- **Done when:** the curated grid renders as tiles, an entry needing a key
  shows its inline field and hint, clicking Add flips the tile to Added,
  and the added server then appears under Configured with a tool count.

### R0.8 — Registry browse and refresh 🟡

Driven headlessly against the live registry, never on a screen.

- **Done when:** opening the tab shows the cached listing with its fetch
  time and makes no network request; Refresh fetches and updates that
  time; the filter box narrows the grid; and a server with no pinned
  package shows "not installable" rather than an Add button.
- **Then pull the network out and reopen the tab.** **Done when:** the
  cached listing still renders and Refresh reports it could not reach the
  registry, rather than emptying the directory.

---

## P1 — Audience restriction of OAuth access tokens

`iss` is validated (RFC 9207), so a mix-up attack is refused. The audience
claim is not checked, because the pinned SDK does not surface it: a token
issued for one resource server is not refused when presented to another.

- **Done when:** either the SDK exposes the claim and Jarvis checks it, or
  Jarvis decodes the token's `aud` itself and refuses a mismatch.
- Recorded in `mcp_security.spec.md` non-goals until then.

---

## P2 — A rarely flaky test

`test_text_chat.py::test_one_shot_puts_only_the_answer_on_stdout` failed
once in roughly thirteen full-suite runs and has not reproduced since.

The one-shot path swaps `sys.stdout` process-wide so the engine's
narration goes to stderr, which means a print from any other thread during
that window lands in the captured answer. That is the likely mechanism, but
it was not confirmed.

- **Do not weaken the assertion.** Exact equality is the point: the README
  advertises `answer=$(python -m jarvis.chat "…")`, and anything extra on
  stdout ends up inside the caller's variable.
- **Done when:** the cause is identified and either the leaking print is
  stopped or the redirect is made thread-safe.

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
