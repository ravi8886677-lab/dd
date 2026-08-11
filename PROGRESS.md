# Jarvis — session handoff

Written to be pasted into a fresh agent session. Repo:
`https://github.com/ravi8886677-lab/dd` (branch `main`, tip `8c3e1f9`).

`ravi8886677-lab/jarvis` is an upstream fork and is **not** the working
repo — do not push there.

Read `CLAUDE.md` first. It carries hard project rules (offline-first,
British English, TDD, spec files next to code, emoji CLI output) and
they have teeth.

---

## What this is

A privacy-first personal assistant: persistent memory across sessions, a
tool-using reply engine, MCP integration, a web dashboard, computer
control, and (on real hardware) voice.

The work since the last handoff has been almost entirely the MCP layer
and the security around it, plus the approval model.

---

## State: what works, and how confident to be

**Verified** means observed working in a container — a headless Linux
box with no display, no microphone, and no working OS keyring.

| Area | State |
|------|-------|
| Text chat (`python -m jarvis.chat`) | ✅ Verified |
| Dashboard chat + orb | ✅ Driven headlessly |
| Memory (diary + knowledge graph) | ✅ Facts recalled across processes |
| Dashboard auth (token + Host check) | ✅ Verified against a live server |
| MCP over **stdio** | ✅ Real `@modelcontextprotocol/server-everything`, 13 tools, real call |
| MCP over **Streamable HTTP** | ✅ Same server in `streamableHttp` mode, 13 tools, real call |
| Supply-chain pinning | ✅ Refused unpinned, allowed pinned, on a real launch |
| Tool fingerprinting (rug pull) | ✅ Real poisoned server withheld |
| Local audit | ✅ Flags hidden chars, prompt markup, credential paths, shadowing |
| YOLO gate | ✅ Real server not called with it off; called with it on |
| Dashboard YOLO slider | ✅ Driven against a live server, 5 min → 8h |
| Settings tab writing a fresh config | ✅ From no config file at all |
| Connections directory + one-click add | ✅ Every catalogue entry added and re-checked against the spawn-time guard |
| MCP registry browse / refresh / add | ✅ Against the live registry: 63 servers cached, a real entry installed and its pinned launch accepted |
| Dashboard rate limits | ✅ Token guessing and MCP writes both lock out and recover |
| `fetchWebPage` SSRF guard | ✅ Refuses loopback, private, link-local and inward redirects before the request |
| RFC 9207 `iss` validation | ✅ Unit-tested; a real provider still needs R0.4 |
| `/yolo` in the text chat | ✅ Grants, revokes and reports without touching the assistant |
| **Credential store on a real OS** | ❌ **Never** — this box's keyring is broken; only failure paths tested |
| **OAuth against a real provider** | ❌ **Never** — unit-tested only |
| **The re-pin migration on a real install** | ❌ Only a synthetic config |
| Voice / wake word / TTS | ❌ Never run — no microphone |
| `computerUse` actually clicking | ❌ Never run — no display |
| Desktop tray app, orb on a screen | ❌ Never run — no display |

**The honest summary:** the MCP layer is exercised against real servers
over both transports, and the dashboard work above is driven against a
live Flask instance and the live public registry. What has never touched
real hardware is everything that needs a keychain, a browser redirect, a
microphone or a screen — and two of those (credential migration, server
re-pinning) run on *every existing user's first launch after upgrade*.

Everything on the old backlog below P0 is done. What is left is the part
no container can do.

---

## Running it

```bash
git clone https://github.com/ravi8886677-lab/dd.git && cd dd
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-chat.txt
pip install flask psutil          # dashboard
```

```bash
PYTHONPATH=src python -m jarvis.chat                            # text chat
PYTHONPATH=src:. python src/desktop_app/memory_viewer.py 5071   # dashboard
PYTHONPATH=src python -m jarvis.daemon                          # voice (needs a mic)
PYTHONPATH=src python -m jarvis.mcp_trust_cli audit             # audit MCP tool definitions
```

Config is at `~/.config/jarvis/config.json` (mode 0600), or set it
entirely from the dashboard's **Settings** tab — verified working from
no config file at all. API keys move into the OS credential store on
first load and are blanked from the file.

The dashboard prints a URL carrying a per-launch token. Plain
`localhost:5071` returns 401 by design.

---

## The security model, in one page

Full detail in `src/jarvis/tools/external/mcp_security.spec.md`. Four
independent defences at four moments:

| Moment | Module | Question |
|--------|--------|----------|
| Before the subprocess starts | `mcp_supply_chain.py` | Is this code pinned, or whatever the registry serves today? |
| At tool discovery | `mcp_trust.py` | Is this the same tool the user accepted? |
| Before a tool runs | `mcp_gate.py` + `approval.py` | Is YOLO mode on? |
| On demand | `mcp_audit.py` | Do any definitions carry the shape of a known attack? |

**The one rule that must not be broken:** nothing in the tool layer may
call `approval.grant`. Jarvis reads web pages, MCP tool descriptions and
tool results, any of which carry text other people wrote. If a tool
could grant YOLO, that text could grant it. `tests/test_yolo.py` asserts
no built-in tool and no part of the registry can reach it. Keep that
test passing.

Credentials live in the OS keychain (`jarvis.utils.secret_store`), never
`config.json`. A key only leaves the file once the store has handed the
same value back, and a write that cannot be verified is rolled back so
no orphan copy is left behind.

---

## Outstanding work

Ranked. See `REQUIREMENTS.md` for the full backlog with acceptance
criteria.

