# Jarvis — session handoff

Written to be pasted into a fresh Claude session. Repo:
`https://github.com/ravi8886677-lab/dd` (branch `main`).

`ravi8886677-lab/jarvis` is an upstream fork and is **not** the working
repo — do not push there.

---

## What this is

Jarvis is a privacy-first personal assistant: persistent memory across
sessions, a tool-using reply engine, and (on real hardware) voice.
Upstream is voice-only; this branch added a text front end, a web
dashboard, and computer control.

Read `CLAUDE.md` first — it carries hard project rules (offline-first,
British English, TDD, spec files, emoji CLI output). Several of them
have teeth, and at least one was breached below.

---

## State: what works, and how confident to be

Verified means "observed working in this container".

| Area | State |
|------|-------|
| Text chat (`python -m jarvis.chat`) | ✅ Verified end to end |
| Dashboard chat + orb | ✅ Driven in a headless browser |
| Memory (diary + knowledge graph) | ✅ Facts recalled across separate processes |
| Dashboard auth (token + Host check) | ✅ Tested |
| MCP Connections tab | ✅ Endpoints tested; never tried a real MCP server |
| Settings tab (provider/key) | ✅ Live model list fetched from Gemini |
| `openApp` | ✅ "play a Hindi song on YouTube" routed and opened |
| `computerUse` gate | ✅ Proposal/approval flow verified |
| `computerUse` actually clicking | ❌ **Never run** — no display here |
| Voice / wake word / TTS | ❌ **Never run** — no microphone here |
| Desktop tray app | ❌ Never run — no display |
| Orb on a real screen | ❌ Only offscreen renders |

**The honest summary:** roughly half the surface has never been observed
working, and it is the half a client would judge first. Everything in
that column failed for the same reason — this container has no display,
no `/dev/snd`, no input devices.

---

## Running it

```bash
git clone https://github.com/ravi8886677-lab/dd.git && cd dd
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-chat.txt
pip install flask psutil          # dashboard
```

Config lives at `~/.config/jarvis/config.json` (mode 0600):

```json
{
  "llm_provider": "openai_compatible",
  "llm_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
  "llm_api_key": "YOUR_KEY",
  "llm_chat_model": "gemini-3.1-flash-lite",
  "fast_model": "gemini-3.1-flash-lite",
  "embedding_provider": "openai_compatible",
  "embedding_model": "gemini-embedding-001",
  "computer_use_confirm": "risky"
}
```

```bash
PYTHONPATH=src python -m jarvis.chat                      # text chat
PYTHONPATH=src python -m jarvis.chat "what's the time?"   # one-shot, stdout is the answer alone
PYTHONPATH=src:. python src/desktop_app/memory_viewer.py 5071   # dashboard
PYTHONPATH=src python -m jarvis.daemon                    # voice (needs a mic)
```

The dashboard prints a URL carrying a per-launch token. Plain
`localhost:5071` returns 401 by design.

---

## Outstanding work

Ranked. The first two are user-visible; the third is the one that makes
computer use actually useful.

### 1. Undefined CSS variables (small, visible)

`src/desktop_app/memory_viewer.py` uses `var(--border)`,
`var(--success-light)` and `var(--error-light)` in the chat and
connections styles. None are defined — the theme block declares
`--border-color`, `--success`, `--error`. An unresolvable `var()`
invalidates the whole declaration, so those borders render as none and
**a failed MCP server is visually identical to a working one**.

### 2. Dead `.chat-shell` CSS (small)

The chat code toggles `talking` on `.hud-core`, but every rule keyed on
it targets `.chat-shell.talking` — a class no element carries since the
three-column rebuild. The orb-shrink behaviour never fires and ~60 lines
of CSS are unreachable.

### 3. Vision for `computerUse` (the real one)

`computerUse` can click and type, but **cannot see**. Nothing in
`src/jarvis/llm/` sends images, so `action="screenshot"` only reports
screen size. It works for "click at 500,400" and not for "click Play".

To close it: capture with `mss`/`pyautogui`, send as an image part to
the vision model (Gemini 3.x and Claude both return click coordinates
directly — no OCR engine needed), feed the coordinate back. The
`LLMBackend` ABC in `src/jarvis/llm/backend.py` is the seam; it has no
image support yet, so that is the first change.

