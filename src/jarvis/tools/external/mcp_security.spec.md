# MCP security spec

## Purpose

An external MCP server is code the user installed and text the model
reads. Both are attack surface, and Jarvis drives a real mouse and
keyboard, so a compromised server is not a data-leak problem alone.

Four defences sit in front of it, at four different moments:

| Moment | Module | Answers |
|--------|--------|---------|
| Before the subprocess starts | `mcp_supply_chain.py` | Is this code pinned, or whatever the registry serves today? |
| At tool discovery | `mcp_trust.py` | Is this the same tool the user accepted? |
| Before a tool runs | `mcp_gate.py` | Does a person need to say yes to this? |
| On demand | `mcp_audit.py` | Do any definitions carry the shape of a known attack? |

They are independent. Any one can be defeated without opening the
others, which is the point of having four.

## Supply chain: pinning

`npx pkg@latest` and `uvx pkg` resolve against a public registry on
every launch, so the code that runs is whatever is published at that
moment. `validate_server_launch` runs inside `MCPClient._connect_stdio`,
before `stdio_client` is called, so a refused launch never spawns
anything.

| Input | Result |
|-------|--------|
| `npx -y pkg@1.2.3` | allowed — exact semver |
| `npx -y pkg@latest`, `pkg@^1.0.0`, `pkg@1.x`, bare `pkg` | refused |
| `uvx pkg==1.2.3` | allowed — exact PEP 440 pin |
| `uvx pkg`, `pkg>=1`, `pkg==1.*` | refused |
| `npx -y ./local`, `file:../x`, `/opt/x` | allowed — already on disk |
| `node server.js`, `python -m x`, `docker run …` | not checked — no registry step |
| git URL ending in a 40-hex commit | allowed |
| any of the above with `"allow_unpinned": true` on that server | allowed, logged |

`allow_unpinned` must be a real boolean `true`. A truthy string does not
count, so a stray `"no"` cannot silently disable the guard.

`npx -p X runner` and `uvx --from X tool` name the package in the flag;
the bare token after it is the binary to run, not a second install.
`-p` means `--package` to npx and `--python` to uvx, and the two are
parsed accordingly.

Every entry in `src/desktop_app/mcp_catalogue.py` is pinned, enforced by
`tests/test_mcp_catalogue.py` against this same function, so the wizard
cannot offer something the launcher will refuse.

## Trust: fingerprints

A tool's name, description and schema reach the model as instructions.
`fingerprint_tool` hashes those plus annotations; `TrustStore.review`
compares each tool against its record.

- **First sight** is recorded and allowed. Adding the server to
  `config.json` is already the user's trust decision, so this is trust
  on first use, as with SSH host keys.
- **Unchanged** passes.
- **Changed** is withheld, and stays withheld across restarts until
  `TrustStore.accept` records the new definition. A change nobody saw
  must not become trusted just because the daemon restarted.

Records live in `mcp_trust.json` beside `config.json`, mode `0600`,
keyed by server then tool so two servers cannot pre-approve each other's
names. A corrupt store logs and starts fresh rather than taking
discovery down: a damaged file is far likelier to be disk trouble than
an attack, and failing shut would strand the user with no tools.

## Gate: confirmation

`classify_risk` reads the server's annotations. Only a literal `True`
counts, so `"readOnlyHint": "yes"` does not reach the safe bucket.

| Annotations | Risk |
|-------------|------|
| `destructiveHint: true` | HIGH |
| `readOnlyHint: true` | LOW |
| `openWorldHint: true` (without read-only) | HIGH |
| none, empty, or non-boolean | UNKNOWN |

The MCP spec's own defaults treat an unannotated tool as destructive and
open-world. Applied literally that puts every tool from every
unannotated server behind a prompt, and a gate met on every call is one
people learn to clear without reading, so silence maps to UNKNOWN and
the user's policy decides what that is worth.

`mcp_confirm` in `config.json`:

| Value | Confirms |
|-------|----------|
| `off` | nothing |
| `destructive` (default) | HIGH |
| `unannotated` | HIGH and UNKNOWN |
| `all` | everything |

`resolve_policy` reads `cfg` only. A key inside a server entry or a tool
annotation cannot change it: installing a server is not implicitly a
request to stop being asked, and a gate the gated party can open is not
a gate.

