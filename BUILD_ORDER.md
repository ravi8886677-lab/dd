# Jarvis — build order and feature ledger

The one file to read before starting a feature. It answers two questions:

1. **Which slice do I build next?** Section 3, top unticked row. One at a
   time, in order.
2. **What already exists and what does it do?** Section 5, the ledger.
   Every agent adds a row when a slice lands, so the next session does
   not have to re-derive the tree.

Companions: `PROGRESS.md` says where things stand, `REQUIREMENTS.md`
lists hardware verification that needs a human, `CLAUDE.md` carries the
rules that have teeth. This file is about sequencing new capability.

---

## 1. The rule: vertical slices, one at a time

The gap between the spec and the tree is not polish, it is the bottom
three layers of the spec's own architecture (identity, permissions,
connectors). That gap is large enough to tempt an agent into building
half of four things at once. Do not.

**A slice is finished when a user can do the whole thing, not when the
code for part of it exists.** Concretely, a slice is only done when all
of the following are true:

- 🧪 Tests written first, failing before the change, passing after, and
  asserting behaviour (what a user observes) rather than call counts.
- 📄 A `*.spec.md` sits next to the code and describes the present
  state, not the change.
- 🗺️ `docs/llm_contexts.md` updated if any LLM call was added, removed,
  or altered.
- 🖥️ It is reachable from at least one real surface (chat, dashboard,
  or tray). Code with no way to invoke it is not a slice, it is debt.
- 🧾 A row added to the ledger in section 5 of this file.
- 🐛 `pytest -q -m unit` green **on a machine that has never run it
  before**, not just on a warm one. See slice 0 for why that distinction
  is not pedantry.

If a slice turns out bigger than expected, split it into two slices that
are each independently usable. Never leave a half-wired one behind.

---

## 2. Why this order and not another

The spec's own build order puts identity and permissions first, and the
reason is mechanical rather than aesthetic: the per-account and
per-device permissions of §16 cannot be expressed without the `User`,
`Device` and `Connected Account` rows of §29. Every connector in §10
needs somewhere to hold its credentials and its scope. Every automation
in §22 needs an identity to act as and a permission to act under.

So the dependency chain is:

```
  0. live bugs
        │
        ▼
  1. data model + identity  ──────────────┐
        │                                 │
        ▼                                 ▼
  2. verification + audit          3. permission engine
        │                                 │
        └───────────────┬─────────────────┘
                        ▼
                 4. scheduler
                        │
                        ▼
                 5. connectors   (blocked on the decision in section 4)
```

Nothing in Phase 2 or Phase 3 of the spec starts before all five land.

---

## 3. The slices, in order

### Slice 0 — the dashboard is broken on a fresh install 🔴

**Why first:** it is the only place in the tree where a shipped feature
does not work for a new user, and the rule at the top of this file is
that we do not add features while an existing one is broken. It is also
half a day, not a week.

Two defects, both confirmed against this tree:

- `src/desktop_app/memory_viewer.py:236` opens the SQLite file with a
  raw `sqlite3.connect` and never applies the schema. `Database.__init__`
  in `src/jarvis/memory/db.py` is what creates the tables, and it only
  runs when the daemon starts. A user who installs Jarvis and opens the
  dashboard before ever speaking to it gets `500 {"error":"no such table:
  conversation_summaries"}` from `/api/stats`, `/api/memories` and
  `/api/meals`. `/api/graph/stats` returns 200 because `GraphMemoryStore`
  initialises its own schema on access, and that is the shape to copy.
- `tests/test_memory_viewer_auth.py` runs against the real
  `~/.local/share/jarvis/jarvis.db` rather than a tmp path. On a cold
  machine the five tests touching `/api/stats` fail, the run creates the
  database as a side effect, and every run after it is green. A cold CI
  runner goes red and nobody can reproduce it locally. It also means the
  suite writes to the user's own data directory.

Fixing the first makes the second's symptom vanish, which is exactly why
the second needs its own fix with a tmp path.

**Done when:** a container that has never run Jarvis can start the
dashboard against an empty data directory and get 200 with empty results
from all four endpoints, and the dashboard test files point at a tmp
database that the run deletes.