Considered and rejected: **OSWorld** is a benchmark that drives VMs, not
a library. **Agent-S** is a real GUI-agent framework, but pulls
`paddleocr`+`paddlepaddle` (~1 GB) to answer a question our models
already answer directly.

### 4. CLAUDE.md compliance gaps

- No `computer_use.spec.md` or `open_app.spec.md`, and the Spec File
  Registry table in `CLAUDE.md` is not extended.
- README's built-in tools list has `openApp` missing and no `computerUse`.
- `computer_use_confirm` has no `FieldMeta` entry, so the only way to
  change it is hand-editing config.json — for a safety-critical setting.

### 5. Product gaps (not bugs)

Streaming replies (~16s of dead air per turn is the most visible),
image/file input, document RAG, multi-user (the dashboard holds one
global conversation), scheduled tasks, tool-call visibility in the UI.

---

## Design decisions worth not undoing

**`openApp` takes no command string.** The model picks a name from a
closed set or gives an http(s) URL. Jarvis reads attacker-controlled web
pages; a `command` field turns "ignore previous instructions and run…"
on any page into code execution. A test asserts the schema has no
command-shaped field.

**`computerUse` confirmation is enforced, not requested.** The code is
printed to the user's screen via a channel the model never reads, so it
cannot approve itself. Codes are single-use, expire, and are bound to the
exact action — an approval for "click Play" cannot be spent on "click
Delete". One approval then opens a 15-minute window for ordinary actions,
because a gate met on every scroll is one people learn to clear without
reading. Typing and key presses never get the window.

**The dashboard fetches nothing external.** No CDN webfonts. It renders
the user's diary; a font request would disclose their IP and the time
they opened it, on a product whose headline claim is "100% local".
Guarded by `tests/test_memory_viewer_offline.py`.

**Web search reports failure honestly.** A dead search returns an
envelope telling the model to say so and invent nothing. Previously it
wrapped an empty payload in a success envelope and the model made up an
excuse.

---

## Gotchas that cost time

- **The dashboard HTML is inside a Python triple-quoted string.** A JS
  `"\n"` is eaten by Python and breaks the literal at parse time. Write
  `"\\n"`.
- **Chromium blocks port 5060** (`ERR_UNSAFE_PORT`, it is the SIP port).
  Browsers silently refuse to connect. Use 5071+.
- **Gemini free tier is tight.** `gemini-3.6-flash` returned
  `RESOURCE_EXHAUSTED` at ~20 requests; each conversational turn costs
  3-4 chat-tier calls. `gemini-3.1-flash-lite` has headroom. Symptom of
  running out: memory writes fail *silently*, because the summariser
  swallows errors and returns None.
- **~34 tests fail in a bare checkout** from missing optional deps
  (`faster_whisper`, `sounddevice`, `psutil`, PyQt6 needing `libEGL`).
  Diff failures against a baseline rather than reading the raw count.
- **Qt offscreen rendering works** (`QT_QPA_PLATFORM=offscreen`), which
  is how the orb was reviewed without a display.

---

## Security notes

Two real holes were found and closed in review; both are worth not
reintroducing.

- The dashboard token was exported to `os.environ`, so every subprocess
  inherited it — including third-party MCP servers, which are spawned
  with `{**os.environ}`. Any of them could POST `/api/mcp`, the endpoint
  that writes a command Jarvis later executes. It is now passed to the
  viewer child only.
- `/api/settings/test` sent the saved API key as a Bearer token to
  whatever URL it was given. It now refuses non-http(s) schemes and
  private/loopback/link-local addresses.

Still unaddressed: the dashboard has no rate limiting, and
`/api/mcp` remains a code-execution path by design — it is gated only by
the session token.

**Credentials:** two GitHub PATs and a Gemini key were pasted in the
originating chat session and should be treated as compromised.

---

## Suggested next session

Start with: *"Read PROGRESS.md. Fix the undefined CSS variables and the
dead `.chat-shell` rules, then add image support to `LLMBackend` so
`computerUse` can see."*

Follow `CLAUDE.md`: TDD, spec files next to code, British English,
conventional commits, and update the Spec File Registry when adding one.
