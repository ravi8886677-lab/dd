"""Behavioural tests for the MCP supply-chain guard.

The guard's job is narrow: before Jarvis spawns an MCP server
subprocess, refuse any launch that would auto-install an unpinned
package from a public registry. That is a remote-code-execution channel
which re-arms on every start, so the check runs at spawn time rather
than at config-write time.
"""

import pytest

from jarvis.tools.external.mcp_supply_chain import (
    UnpinnedServerError,
    describe_package_specs,
    validate_server_launch,
)


def _cfg(command, args, **extra):
    return {"transport": "stdio", "command": command, "args": list(args), **extra}


# ---------------------------------------------------------------------------
# npm / npx
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "args",
    [
        ["-y", "some-server@latest"],
        ["-y", "some-server@next"],
        ["-y", "some-server"],                  # bare name resolves to latest
        ["-y", "some-server@^1.2.3"],
        ["-y", "some-server@~1.2.3"],
        ["-y", "some-server@1.x"],
        ["-y", "some-server@>=1.0.0"],
        ["-y", "some-server@*"],
        ["-y", "@scope/some-server"],           # scoped, no version
        ["-y", "@scope/some-server@latest"],
    ],
)
def test_unpinned_npm_specs_are_refused(args):
    with pytest.raises(UnpinnedServerError):
        validate_server_launch("srv", _cfg("npx", args))


@pytest.mark.parametrize(
    "args",
    [
        ["-y", "some-server@1.2.3"],
        ["-y", "@scope/some-server@0.4.6"],
        ["-y", "some-server@2025.4.25"],
        ["-y", "some-server@1.2.3-beta.1"],
        ["-y", "@scope/some-server@1.0.0+build.5"],
    ],
)
def test_exactly_pinned_npm_specs_are_allowed(args):
    validate_server_launch("srv", _cfg("npx", args))


def test_npx_dash_p_flag_carries_the_package():
    """``npx -p pkg runner`` installs ``pkg``; the runner name is not a spec."""
    with pytest.raises(UnpinnedServerError):
        validate_server_launch("srv", _cfg("npx", ["-p", "some-server", "runner"]))
    validate_server_launch("srv", _cfg("npx", ["-p", "some-server@1.2.3", "runner"]))


def test_npx_package_equals_form():
    with pytest.raises(UnpinnedServerError):
        validate_server_launch("srv", _cfg("npx", ["--package=some-server", "runner"]))
    validate_server_launch("srv", _cfg("npx", ["--package=some-server@1.2.3", "runner"]))


def test_windows_npx_cmd_is_also_checked():
    with pytest.raises(UnpinnedServerError):
        validate_server_launch("srv", _cfg("npx.cmd", ["-y", "some-server@latest"]))


# ---------------------------------------------------------------------------
# PyPI / uvx
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "args",
    [
        ["some-server"],
        ["some-server>=1.0"],
        ["some-server~=1.2"],
        ["--from", "some-server", "tool"],
    ],
)
def test_unpinned_pypi_specs_are_refused(args):
    with pytest.raises(UnpinnedServerError):
        validate_server_launch("srv", _cfg("uvx", args))


@pytest.mark.parametrize(
    "args",
    [
        ["some-server==0.2.1"],
        ["--from", "some-server==0.2.1", "tool"],
        ["--from=some-server==0.2.1", "tool"],
        ["some-server[extra]==0.2.1"],
    ],
)
def test_exactly_pinned_pypi_specs_are_allowed(args):
    validate_server_launch("srv", _cfg("uvx", args))


def test_uvx_dash_p_is_a_python_version_not_a_package():
    """``uvx -p 3.11 pkg==1.0`` selects an interpreter; only ``pkg`` is a spec.

    npx and uvx both use ``-p`` but mean different things by it. Reading
    uvx's ``-p`` as a package would reject a perfectly pinned launch.
    """
    validate_server_launch("srv", _cfg("uvx", ["-p", "3.11", "some-server==1.0.0"]))


def test_uvx_with_flag_packages_must_also_be_pinned():
    with pytest.raises(UnpinnedServerError):
        validate_server_launch(
            "srv", _cfg("uvx", ["--with", "extra-pkg", "some-server==1.0.0"])
        )
    validate_server_launch(
        "srv", _cfg("uvx", ["--with", "extra-pkg==2.0.0", "some-server==1.0.0"])
    )