---

### Slice 1 — data model and local identity (§29, §35.1)

**Why here:** everything above it needs these rows, and the report is
right that they need to exist even while there is exactly one user.

Add the identity tables to `src/jarvis/memory/db.py` and have the daemon
create and read them: `User` (one local row), `Workspace` (one
`personal` row, with the shape to hold more), `Device` (this machine,
stable id, last seen), `ConnectedAccount` (empty for now, credentials by
reference into `utils/secret_store.py`, never inline).

Scope discipline: 25 entities are specified. Build the four that slices
2 to 5 actually consume. The rest arrive with the features that need
them, not before, or they rot as empty tables.

**Done when:** a fresh install creates exactly one user, one workspace
and one device row on first launch; a second launch reuses them rather
than adding more; the device row's last-seen updates; and the dashboard
shows which device it is talking to. No feature yet reads a second
workspace, and that is fine, but the schema must not need changing to
add one.

---

### Slice 2 — verification and the action log (§18, §25)

**Why here:** these are the two non-negotiables the tree currently
violates, they are the cheapest large win, and slice 1 gives them
something to attribute an action to.

Two halves, both required for the slice to be done:

- An `actions` table plus a write on every external side effect, holding
  who, which device, which tool, what arguments (redacted through
  `utils/redact.py`), the result, and the outcome. Surface it as an audit
  timeline in the dashboard, because an audit log nobody can read is not
  an audit log.
- A post-action check per tool that can have one. Today `computerUse`
  reports `Done: <description>` from the bare fact that pyautogui
  returned, and the system prompt contains no instruction against
  claiming a completion that did not happen. Both need fixing: the tool
  re-checks observable state, and the prompt change is verified against
  an eval case that is worse before it and better after, per `CLAUDE.md`.

**Done when:** every tool call with an external effect appears in the
audit timeline with its outcome; a `computerUse` action whose effect did
not land reports that it did not, and a test proves it; and the eval for
fabricated completion claims improves.

---

### Slice 3 — the permission engine (§16)

Replace the single global time-boxed grant in `src/jarvis/approval.py`
with per-tool and per-account scopes, keeping YOLO as one policy layered
on top rather than the only mechanism. The five risk levels of §17
replace the three of `tools/external/mcp_trust.py`.

Keep the property that `tests/test_yolo.py` asserts: nothing in the tool
layer can widen its own permissions. That is the single most important
line in the current approval model and it must survive the rewrite.

**Done when:** a user can grant one tool without granting all of them,
revoke one without revoking the rest, see every live grant in the
dashboard and end it there; grants still lapse on their own; and no tool
call path can reach the grant API.

---

### Slice 4 — the scheduler (§11, §22)

One timer unlocks briefings, alerts, monitoring and automations at once,
which is why they are a single slice rather than four. Jarvis is
currently entirely reactive: `daemon.py` has no timer and no unprompted
output path.

Needs slice 3 first, because an automation that fires while nobody is
watching is precisely the case where a global grant is the wrong model.

**Done when:** a user can create one scheduled thing in natural
language, see it listed, see it fire, see what it did in the slice 2
audit timeline, and stop it. One working automation beats a builder that
can express ten it cannot run.

---

### Slice 5 — connectors (§10) 🚧 blocked

Blocked on the decision in section 4 below, not on code. Do not start it
until that is settled in writing.

---

## 4. The decision that has to be made before slice 5

`CLAUDE.md` line 1 is "Data privacy comes first, always", and the project
declines any vendor-locked cloud dependency on principle, including as
an opt-in. The spec asks for hosted email, calendar, CRM, smart-home and
mobile integrations, most of which have no local path at all.

These two cannot both hold. The resolution is the user's call, not an
agent's, and there are only two honest options:

- **A carve-out.** `CLAUDE.md` gains an explicit exception for
  third-party accounts the user has personally authorised, with the
  boundary written down (their own credentials, their own data, no
  vendor SDK in the core, revocable per account). The offline principle
  then means "no vendor in the pipeline the user did not choose" rather
  than "no network".
