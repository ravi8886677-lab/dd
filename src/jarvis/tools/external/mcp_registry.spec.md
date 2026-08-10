# MCP registry — spec

`mcp_registry.py` reads the official registry at
`registry.modelcontextprotocol.io` and caches it locally. The dashboard
renders it under the curated catalogue, which stays the default view.

## Why it is allowed at all

Jarvis is offline-first, and the dashboard fetches nothing external while
rendering. Two properties keep the registry on the right side of that line:

- **Metadata only.** Reads need no authentication and carry nothing about
  the user. Jarvis asks for a list of packages; it does not say who is
  asking or what they were doing. No user audio, text or diary content
  leaves the machine.
- **Cached, and browsed from the cache.** `GET /api/mcp/registry` reads
  disk and never opens a socket. Fetching is a separate explicit action
  (`POST /api/mcp/registry/refresh`) behind a button. Running with no
  network is supported, so a directory that needed one would be a
  directory that vanished.

It is also not a vendor: an open registry any client may read, with no
account and no lock-in.

## What the registry proves, and what it does not

The registry verifies **namespace ownership**. `io.github.acme/thing` means
the publisher authenticated to the `acme` GitHub account;
`com.example/thing` means they control `example.com`.

That is an identity signal. It says *who* published a server. It says
nothing about whether the code is safe, and nothing here reviews it.

The UI therefore shows the namespace with a label naming what was checked
("GitHub account checked", "Domain checked") rather than a tick. There is no
"verified" badge, no `verified` field, and
`tests/test_memory_viewer_catalogue_api.py` asserts no badge-like label
appears in the markup. Presence in the registry is not an endorsement, and
an interface that implied otherwise would undo the reasoning behind every
other defence in `mcp_security.spec.md`.

## Normalisation

| Registry shape | Entry |
|---|---|
| `server.name` | `name`, split into `namespace` + the rest |
| `packages[]` with `registryType` npm/pypi and a `version` | `install`: a pinned `npx`/`uvx` launch |
| `packages[]` without a version, or another ecosystem | `install: None` |
| `remotes[0].url` | `remote_url` |
| `_meta[…].isLatest == false` | dropped |
| `_meta[…].status != active` | dropped |

**`install` is `None` unless a pinned launch can be built.** An unpinned
package, or one from an ecosystem the supply-chain guard cannot pin, gets no
Add button. Offering one would write a config that
`validate_server_launch` refuses at spawn time, which is the exact failure
the connections directory exists to prevent. `tests/test_mcp_registry.py`
runs the real guard over the generated configs.

The registry returns every published version of every server; only the
current one belongs in a directory.

## Cache

Written next to `config.json` as `mcp_registry_cache.json`.

- The cache is replaced **only after a fetch succeeds**, so a failed refresh
  leaves the previous listing intact rather than emptying the directory.
- A missing or corrupt cache reads as empty, never raises.
- `fetched_at` is served with the entries and shown on screen. A cached
  directory that hides its age invites acting on stale data.
- A failed refresh reports 503. Silently serving the old cache as though it
  were fresh would be the dishonest failure.
