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
| `npx [options] -- pkg@1.2.3` | allowed — the separator precedes the package, it does not mean there is none |
| `pipx run pkg==1.2.3` | allowed — `run` is a subcommand, not the spec |
| `node server.js`, `python -m x`, `docker run …` | not checked — no registry step |
| git URL ending in a 40-hex commit | allowed |
| any of the above with `"allow_unpinned": true` on that server | allowed, logged |

`allow_unpinned` must be a real boolean `true`. A truthy string does not
count, so a stray `"no"` cannot silently disable the guard.

`npx -p X runner` and `uvx --from X tool` name the package in the flag;
the bare token after it is the binary to run, not a second install.
`-p` means `--package` to npx and `--python` to uvx, and the two are
parsed accordingly. A flag not in the value-taking lists is assumed to
be a boolean, so its following token is read as the package: that errs
towards refusing a launch we cannot parse rather than waving through one
we misread, and the lists are kept generous so the refusal is rare.

Every entry in `src/desktop_app/mcp_catalogue.py` is pinned, enforced by
`tests/test_mcp_catalogue.py` against this same function, so the wizard
cannot offer something the launcher will refuse.

Config migration v4 (`_repin_catalogue_servers` in `config.py`) rewrites
any `mcps` entry the wizard previously wrote with a floating version to
the catalogue's pinned args. Without it every server an existing user
installed would be refused at once, and their whole MCP tool set would
disappear behind one error line each. It only rewrites an entry whose
name, command and package name all match a catalogue entry, so a
hand-written config is never changed under the user; anything else is
refused with the message above. The catalogue module is loaded from its
file rather than imported, because `desktop_app/__init__` pulls in Qt
and a headless install has to be able to re-pin too.

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

## Gate: YOLO mode

`classify_risk` reads the server's annotations. Only a literal `True`
counts, so `"readOnlyHint": "yes"` does not reach the safe bucket.

| Annotations | Risk |
|-------------|------|
| `destructiveHint: true` | HIGH |
| `readOnlyHint: true` | LOW |
| `openWorldHint: true` (without read-only) | HIGH |
| `readOnlyHint: false` alone | HIGH — the spec defaults `destructiveHint` to true for a tool that is not read-only, so this is a declaration, not silence |
| none, empty, or non-boolean | UNKNOWN |

The MCP spec's own defaults treat an unannotated tool as destructive and
open-world. Applied literally that puts every tool from every
unannotated server behind a prompt, so silence maps to UNKNOWN and the
user's policy decides what that is worth.

`mcp_confirm` in `config.json` decides which risks are gated at all:

| Value | Gates |
|-------|-------|
| `off` | nothing |
| `destructive` (default) | HIGH |
| `unannotated` | HIGH and UNKNOWN |
| `all` | everything |

`resolve_policy` reads `cfg` only. A key inside a server entry or a tool
annotation cannot change it: installing a server is not implicitly a
request to stop being asked.

A gated call runs when **YOLO mode** is on and does not when it is off.
YOLO (`jarvis/approval.py`) is a window the user opens by hand for 15 or
30 minutes, from the tray menu or the dashboard. Outside it, a gated
call returns a `NOT DONE:` message naming the action and telling the
model to ask the user to switch YOLO on. Read-only tools are unaffected
either way.

### The one rule

**Nothing in the tool layer may call `approval.grant`.** Jarvis reads web
pages, MCP tool descriptions and tool results, any of which can carry
text someone else wrote to be read by a model. If enabling YOLO were
reachable from a tool call, that text could enable it and then act
freely until the window lapsed. Granting is a human action in the UI,
and `tests/test_yolo.py` asserts no built-in tool and no part of the
registry can reach it.

That is the property the old per-action confirmation codes existed to
provide. The codes are gone — they were invisible in the packaged build
and tedious everywhere else — but the property they protected is not.

Because a grant is not a per-call argument, there is nothing for the
model to guess at or forge: `confirmation_code`, `yolo: true` and
friends in a tool call are ordinary arguments with no meaning to the
gate. The advertised schema for an MCP tool is now exactly the schema
the server published, with nothing injected and nothing stripped.

A grant lives in memory only, so a restart closes it. Durations are
clamped to `MAX_GRANT_MINUTES`; a longer one is a standing permission
wearing a timer. A non-numeric duration is refused rather than coerced,
so a stray config or message value cannot become a grant.

