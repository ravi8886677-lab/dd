# The action boundary and the action log

What Jarvis did, whether it was allowed, and whether it actually
happened.

Two of the project's non-negotiables were the two it did not meet. There
was no verification, so a completion claim was only as good as the
model's optimism; and there was no record, so "every external action
attributable" had nothing to attribute to. Both need the same thing
underneath.

## One boundary

`tools/registry.run_tool_with_retries` is the boundary. Every builtin
and every MCP call crosses it, which is what makes it the one place able
to answer "what did Jarvis do?".

Before this, two tools gated themselves and everything else ran ungated
and unrecorded: writing a file under `$HOME`, launching a process,
fetching a page.

The order at the boundary is fixed, and the order is the point:

1. classify the call (builtin, mcp, unknown)
2. decide
3. **record the decision, before anything executes**
4. execute, or return the refusal
5. **record what came back**

Step 3 lands before step 4 so that an action which took the process down
with it still left evidence that it was attempted. An action with a
decision and no outcome is a call that never came back, which is more
informative than no row at all.

## Two entries, appended, never rewritten

An entry is never updated after it is written. A record that can be
edited after the fact is not evidence of anything, so the outcome is a
second appended entry correlated by `action_id` rather than a column
written over the first.

A reader wanting one row per action gets it from `get_actions()`, which
folds the pair. `outcome` is `None` there for an action that never
finished, and `decision` is what distinguishes that from one that was
refused: a refusal never executed, so it has nothing to report.

## Verification is not assumed

`outcome` says what the call returned. `verification` says whether
anyone looked. They are different fields because a function returning is
not the same as the world having changed, which is the whole distinction
between logging and verification.

| Value | Means |
|---|---|
| `confirmed` | a tool looked after acting, and the change was there |
| `failed` | a tool looked, and it was not |
| `not_checked` | nobody looked |

`not_checked` is the default. A tool with nothing to check must never
read as confirmed, because "we did not check" and "it worked" are the
two things this field exists to keep apart.

What checks today: `localFiles` reads back what it wrote and compares,
and checks that an append grew the file; `openApp` looks for the process
it launched, and answers `not_checked` where the question cannot be
asked on this machine.

MCP calls stay `not_checked` on success. The server's own result is
recorded, in `outcome`, which is where a report from the acting party
belongs; treating it as verification would mean trusting the thing being
audited to audit itself.

## Enrichment fails open. Authorisation fails closed.

The codebase's reflex is to fail open, and for retrieval that is right:
a broken recall gate should degrade to more context, not to a dead
assistant. Authorisation is the opposite, and the difference has to be
stated because applying the house reflex here would produce a gate that
opens whenever it breaks.

**A broken rule never grants.**

Recording is neither. It is best-effort and never raises: witnessing an
action must not be able to prevent it, because a full disk is not a
reason to refuse work the user asked for. A gap in the log is visible on
its own.

## Where the rules live

The boundary decides what it can evaluate from the call itself. A tool
that also enforces its own rule keeps doing so, because a tool can be
called directly and a gate that only exists upstream is not a gate.

`computerUse` is the case: `physical_action_is_permitted` is one
function with two callers, asked by the boundary before execution so the
decision is recorded before the fact, and asked again by the tool on its
own path. One rule, two call sites, rather than two copies that drift.

Consolidating enforcement belongs to the permission engine, which is
what a stored, evaluable policy is for. Until then the boundary records
every decision and enforces the ones it can express.

## Where the log lives is not the caller's job

`configure(db_path)` is the precise path: a caller holding a `Settings`
points the recorder at the database it is already using, and the
boundary does it on every call from the `cfg` it is handed, so the
record lands in the same database the action operated against.

Nothing depends on a front end remembering to, because a front end that
forgets loses its entire record and loses it without an error: recording
is best-effort by design, so the failure is silent by construction. An
unconfigured recorder therefore resolves the database from settings
rather than going quiet.

The dashboard is why this is stated. It reads the log for its Activity
tab, it can act as the user from `/api/chat`, and it granted YOLO from
`/api/yolo`, while only `daemon.py` and `chat/cli.py` ever called
`configure` and the frozen desktop app serves the dashboard in-process
without going through either. Every action taken there went unrecorded,
so the log was missing exactly the calls someone went looking for, and
had nothing to say it was missing them.

A log that is complete for the front ends someone remembered is not an
audit trail; it is a summary written by the ones that were thought of.

## Secrets

Arguments are redacted by the log on the way in, through
`utils/redact.py`, rather than by asking every caller to remember. A
secret is recorded by name and character count and never by value:
enough to answer "was a credential used here?" without the log becoming
the thing worth stealing.

The secret description is appended after the scrub rather than passed
through it. It contains no value by construction, and the scrub would
otherwise read `api_key=<17 chars>` as a credential and redact away the
count that makes it useful.

## Human decisions are on the same log

Opening the YOLO window is the most consequential thing the user does,
and it used to exist only in memory: a restart erased the evidence that
it had ever been open. A log showing Jarvis driving the mouse with
nothing to say the window was open cannot explain itself.

`yolo.granted` and `yolo.revoked` are recorded with `tool_source`
`human`. The recording is best-effort and cannot fail a grant, and
`approval.py` keeps no import edge towards the tool layer: granting is a
human action, and a log entry is not a way in.

## What the user sees

The dashboard's Activity tab, newest first. Each row says whether the
action was allowed, whether it finished, and whether anyone checked.
`not checked` is shown as its own state rather than folded into success,
because folding it in is the thing the log exists to prevent.