def test_pypi_wildcard_pin_is_not_a_pin():
    with pytest.raises(UnpinnedServerError):
        validate_server_launch("srv", _cfg("uvx", ["some-server==1.*"]))


# ---------------------------------------------------------------------------
# Commands that are not auto-installing launchers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "command,args",
    [
        ("/usr/bin/node", ["/home/me/server.js"]),
        ("python", ["-m", "my_server"]),
        ("/bin/true", []),
        ("docker", ["run", "--rm", "some/image:1.2.3"]),
    ],
)
def test_non_launcher_commands_are_not_subject_to_pinning(command, args):
    validate_server_launch("srv", _cfg(command, args))


def test_local_paths_are_not_registry_installs():
    """A path is already on disk — there is no registry fetch to pin."""
    validate_server_launch("srv", _cfg("npx", ["-y", "./local-server"]))
    validate_server_launch("srv", _cfg("npx", ["-y", "/opt/servers/thing"]))
    validate_server_launch("srv", _cfg("npx", ["-y", "file:../sibling"]))


def test_launcher_with_no_package_argument_is_allowed():
    validate_server_launch("srv", _cfg("npx", []))


# ---------------------------------------------------------------------------
# Escape hatch and diagnostics
# ---------------------------------------------------------------------------

def test_per_server_opt_out_allows_an_unpinned_launch():
    validate_server_launch(
        "srv", _cfg("npx", ["-y", "some-server@latest"], allow_unpinned=True)
    )


def test_opt_out_must_be_a_real_boolean_true():
    """A truthy string must not silently disable the guard."""
    with pytest.raises(UnpinnedServerError):
        validate_server_launch(
            "srv", _cfg("npx", ["-y", "some-server@latest"], allow_unpinned="no")
        )


def test_error_names_the_server_and_the_offending_spec():
    with pytest.raises(UnpinnedServerError) as excinfo:
        validate_server_launch("chrome-devtools", _cfg("npx", ["-y", "thing@latest"]))
    message = str(excinfo.value)
    assert "chrome-devtools" in message
    assert "thing@latest" in message
    assert "allow_unpinned" in message


def test_describe_package_specs_reports_what_would_be_fetched():
    assert describe_package_specs("npx", ["-y", "a@1.0.0"]) == ["a@1.0.0"]
    assert describe_package_specs("uvx", ["--with", "b==2.0", "a==1.0"]) == [
        "b==2.0",
        "a==1.0",
    ]
    assert describe_package_specs("/usr/bin/node", ["server.js"]) == []


# ---------------------------------------------------------------------------
# Enforcement at the spawn boundary
# ---------------------------------------------------------------------------

def test_unpinned_server_never_reaches_subprocess_spawn(monkeypatch):
    """The guard must run before ``stdio_client``, not after.

    Checking after the spawn would still have executed the fetched code,
    which is the whole thing being prevented.
    """
    from jarvis.tools.external.mcp_client import MCPClient

    spawned = {"n": 0}

    def fake_stdio_client(params, **kw):
        spawned["n"] += 1
        return None

    monkeypatch.setattr(
        "jarvis.tools.external.mcp_client.stdio_client", fake_stdio_client
    )
    monkeypatch.setattr(
        "jarvis.tools.external.mcp_client._resolve_command", lambda c: c
    )

    client = MCPClient({"bad": _cfg("npx", ["-y", "thing@latest"])})
    with pytest.raises(UnpinnedServerError):
        client._connect_stdio(client.server_configs["bad"], "bad")

    assert spawned["n"] == 0, "a subprocess was spawned despite the guard"


def test_pinned_server_still_reaches_subprocess_spawn(monkeypatch):
    from jarvis.tools.external.mcp_client import MCPClient

    spawned = {"n": 0}

    def fake_stdio_client(params, **kw):
        spawned["n"] += 1
        return None

    monkeypatch.setattr(
        "jarvis.tools.external.mcp_client.stdio_client", fake_stdio_client
    )
    monkeypatch.setattr(
        "jarvis.tools.external.mcp_client._resolve_command", lambda c: c
    )

    client = MCPClient({"good": _cfg("npx", ["-y", "thing@1.2.3"])})
    client._connect_stdio(client.server_configs["good"], "good")

    assert spawned["n"] == 1