Only a tool present in the discovery cache may run. A withheld tool is
absent from it, and `run_tool_with_retries` refuses any MCP name the
cache does not carry — otherwise the model could still call one from its
own conversation history, or because another server's description named
it, straight past the withholding.

## Audit

`audit_server_tools` checks definitions for the shapes tool poisoning
and cross-server shadowing need. Reachable via
`python -m jarvis.mcp_trust_cli audit`.

| Code | Detects |
|------|---------|
| `hidden_characters` | Unicode that renders as nothing or reorders text (`Cf`/`Cc`, bidi overrides, zero-width) — invisible when a human reviews the description, read by the model. ZWNJ, ZWJ and variation selectors are exempt: they are orthographically required in Persian, Hindi, Bengali and Malayalam and join every multi-person emoji, so flagging them would make the audit unusable for most of the world's writing systems |
| `instruction_markup` | `<IMPORTANT>`, `<system>`, `[INST]`, `<\|…\|>` — imitating the prompt's framing rather than describing a tool |
| `sensitive_path` | `~/.ssh`, `.env`, `~/.aws/credentials` and similar |
| `cross_server_reference` | one server's description naming a tool owned by another (names under 4 characters are ignored — they match inside ordinary words). A name is only "another server's" when this server does not offer it too: `search`, `read` and `list` collide across servers constantly, and treating a collision as ownership accused a server of describing its own tool |

Every check is **structural, never lexical**. Matching English phrases
would miss an attack written in another language while flagging honest
descriptions that use those words, which is why
`tests/test_mcp_audit.py` asserts that French, Japanese, Russian and
Arabic descriptions raise nothing.

Schemas are serialised with `ensure_ascii=False`; the default would turn
a hidden `U+202E` into the visible literal `‮` and defeat the
invisible-character check.

## Remote servers

`_connect` dispatches on `transport`. `stdio` spawns a subprocess;
`http` / `https` / `streamable_http` / `streamable-http` open a
Streamable HTTP connection. The deprecated SSE transport is deliberately
absent. Both shapes yield the same `(read, write)` pair, so the
persistent runtime's sessions, serialisation, idle reaping and
retry-once-on-death behave identically either way.

A URL is attacker-influenceable in ways a command path is not, so
`_validate_remote_url` runs before anything opens:

| Input | Result |
|-------|--------|
| `https://host/mcp` | allowed |
| `http://localhost/mcp`, `http://127.0.0.1/mcp`, `http://[::1]/mcp` | allowed — a loopback hop cannot be observed off the machine |
| `http://remote-host/mcp` | refused — the bearer token would cross the network in clear text |
| `file://`, `ftp://`, anything else | refused |
| `https://user:pass@host/mcp` | refused — URL credentials leak into logs, proxies and Referer headers |
| no host, or no `url` at all | refused, naming the server |

The supply-chain guard has nothing to say about a remote server: there
is no registry resolution step, so nothing to pin.

## Authentication for remote servers

Two modes, chosen by `auth` on the server entry.

**Static token** (default). Read from the credential store under
`mcp_token:<server>` and sent as `Authorization: Bearer`. It
deliberately overrides any `Authorization` header left in `config.json`,
so rotating the token in the keychain takes effect instead of being
shadowed by a stale value.

**OAuth** (`"auth": "oauth"`). The user clicks Connect, their own
browser opens at their own provider, and a token arrives without them
ever seeing it. PKCE and `state` verification are handled by the SDK's
provider; this module supplies the pieces it has no opinion about:

- `LoopbackCallback` binds a one-shot listener to `127.0.0.1` on an
  ephemeral port. Loopback is the desktop OAuth pattern because there is
  no server to host a redirect on, and a code delivered there cannot be
  intercepted from off the machine. The registered `redirect_uri` is the
  exact URI the browser is later sent back to.
- The handler's access log is silenced: the default would print the
  request line, and that line contains the authorisation code. The page
  the user is left on carries no parameters either.
- `KeychainTokenStorage` persists tokens and the client registration in
  the OS credential store, keyed per server so two servers cannot share
  a grant. Without it every restart would send the user back through the
  browser. This matters more than for an API key: a refresh token is a
  standing grant on the user's account, and MCP subprocesses can read
  `config.json`.
