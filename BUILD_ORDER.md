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

### Slice 0 — the dashboard works on a fresh install ✅

**Why it came first:** it was the only place where a shipped feature did
not work for a new user, and the rule at the top of this file is that we
do not add features while an existing one is broken.

What now holds:

- The dashboard applies the schema when it opens the database, so an
  install that has never run Jarvis answers `/api/stats`,
  `/api/memories`, `/api/meals` and `/api/topics` with empty results
  rather than `500 no such table`. The schema has one definition, in
  `jarvis.memory.db`, which both the daemon and the dashboard apply.
- Resolving a path never creates it, in any of the writers. The
  database, the GeoIP database, Piper voices and dictation history are
  all named by pure resolvers; the directory appears at the write site.
  Nothing arrives on disk because a module was imported.
- The whole data directory is redirected during tests, not just the
  database. `JARVIS_DATA_DIR` points `paths.data_dir()` elsewhere, and
  `tests/conftest.py` sets it session-wide alongside the config-path
  guard that was already there, so every writer moves together and one
  added later cannot slip through. This is what made the fresh-install
  defect invisible: the first run on a cold machine created the database
  as a side effect and every run after it was green.

Verified against a real server on a fresh home: all four endpoints 200,
then the daemon wrote a summary and a meal to the file the dashboard had
created, and the dashboard read both back, full-text search included.

### Slice 1 — data model and local identity (§29, §35.1) ✅

**Why it came here:** everything above it needs these rows. §16's
per-account and per-device permissions cannot be expressed without
something to point at, and §25's audit cannot say which machine acted.

What now holds:

- Four entities in the same SQLite file: `users`, `workspaces`,
  `devices`, `connected_accounts`. Four rather than the specified 25,
  because the rest arrive with the features that need them.
- The daemon establishes identity on the way up and names the machine
  and workspace it is acting in. It fails open: nothing depends on the
  rows yet, so a store that will not open is reported, not fatal.
- A second launch adds nothing and refreshes the device's last-seen.
- The device identifier lives beside the database, not inside it, so
  rebuilding or moving the database does not present the machine as a
  new one and silently drop what was granted to it.
- `connected_accounts` holds a reference to a credential, never a
  credential. The keychain in `utils/secret_store.py` keeps the secret.
- The dashboard shows which machine it is reading from, with the other
  devices on the account in the tooltip.

Establishing identity is safe when the daemon and the dashboard do it at
the same moment on a fresh install, which is an ordinary thing to happen:
the whole decision runs inside one `BEGIN IMMEDIATE` transaction, so the
second process waits and then finds what the first one wrote.

Nothing reads a second workspace yet, which is expected. The schema does
not need changing to add one, and the reads that mean "yours" are scoped
by user so that adding one does not quietly show someone else's machines.

### Slice 2 — verification and the action log (§18, §25) ✅

**Why it came here:** these were the two non-negotiables the tree
violated, and slice 1 gave an audit entry something to attribute to.

What now holds:

- **One boundary.** Every builtin and every MCP call crosses
  `run_tool_with_retries`, which decides, records the decision *before*
  anything executes, runs the call, and records what came back. Two
  tools used to gate themselves and everything else ran ungated and
  unrecorded.
- **The decision lands first.** An action that took the process down
  with it still left evidence that it was attempted. An action with a
  decision and no outcome is a call that never came back.
- **Entries are appended, never rewritten.** A record that can be edited
  after the fact is not evidence, so the outcome is a second entry
  correlated by `action_id`.
- **Verification is a separate field from outcome.** `localFiles` reads
  back what it wrote; `openApp` looks for the process. A tool with
  nothing to check records `not_checked`, which is never treated as
  success. MCP stays `not_checked` on success: the server's own report
  is recorded as the outcome, and treating it as verification would mean
  trusting the audited party to audit itself.
- **Arguments are redacted by the log**, not by asking callers to
  remember, and a secret is recorded by name and character count only.
- **The YOLO grant is on the same log.** It used to exist only in
  memory, so a restart erased the evidence that the window had ever been
  open.
- **The prompt forbids a completion claim the record does not support**,
  with a live eval case and a mocked equivalent.
