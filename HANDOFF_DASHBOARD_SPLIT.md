# Task brief: break up `memory_viewer.py`

For the agent taking the dashboard. Written by the agent who has been
working in this file all session, so the traps below are ones that have
actually bitten rather than ones that might.

Read `CLAUDE.md` first. Its rules are enforced by tests, not by taste.

---

## The problem

`src/desktop_app/memory_viewer.py` is 259 KB. **197 KB of that — 76% — is
one Python triple-quoted string** holding the entire front end:

| | |
|---|---|
| JavaScript | 114 KB |
| CSS | 66 KB |
| Flask routes | 39, in the same file |

The consequences are not stylistic:

- **114 KB of JavaScript is invisible to every JS tool.** No syntax check,
  no linting, no formatter, no editor support. A typo ships.
- **Python's escape rules run first.** A JS string containing `"\n"` is
  eaten by the Python parser and the file breaks at import. You have to
  write `"\\n"`. Same for `\d` in a regex, `\.` in a selector. This is
  documented in `PROGRESS.md` because it has cost real time.
- **Nothing can be tested in isolation.** The only way to assert anything
  about the UI today is substring matching against the rendered string,
  which is what the existing tests do.

## What "done" looks like

A `src/desktop_app/dashboard/` package where the CSS and JS are real files
with real extensions, the HTML is a real template, and the Flask routes are
grouped by domain. Every existing test still passes, unchanged where
possible.

```
src/desktop_app/dashboard/
    __init__.py          # exports `app`; imports NO Qt
    server.py            # Flask app, _guard_request, rate limits, _config_path
    routes/
        memories.py      # /api/memories, /api/topics, /api/memory/<id>
        graph.py         # /api/graph/*
        meals.py         # /api/meals, /api/meal/<id>
        mcp.py           # /api/mcp, /api/mcp/catalogue, /api/mcp/registry
        settings.py      # /api/settings, /api/system, /api/weather
        chat.py          # /api/chat, /api/yolo
        diary.py         # /api/diary/*, /api/stats
    static/
        dashboard.css
        dashboard.js
    templates/
        index.html
```

`memory_viewer.py` stays as a thin shim re-exporting `app` until the last
step, because two things import it by that name (see Invariant 2).

---

## Invariants — these are load-bearing, with the reason each exists

### 1. The page must fetch nothing external

`tests/test_memory_viewer_offline.py` asserts no absolute `http(s)` URL
appears in any `href`/`src`, and that every CSS variable used is also
defined.

This is not fussiness. The dashboard renders the user's diary, personal
facts and meal log. A webfont from a CDN hands a third party their IP,
User-Agent and the time they opened it, on a product whose headline claim
is "100% local". Serve assets from disk through Flask's static handling
with **relative** URLs. Never a CDN, not even for a polyfill.

### 2. Two entry paths must both keep working

`app.py` uses the dashboard **two different ways**:

```python
from desktop_app.memory_viewer import app as flask_app     # in-process (line ~1055)
[python_exe, "-m", "desktop_app.memory_viewer"]            # subprocess  (line ~1118)
```

Both must survive. A package with `__main__` handling covers the second;
the first needs the `app` name to stay importable from wherever you land.

### 3. It must import without Qt

`memory_viewer` runs headless — it is launched on servers with no display.
But `desktop_app/__init__.py` does `from desktop_app.app import main`,
which pulls in PyQt6. Anything you add under `desktop_app/` inherits that
problem the moment someone imports the package rather than the module.

There is already a worked example of the escape hatch:
`src/jarvis/utils/mcp_catalogue.py` loads `mcp_catalogue.py` straight from
its file with `importlib`, precisely to avoid the package `__init__`. Read
it before you invent a second mechanism.

### 4. PyInstaller must find the new files

`jarvis_desktop.spec` bundles data files explicitly. There is one entry
today:

```python
datas = [
    (str(src_path / 'desktop_app' / 'desktop_assets' / '*.png'), 'desktop_app/desktop_assets'),
]
```

CSS, JS and templates become data files the moment they leave the `.py`.
Add them to `datas`, and resolve paths at runtime through `sys._MEIPASS`
when frozen — a bare `Path(__file__).parent / "static"` is correct in a
checkout and wrong in a bundle. **A dev-checkout pass proves nothing about
the packaged app**, which is the recurring lesson in `REQUIREMENTS.md`.

`tests/test_desktop_app.py` already guards a related trap: `app.py` runs as
`__main__` with no package context, so a relative import there raises at
launch. It has caught this once.

### 5. The auth gate covers every route, and must keep doing so

`_guard_request` is a single `@app.before_request` hook: Host allow-list,
session token, then rate limits. Blueprints do **not** get their own
`before_request` unless you give them one.