- A corrupt stored token or registration logs and reads as absent, so
  the next call re-authorises rather than the daemon failing to start.
- Under OAuth the provider owns the `Authorization` header and refreshes
  it as it expires, so no static bearer is added alongside it.

`mcp_oauth.forget(server)` clears both entries — the Disconnect action.

## The server's environment

`_connect_stdio` always builds the environment explicitly. `env=None`
does not inherit: the SDK substitutes `get_default_environment()`, only
HOME, PATH, SHELL and TERM, which strips proxy settings,
`NODE_EXTRA_CA_CERTS` and registry credentials and leaves an npx-based
server hanging until the setup timeout with nothing on stderr.

What is inherited excludes anything whose *name* says it holds a
credential (`*_TOKEN`, `*_SECRET`, `*_API_KEY`, `*_PASSWORD`,
`*_ACCESS_KEY`, …). A server is third-party code, and handing it the
user's whole shell would give every installed package a copy of any
cloud key exported there — which is the same threat the credential store
below exists to answer. A server that genuinely needs one declares it in
its own `env` block, which is merged on top and always wins; that block
is the sanctioned way to pass a secret to a server. `SSH_AUTH_SOCK` and
`GPG_AGENT_INFO` match the patterns but carry no secret, so they pass.

## Credentials

`jarvis.utils.secret_store` keeps API keys in the OS credential store
(Keychain, Credential Manager, Secret Service) instead of `config.json`.
This matters here specifically: MCP server subprocesses run as the user
and can read that file.

- A key leaves `config.json` only once the store returns the same value.
  A machine with no working backend keeps its plaintext key rather than
  losing the user's only copy.
- A write that cannot be read back is rolled back. On macOS storing an
  item usually needs no approval while reading one back does, so denying
  that prompt fails verification with the value already in the keychain.
  The rollback goes through the backend handle directly, because the
  failed read has already latched the store off for the session and
  `delete_secret` would quietly do nothing. Leaving the orphan would put
  a copy of the key in a store the user was never told about, and
  `resolve_secret` prefers the store when config.json is empty — so it
  would silently resurrect a key they later thought they had removed.
- The sweep runs on every config load, not once at a version bump. The
  Settings window writes API keys straight back into `config.json`, so a
  version-gated migration would cover only keys that predated the
  upgrade and leave every later one in plain text.
- Resolved values are cached per process. `load_settings()` is on a hot
  path, and each lookup is a synchronous DBus round trip on Linux and can
  raise a blocking "allow access" dialog on macOS.
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
| `tests/test_mcp_gate.py` | that a gated call is blocked with YOLO off and runs with it on, that the window expiring blocks again, and that no invented argument stands in for the grant |
| `tests/test_mcp_audit.py` | each check, non-English descriptions raising nothing, and ordinary text not tripping the markup or path checks |
| `tests/test_yolo.py` | the window opening, expiring, being revoked and clamped, and that no tool can grant it |
| `tests/test_secret_store.py` | round trip, honest unavailability, read-back before trusting a write, migration, `BaseException` backends, no recursion |
| `tests/test_mcp_catalogue.py` | every shipped catalogue entry passes the launch guard |
| `tests/test_mcp_review_fixes.py` | the separator and subcommand grammars, annotation edge cases, joiner-using scripts, colliding tool names, withheld tools being uncallable, credential-shaped variables, and `mcp_confirm` reaching the policy from a real config file |
| `tests/test_mcp_remote_transport.py` | transport dispatch, URL validation per scheme and host, the token coming from the credential store rather than a config header, and a bad URL being refused before any connection opens |
| `tests/test_mcp_oauth.py` | token and registration round trips, per-server isolation, corrupt entries reading as absent, the listener binding to loopback, the success page not echoing the code, and OAuth suppressing the static bearer header |

## Non-goals

- **Sandboxing servers.** A server still runs with the user's
  privileges. Containerised distribution would change that; pinning
  only fixes *which* code runs.
- **Proving a package is safe.** A pin makes the code reproducible, not
  trustworthy.
- **RFC 9207 `iss` validation and audience checks.** Both belong to the
  2026 spec revision; the pinned SDK's provider does not implement them,
  so a malicious authorisation server response is not fully defended
  against yet.
- **Rate limiting the dashboard**, which remains gated by its session
  token alone.