The code is printed to stderr by `_announce`, not through the tool
layer's `user_print`, which is suppressed under `voice_debug` — a code
nobody can read would turn the tool off permanently instead of failing
loudly. An approval is bound to a hash of server, tool and canonicalised
arguments, so a code issued for one call cannot be spent on another;
it is single-use and expires after `_CODE_TTL_SEC`.

`confirmation_code` is Jarvis's own argument. It is advertised as an
optional property on every MCP tool's schema (which tools are gated
depends on policy and on annotations that move between runs, and a model
with no way to express an approval cannot relay one) and is stripped in
`run_tool_with_retries` before the call reaches the server.

Unlike `computerUse` there is no trust window. Computer use approves a
burst of clicks that would be unusable one code at a time; an MCP call
reaching this gate is a discrete, named action with no equivalent flood
to smooth over.

## Audit

`audit_server_tools` checks definitions for the shapes tool poisoning
and cross-server shadowing need. Reachable via
`python -m jarvis.mcp_trust_cli audit`.

| Code | Detects |
|------|---------|
| `hidden_characters` | Unicode that renders as nothing or reorders text (`Cf`/`Cc`, bidi overrides, zero-width) — invisible when a human reviews the description, read by the model |
| `instruction_markup` | `<IMPORTANT>`, `<system>`, `[INST]`, `<\|…\|>` — imitating the prompt's framing rather than describing a tool |
| `sensitive_path` | `~/.ssh`, `.env`, `~/.aws/credentials` and similar |
| `cross_server_reference` | one server's description naming a tool owned by another (names under 4 characters are ignored — they match inside ordinary words) |

Every check is **structural, never lexical**. Matching English phrases
would miss an attack written in another language while flagging honest
descriptions that use those words, which is why
`tests/test_mcp_audit.py` asserts that French, Japanese, Russian and
Arabic descriptions raise nothing.

Schemas are serialised with `ensure_ascii=False`; the default would turn
a hidden `U+202E` into the visible literal `‮` and defeat the
invisible-character check.

## Credentials

`jarvis.utils.secret_store` keeps API keys in the OS credential store
(Keychain, Credential Manager, Secret Service) instead of `config.json`.
This matters here specifically: MCP server subprocesses run as the user
and can read that file.

- A key leaves `config.json` only once the store returns the same value.
  A machine with no working backend keeps its plaintext key rather than
  losing the user's only copy.
- An explicit `config.json` value wins over the store, so hand-editing
  the file behaves the way it looks like it should.
- Credential-store calls catch `BaseException`, not `Exception`:
  keyring's Secret Service backend raises `pyo3_runtime.PanicException`
  on a host with a half-installed `cryptography`, and that derives
  straight from `BaseException`. `KeyboardInterrupt` and `SystemExit`
  still propagate.
- Logging from this path is re-entrancy-guarded and a failed backend is
  latched off for the session. `debug_log` asks `load_settings()`
  whether debug output is on, and `load_settings()` resolves credentials
  through this module, so an unguarded failure logs, loads config,
  fails, and logs without bound.

## Test contract

| File | Covers |
|------|--------|
| `tests/test_mcp_supply_chain.py` | pin detection per ecosystem, flag parsing, opt-out, and that a refused launch never reaches `stdio_client` |
| `tests/test_mcp_trust.py` | fingerprint stability, trust on first sight, withholding across restarts, accept, per-server isolation, file mode, corrupt store |
| `tests/test_mcp_gate.py` | proposal/approval, binding to server + tool + arguments, single use, expiry, and that the server is not called before approval |
| `tests/test_mcp_audit.py` | each check, non-English descriptions raising nothing, and ordinary text not tripping the markup or path checks |
| `tests/test_secret_store.py` | round trip, honest unavailability, read-back before trusting a write, migration, `BaseException` backends, no recursion |
| `tests/test_mcp_catalogue.py` | every shipped catalogue entry passes the launch guard |

## Non-goals

- **Sandboxing servers.** A server still runs with the user's
  privileges. Containerised distribution would change that; pinning
  only fixes *which* code runs.
- **Proving a package is safe.** A pin makes the code reproducible, not
  trustworthy.
- **Remote transports.** Only stdio is supported, so OAuth token
  handling, `iss` validation and audience checks do not arise yet.
- **Rate limiting the dashboard**, which remains gated by its session
  token alone.
