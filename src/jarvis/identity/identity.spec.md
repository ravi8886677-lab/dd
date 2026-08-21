# Identity

Who Jarvis is acting for, which workspace that work belongs to, where it
is running, and which outside accounts it has been given.

There is one person on one machine. The rows exist anyway, because the
things built on top of them cannot be expressed without something to
point at: a permission that applies to one tool on one device, an audit
entry that records which machine acted, a connected account that carries
its own scope. Adding identity after those means rebuilding those.

## What exists

Four entities, in the same SQLite file as the diary and the knowledge
graph. Four rather than the full data model, because the rest arrives
with the features that need it and empty tables rot.

| Entity | Holds | Notes |
|---|---|---|
| `users` | the person | One row per install today. Nothing assumes there is only one. |
| `workspaces` | what a piece of work belongs to | One `personal` row is created for the user. `kind` is open, so a second one needs no migration. |
| `devices` | the machines this user runs on | Keyed by the local device identifier, one row per machine, last-seen refreshed on every launch. |
| `connected_accounts` | outside accounts the user has connected | Holds a **reference** to a credential, never a credential. Empty until connectors exist. |

## The store owns its schema

`IdentityStore` applies the schema on construction, the same contract as
`GraphMemoryStore`. Either process may be first: the daemon establishes
identity on the way up, and the dashboard can be opened on a machine
where the daemon has never run.

`ensure_local_identity()` is idempotent and is what both call. It returns
the user, the personal workspace and this device, creating only what is
missing, and marks the device as seen. Calling it twice adds nothing.

## A device is a machine, not a database

The device identifier lives in a file in the data directory, beside the
database rather than inside it. Rebuilding, moving or restoring the
database must not make Jarvis believe it woke up somewhere new, because
a per-device permission would be silently dropped at exactly the moment
it mattered.

It is a random identifier generated on first use. It is not derived from
the hostname, which changes, nor from any hardware address, which is the
kind of thing this project does not collect. If it cannot be written
(a read-only home), Jarvis still runs; the machine is simply not
remembered between launches.

## Credentials are referenced, never stored

`connected_accounts.secret_ref` is a name to look up through
`utils.secret_store`, which is the OS keychain. A password in a row is a
password in every backup of that row. Unlinking an account removes the
row; removing the credential is the keychain's job.

## Failing open

Establishing identity is not a gate. If the store cannot be opened, the
daemon says so and carries on, because nothing yet depends on the rows.
When something does, it gates on the rows being absent, not on Jarvis
having refused to start.

## What the user sees

Startup names the machine and the workspace it is acting in. The
dashboard shows the same device in its header, with the other devices on
the account in the tooltip, which is how a user can tell that the window
in front of them is not showing another machine's view.