1. **Run the smoke test on real hardware.** This is the whole list now.
   Eight checks, about an hour, in `REQUIREMENTS.md`. Two of them cover
   code that runs for every existing user on upgrade.
2. **Audience restriction of OAuth access tokens.** `iss` is validated;
   the audience claim is not, because the pinned SDK does not surface
   it. A token issued for one resource server is not refused when
   presented to another. Recorded in the security spec's non-goals.
3. **`test_one_shot_puts_only_the_answer_on_stdout` is rarely flaky.**
   Seen once in roughly thirteen full-suite runs, never reproduced
   since. The one-shot path swaps `sys.stdout` process-wide, so a print
   from any thread during that window lands in the captured answer. Not
   diagnosed; the exact-equality assertion is correct and worth keeping.

---

## Gotchas that cost time

- **Dashboard browser code belongs under `desktop_app/dashboard/`.** Keep
  JavaScript, CSS and Jinja markup in their native files so browser tooling
  can parse them and Python never interprets their escape sequences.
- **`app.py` is the PyInstaller entry point** and runs as `__main__`
  with no package context. A relative import there raises ImportError at
  launch. `tests/test_desktop_app.py` guards this — it has already
  caught one.
- **`StdioServerParameters(env=None)` does not inherit the environment.**
  The SDK substitutes a four-variable default (HOME, PATH, SHELL, TERM),
  which starves npx of proxy settings and CA paths and hangs the launch
  with nothing on stderr. Always build the env explicitly.
- **The suite reaches this package as both `jarvis.*` and `src.jarvis.*`**,
  which are separate `sys.modules` entries with separate module-level
  state. A fixture clearing one does not clear the other. Reference the
  module the code under test actually holds.
- **Grep `^FAILED` alone misses collection errors.** Compare
  `^(FAILED|ERROR)` or a whole broken test file looks like a pass.
- **Chromium blocks port 5060** (`ERR_UNSAFE_PORT`, the SIP port). Use
  5071+.
- **Gemini free tier is tight.** `gemini-3.6-flash` returns
  `RESOURCE_EXHAUSTED` at ~20 requests and each turn costs 3-4 calls;
  `gemini-3.1-flash-lite` has headroom. The symptom of running out is
  that memory writes fail *silently*, because the summariser swallows
  errors and returns None.
- **Qt offscreen rendering works** (`QT_QPA_PLATFORM=offscreen`), which
  is how the dashboard and Qt code were exercised without a display.

---

## Tests

`33 failed, 2623 passed, 33 skipped` on a headless container with
`pytest`, `requests`, `beautifulsoup4`, `pyperclip`, `pillow`, `flask`
and `PyQt6` installed. **All 33 are missing audio or display
dependencies, not defects** — 16 in `test_dictation.py`, 7 in
`test_voice_listener.py`, 5 in `test_desktop_app.py`, 4 in
`test_portaudio_serialisation.py`, 1 in `test_llm_thinking.py`.

```bash
pip install -r requirements-dev.txt flask psutil
QT_QPA_PLATFORM=offscreen PYTHONPATH=src:. python -m pytest tests/ -q
```

`pynput` installs but raises on *import* without an X server, so the
dictation tests cannot run headless at all. `pyautogui` needs X11 headers
to build on Linux. PyQt6 additionally needs `libegl1` from apt, or every
`desktop_app` import fails with `libEGL.so.1: cannot open shared object
file`. On a laptop with a screen they all work.

The two order-dependent 429 tests are fixed: they were patching
`module.time.sleep`, which resolves to the shared `time` module and
swapped sleep for the whole process, so any other thread's sleep landed
in the mock and the backoff assertion became a race. They now patch a
per-module seam (`jarvis.utils.backoff`).

Still diff failures against a baseline rather than reading the raw
count. The quickest honest baseline is a worktree at the last known-good
commit:

```bash
git worktree add /tmp/base <commit> && cd /tmp/base && python -m pytest tests/ -q
```

---

## Design decisions worth not undoing

**YOLO is granted by a human, never by a tool.** Discussed above. It is
the single property the whole approval model rests on.

**Per-action confirmation codes were removed deliberately.** They were
drawn to a hidden Log Viewer window in packaged builds, so the
`computerUse` gate was never openable in any shipped build. They were
also tedious. The property they protected survives in YOLO; the friction
does not.

**MCP tool schemas are passed through untouched.** Jarvis used to inject
a `confirmation_code` property and strip it before the call, which ate
the argument of any server that legitimately had one.

**`openApp` takes no command string.** The model picks from a closed set
or gives an http(s) URL. Jarvis reads attacker-controlled web pages; a
`command` field turns "ignore previous instructions and run…" into code
execution. A test asserts the schema has no command-shaped field.

**The dashboard fetches nothing external.** No CDN webfonts. It renders
the user's diary; a font request would disclose their IP on a product
whose headline claim is "100% local". Guarded by
`tests/test_memory_viewer_offline.py`.

**The Host check is not authentication.** It is the DNS-rebinding
defence, and it stays on even when `JARVIS_DASHBOARD_NO_AUTH` is set.
Without it any website the user visits can drive the dashboard from
their own browser, including `POST /api/mcp`, which registers a command
Jarvis later spawns.

**Web search reports failure honestly.** A dead search returns an
envelope telling the model to say so and invent nothing.

---

## Credentials

Two GitHub PATs and a Gemini key were pasted into an originating chat
session earlier in this project's history and should be treated as
compromised.
