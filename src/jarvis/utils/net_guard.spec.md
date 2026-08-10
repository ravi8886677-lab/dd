# Network guard (SSRF) — spec

`net_guard.py` is the single gate every outbound fetch of an untrusted URL
passes through. `webSearch` and `fetchWebPage` both use it.

## Why it exists

Jarvis fetches URLs it did not get from the user:

- a link on a search results page,
- a link on a page it already read,
- an address an MCP tool description talked the model into using.

Jarvis runs inside the user's trust boundary, so an unguarded fetch is a
confused-deputy primitive: the model can be induced to request
`169.254.169.254` (cloud metadata), a router admin page on the LAN, or a
service bound to loopback, and hand the response back to whoever asked.

## Contract

```python
check_url(url) -> None                  # raises on refusal
is_public_url(url) -> bool              # the same check, as a predicate
guarded_get(url, **kw) -> Response      # validated GET, redirects walked by hand
```

### What is refused

| Category | Examples |
|---|---|
| Non-`http(s)` schemes | `file://`, `ftp://`, `javascript:`, `data:` |
| Loopback | `127.0.0.0/8`, `::1` |
| Private | `10/8`, `172.16/12`, `192.168/16` |
| Link-local | `169.254/16` (cloud metadata), `fe80::/10` |
| Reserved, multicast, unspecified | `240.0.0.0/4`, `224.0.0.1`, `0.0.0.0` |
| IPv4-mapped IPv6 wrapping any of the above | `::ffff:127.0.0.1` |

A hostname is refused when **any** address it resolves to is non-public. A
hostile resolver can answer `[1.1.1.1, 127.0.0.1]`, and clients differ on
which record they try, so the worst record decides.

### Refusal reasons are distinct types

`UnsafeURLError` is the base. Callers that need to explain themselves branch
on the subclass:

| Exception | Meaning | What the caller should tell the model |
|---|---|---|
| `NonPublicAddressError` | Refused by policy | Do not retry this address in another form |
| `UnresolvableHostError` | DNS did not answer | An ordinary fetch failure; retrying is legitimate |
| `TooManyRedirectsError` | Chain outran its cap, every hop public | An ordinary fetch failure |

Collapsing these would make a typo or a DNS outage look like a security
refusal, and would teach the model to give up on addresses that were only
temporarily unresolvable.

### Redirects

`guarded_get` sets `allow_redirects=False` and walks the chain itself,
re-validating each hop before requesting it. This is the whole point: by the
time `requests` returns from a followed chain, a hop through `127.0.0.1` has
already been fetched. Relative `Location` headers resolve against the current
URL. The chain is capped at `max_redirects` (default 3).

`guarded_get` does not inspect the final status code; callers keep their own
error handling.

## Known limitation: DNS rebinding

The address is resolved once for the check and again by the HTTP client when
it connects. A resolver that answers differently between those two moments
can still get a request through. Closing this needs the connection pinned to
the validated address, which the `requests` API does not expose. The guard
raises the cost of an attack substantially; it is not a complete mitigation,
and should not be described as one.

## Testing

`tests/test_net_guard.py` covers the guard directly. Two rules:

- **Assert the request was never issued.** A refusal that still fires the
  request has failed at its only job, and a test that merely checks the
  return value cannot tell the difference.
- **Stub DNS rather than resolving for real.** Jarvis is offline-first and
  its suite must run with no network. Tests that mock `requests.get` must
  also stub `socket.getaddrinfo`, and any stand-in response object needs
  `is_redirect` / `is_permanent_redirect` set — a bare `Mock` returns a
  truthy value for both and reads as an endless redirect.