- **No carve-out.** Sections 7 to 10 stay reachable only through MCP
  servers the user adds themselves, which is where they are today, and
  the spec's connector row is formally out of scope rather than pending.

Whichever is chosen, write it into `CLAUDE.md` before slice 5 opens. An
agent that guesses this wrong either builds something the project's first
principle forbids, or refuses work the user actually wanted.

---

## 5. Ledger: what exists and what it does

Read this before writing anything, so you extend a subsystem rather than
building a second one beside it. **Add a row when a slice lands.** Keep
rows in the present tense: what the code does now, not what changed.

### Already in the tree

| Piece | Where | What it does |
|---|---|---|
| Reply engine | `src/jarvis/reply/engine.py` | The orchestrator. Redact, plan, route tools, enrich from memory, warm the profile, gate the allow-list, run the agentic loop (bounded at 8 turns), update memory. |
| Planner | `src/jarvis/reply/planner.py` | Decomposes one utterance into an ordered step list, and drives direct execution for small models. Per-turn and in-memory: it does not persist goals. |
| Tool registry | `src/jarvis/tools/base.py`, `types.py`, `selection.py` | Typed tools with JSON schemas, 13 built-ins, MCP tools discovered and merged, availability gating so a tool with missing dependencies is never advertised, four selection strategies including two-stage embedding retrieval. The maturest part of the tree. |
| Context engine | `src/jarvis/memory/`, `recall_gate.py` | Warm profile from the user and directives branches, diary and graph enrichment, a recall gate that skips enrichment on follow-ups, time and location block, tool carry-over caps, a memory digest for small models. Assembly is selective, not a dump. |
| Memory | `src/jarvis/memory/db.py`, `graph.py` | SQLite. `meals`, `conversation_summaries`, `summaries_fts`, `embeddings`, `summary_vec`, plus `memory_nodes` for a self-organising topic tree of text nodes. The graph holds free-text nodes under a `parent_id`, so it has no typed entities and no typed edges. |
| Approval | `src/jarvis/approval.py` | One global time-boxed grant, 5 to 480 minutes, in memory only so a restart drops it. Nothing in the tool layer can call `grant`, and `tests/test_yolo.py` asserts it. |
| MCP security | `src/jarvis/tools/external/mcp_supply_chain.py`, `mcp_audit.py`, `mcp_trust.py` | Launch pinning, tool fingerprinting against rug pulls, a static poisoning audit, and a per-server trust store with three risk levels against four policies. |
| Network guard | `src/jarvis/utils/net_guard.py` | One SSRF gate in front of every untrusted fetch. Refusal reasons are distinct types, so a DNS outage never reads as a policy block. |
| Secrets | `src/jarvis/utils/secret_store.py` | OS keychain. Credentials for slice 1's `ConnectedAccount` belong here by reference, never inline in a row. |
| Dashboard | `src/desktop_app/dashboard/`, `memory_viewer.py` | Local Flask dashboard: diary, graph explorer, meals, connections, settings, chat, YOLO slider. Host allow-list, per-launch token, rate limits. Frontend assets are real CSS/JS/Jinja files. |
| Chat front end | `src/jarvis/chat/` | `python -m jarvis.chat`, headless, never imports the audio or GUI stacks. |

### Added by slices

| Slice | Piece | Where | What it does |
|---|---|---|---|
| _(none yet)_ | | | |

---

## 6. Deliberately not in scope

So no agent quietly starts one of these believing it is next:

- The 16 named agents of §13. There is one agent with one persona in
  `src/jarvis/system_prompt.py`, and a router between agents buys
  nothing until there is more than one thing to route to.
- Mobile, wearable, browser extension and smart-home surfaces (§7, §8).
- Image, PDF and Office input (§5). No image reaches the model today;
  screen text arrives through OCR.
- A public or developer API (§4). The dashboard API is loopback-only
  behind a per-launch token and stays that way.
- Long-running async jobs (§26). The agentic loop is bounded and
  synchronous, and checkpointing it is a slice of its own, after 5.