Verify after splitting that a route on every new blueprint still 401s
without a token. The failure mode is silent — a blueprint registered on a
second Flask app, or a route added later — and the thing left unguarded is
`/api/mcp`, which writes a command Jarvis subsequently spawns. See
`src/jarvis/tools/external/mcp_security.spec.md`.

### 6. The shared theme stays the single source

`src/desktop_app/CLAUDE.md`: always use the shared theme in `themes.py`.
The CSS variables are that contract. Splitting the stylesheet must not fork
the palette.

---

## Suggested sequence

The order matters more than the destination. Each step is independently
verifiable and independently revertable.

**Step 1 — extract the assets only.** Move the `<style>` body to
`static/dashboard.css` and the `<script>` body to `static/dashboard.js`.
Leave all 39 routes exactly where they are. Serve the two files through
Flask.

This is the smallest change with the largest payoff: 180 KB becomes
lintable and the `"\n"` trap disappears, because the files are no longer
inside a Python string. Do not undo the `\\` escapes by hand — a
mechanical extraction is safer, then check the JS parses (`node --check`).

**Step 2 — prove packaging.** Update the `.spec`, build, and confirm the
frozen app still serves the page. Do not proceed until this is green;
everything after it is harder to bisect.

**Step 3 — extract the template.** `index.html` as a real Jinja template.

**Step 4 — split the routes into blueprints**, one domain at a time,
running the suite between each. Re-check Invariant 5 after each.

**Step 5 — write `dashboard.spec.md`** next to the code and add it to the
Spec File Registry table in `CLAUDE.md`. There is no dashboard spec today,
which is part of why this file grew unchecked.

---

## Tests that must stay green

Run with `QT_QPA_PLATFORM=offscreen PYTHONPATH=src:.`

| File | What it protects |
|---|---|
| `test_memory_viewer_offline.py` | Invariant 1, and CSS variable integrity |
| `test_memory_viewer_auth.py` | Invariant 5 — token and Host checks |
| `test_memory_viewer_rate_limit.py` | Invariant 5 — the limits behind it |
| `test_memory_viewer_catalogue_api.py` | Connections directory, incl. UI markup assertions |
| `test_memory_viewer_graph_api.py` | Preset node protection |
| `test_memory_viewer_diary_*.py` | Diary endpoints |
| `test_desktop_app.py` | Invariant 2 and 4 — packaging entry points |

Baseline before you start: **33 failed, 2675 passed, 33 skipped**. All 33
failures need an audio device or a display and cannot pass in a container
(16 `test_dictation`, 7 `test_voice_listener`, 5 `test_desktop_app`, 4
`test_portaudio_serialisation`, 1 `test_llm_thinking`).

**Diff your failures against that baseline; do not read the raw count.**
The quickest honest baseline is a worktree at the commit you branched from:

```bash
git worktree add /tmp/base <commit> && cd /tmp/base && python -m pytest tests/ -q
```

Some of the UI assertions in `test_memory_viewer_catalogue_api.py`
substring-match against `_INDEX_HTML` (`'id="conn-catalogue"' in html`).
Those will need rewriting once the markup moves to a template — that is
expected and fine. **Rewrite them to assert the same property against the
new structure; do not delete them.** They encode real contracts, including
"no badge claims the server was vetted".

---

## Traps, from experience this session

- **`jarvis.*` and `src.jarvis.*` are different module objects** with
  separate module-level state. A fixture patching one does not touch the
  other. This has caused three separate wrong results in one session,
  including a test fixture that was silently a no-op. Reference the module
  the code under test actually holds.
- **`grep '^FAILED'` misses collection errors.** Match `^(FAILED|ERROR)` or
  a whole broken file reads as a pass.
- **Chromium blocks port 5060** (`ERR_UNSAFE_PORT`). Use 5071+.
- **PyQt6 needs `libegl1`** from apt in a headless container, or every
  `desktop_app` import dies with `libEGL.so.1: cannot open shared object
  file`.
- **Flask serves on multiple threads.** Module-level caches in this package
  need a lock; there is one in `jarvis/utils/mcp_catalogue.py` for exactly
  this reason.

## Out of scope

Do not change behaviour. This is a move, not a redesign. If you find a bug
while moving code — and you will, there is 114 KB of untested JavaScript —
write it down and raise it separately rather than fixing it in the same
commit. A refactor whose diff also contains fixes cannot be reviewed.

## Coordination

- **You own** `src/desktop_app/memory_viewer.py` and everything it becomes,
  plus `tests/test_memory_viewer_*.py`. I have stopped touching them.
- **I own** `src/jarvis/**`. If you need a change there, ask rather than
  reaching in.
- Work on your own branch off `claude/progress-requirements-handoff-4dw4zn`
  (its tip is where the baseline above was measured).
- Conventional Commits. Default branch in this fork is `main`.
