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
| **Credential store on a real OS** | ❌ **Never** — this box's keyring is broken; only failure paths tested |
| **OAuth against a real provider** | ❌ **Never** — unit-tested only |
| **The re-pin migration on a real install** | ❌ Only a synthetic config |
| Voice / wake word / TTS | ❌ Never run — no microphone |
| `computerUse` actually clicking | ❌ Never run — no display |
| Desktop tray app, orb on a screen | ❌ Never run — no display |

**The honest summary:** the MCP layer is now exercised against real
servers over both transports. What has never touched real hardware is
everything that needs a keychain, a browser redirect, a microphone or a
screen — and two of those (credential migration, server re-pinning) run
on *every existing user's first launch after upgrade*.

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

1. **Run the smoke test on real hardware.** Nothing else on this list
   matters as much. Six checks, about an hour, listed in
   `REQUIREMENTS.md`. Two of them cover code that runs for every
   existing user on upgrade.
2. **Connections tab redesign.** The dashboard still makes you hand-type
   `npx` and an exact version. The catalogue (`mcp_catalogue.py`) has
   everything a directory UI needs — icon, description, category,
   `needs_api_key` — and the dashboard never sees it.
3. **MCP Registry.** Nine hardcoded catalogue entries. The official
   registry at `registry.modelcontextprotocol.io` is a metadata API that
   would replace them. Not started.
4. **RFC 9207 `iss` validation and audience checks** for OAuth. Belongs
   to the 2026 spec revision; the pinned SDK does not implement them.
   Recorded in the spec's non-goals.
5. **No SSRF guard on `fetchWebPage`.** It has no loopback or private-IP
   blocking, unlike `webSearch`. Not currently exploitable for privilege
   escalation (the dashboard token is not in the daemon's environment,
   and `/api/yolo` grants only on POST) but it should be closed.

---

## Gotchas that cost time

- **The dashboard HTML is inside a Python triple-quoted string** in
  `memory_viewer.py` (~237 KB). A JS `"\n"` is eaten by Python and
  breaks the file at parse time. Write `"\\n"`.
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

`45 failed, 2506 passed, 33 skipped` is the baseline in a bare headless
checkout. **All 45 are missing optional dependencies, not defects.**

```bash
pip install -r requirements-dev.txt
QT_QPA_PLATFORM=offscreen PYTHONPATH=src:. python -m pytest tests/ -q
```

Measured here: adding just `beautifulsoup4` and `pyperclip` takes it
from 45 to 33. The remaining 33 need a display or an audio device —
`pynput` installs but raises on *import* without an X server, and
`pyautogui` needs X11 headers to build on Linux. On a laptop with a
screen they all work.

Two tests are genuinely flaky and order-dependent, passing in isolation:
`test_piper_tts::test_429_retried_then_succeeds` and
`test_voice_listener::test_429_gives_up_after_max_retries`. They cost a
false alarm during this session. Diff failures against a baseline rather
than reading the raw count.

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