- **The dashboard has an Activity tab.** `not checked` is shown as its
  own state rather than folded into success.

Enforcement is not yet consolidated: the boundary enforces what it can
express from the call, and a tool that gates itself keeps doing so,
because a tool can be called directly. `computerUse`'s rule is one
function with two callers rather than two copies. Consolidating belongs
to slice 3, which is what a stored, evaluable policy is for.

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

## 3a. What CI runs

CI runs the marker, so the marker decides what is verified. Every test
file carries one; the only file without a `unit` mark is
`tests/performance/`, which needs a live Ollama and is excluded by
`addopts` anyway.

`.github/workflows/tests.yml` selects **`unit and not integration`** and
runs under `xvfb-run`. Both halves matter:

- **The display.** `pynput` raises on import without an X server, and
  that takes the dictation and hotkey tests down with it. A virtual
  display costs one package and keeps 21 real tests in CI that would
  otherwise have to be quarantined for an environment reason rather
  than a behavioural one.
- **`not needs_hardware`.** A test in a unit-marked file can be held
  back by marking it `needs_hardware`, without pulling its whole file
  out of CI with it. That distinction is worth having:
  `test_voice_listener.py` is 96 passing tests and one that needs a
  sound card, and losing 96 to quarantine one would be a bad trade.

  The marker is its own thing rather than a reuse of `integration`,
  because `integration` already means "complex setup" and plenty of
  integration tests run in a container perfectly well: eight of them do
  so in under a second. Selecting on `integration` swept those out of CI
  along with the one test that could not run.

One test is quarantined today: `test_health_warning_fires_on_linux`,
which has been red since before the sweep and fails identically on
`85c8362`. It is marked rather than deleted, so it still runs where
there is a sound card and still shows up in a full run.

`tests/test_ci_selection.py` asserts all of this structurally: that
every file is marked, that the quarantine list is exactly what is held
back, and that `integration` never removes a test from CI. The rule
"nothing carries both markers" was true when checked and then broken by
the change that relied on it, so it is asserted rather than remembered.

**When adding a test, mark it.** An unmarked test is not a test CI runs.

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
| 0 | Data directory | `src/jarvis/utils/paths.py` | One answer to where Jarvis keeps its files. `data_dir()` resolves and touches nothing; `ensure_data_dir(*parts)` creates, and belongs at the write site rather than the resolver. `JARVIS_DATA_DIR` moves the lot at once, which is what lets the suite guarantee it never writes the real one. Every module that needs the directory (config, location, GeoIP, Piper voices, dictation history, prompt dumps, dashboard) reads it from here, so nothing can drift into a second location. |
| 2 | Action log | `src/jarvis/audit/` | `ActionLog` owns the `actions` table and appends two entries per action: the decision before execution, the outcome after. Never rewrites an entry. Redacts arguments on the way in and records a secret by length only. `recorder` holds the process's one log and resolves the acting identity once. |
| 2 | Action boundary | `tools/registry.py` | `run_tool_with_retries` decides, records, executes, records. `_authorise` holds the rules the boundary can evaluate from the call itself; recording is best-effort and never raises, authorisation fails closed. |
| 2 | Verification | `local_files.py`, `open_app.py`, `types.py` | A tool that can check what it did sets `verification` on its result; the boundary records it. `not_checked` is the default and is never success. |
| 1 | Identity store | `src/jarvis/identity/` | `IdentityStore` owns the `users`, `workspaces`, `devices` and `connected_accounts` tables and applies its own schema on construction, like `GraphMemoryStore`. `ensure_local_identity()` is idempotent and is what both the daemon and the dashboard call. `local_device_id()` names the machine from a file beside the database. Accounts carry a `secret_ref` into the keychain, never a secret. |
| 1 | Identity surface | `daemon.py`, `/api/identity` | Startup prints the machine and workspace it is acting in and fails open. The dashboard header shows the device it is reading from and counts the others on the account. |
| 0 | Shared schema | `src/jarvis/memory/db.py` | `ensure_schema(conn)` applies the diary and meal tables to any open connection, and `open_database(path)` does the whole opening: parent directory, connection, row factory, schema. `Database` and the dashboard both go through it, so the daemon and the dashboard cannot disagree about what the file contains. |

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
