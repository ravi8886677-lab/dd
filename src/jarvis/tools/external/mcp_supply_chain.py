"""Supply-chain guard for MCP server launches.

Most MCP servers in the wild are distributed as ``npx <package>`` or
``uvx <package>``. Both launchers fetch and execute the package at
startup. When the spec carries no exact version, that fetch resolves to
whatever the registry serves at that moment, so every launch is a fresh
opportunity for a compromised or hijacked package to run with the
daemon's privileges. Jarvis drives a mouse and a keyboard, so that is
not a theoretical cost.

This module answers one question: *would this launch install code from a
registry without saying exactly which code?* If so, the launch is
refused. Everything else (local paths, interpreters, container
runtimes) is left alone, because there is no registry resolution step to
pin.

The check runs at spawn time, not at config-write time, so a config
edited by hand or by another process is still covered.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence

from ...debug import debug_log


class UnpinnedServerError(RuntimeError):
    """An MCP server launch would auto-install an unpinned package."""


# Launchers that resolve a package spec against a public registry and
# execute it. ``.cmd``/``.exe`` variants are how these appear on Windows.
_NPM_LAUNCHERS = {"npx", "npx.cmd", "pnpx", "pnpx.cmd", "bunx", "bunx.cmd"}
_PYPI_LAUNCHERS = {"uvx", "uvx.exe", "pipx", "pipx.exe"}

# npx flags that take a value which is *not* a package spec. An unknown
# flag is assumed to be a boolean, so its following token is read as the
# package. That errs towards refusing a launch we cannot parse rather
# than waving through one we misread, but it means a value-taking flag
# missing from this set produces a confusing refusal — hence the list is
# kept generous.
_NPM_VALUE_FLAGS_TO_SKIP = {
    "-c", "--call", "-w", "--workspace", "--userconfig", "--loglevel",
    "--registry", "--cache", "--prefix", "--shell", "--node-arg",
    "--otp", "--before", "--engine-strict", "--scripts-prepend-node-path",
    "--node-options", "--npm-path", "--proxy", "--https-proxy", "--noproxy",
    "--userconfig-path", "--globalconfig", "--fetch-timeout", "--fetch-retries",
}
# npx flags whose value *is* a package spec.
_NPM_PACKAGE_FLAGS = {"-p", "--package"}

# uvx flags whose value is a package spec.
_PYPI_PACKAGE_FLAGS = {"--from", "--with", "--with-requirements"}
# uvx flags that take a value which is not a package spec. ``-p`` is
# ``--python`` for uvx, unlike npx where it means ``--package``.
_PYPI_VALUE_FLAGS_TO_SKIP = {
    "-p", "--python", "--index-url", "--extra-index-url",
    "--index", "--cache-dir", "--constraints", "-c", "--index-strategy",
    "--keyring-provider", "--exclude-newer", "--resolution", "--prerelease",
    "--config-file", "--project", "--directory", "--refresh-package",
}

# Launchers that take a subcommand before the package spec.
_SUBCOMMAND_LAUNCHERS = {
    "pipx": {"run"},
    "pipx.exe": {"run"},
}

# An exact npm version: 1.2.3, with optional prerelease/build metadata.
# Ranges (^, ~, x, >=, *) and dist-tags (latest, next) are excluded by
# construction — they all fail this shape.
_EXACT_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")

# An exact PEP 440 pin: ``name==1.2.3``, optionally with extras. A
# trailing ``.*`` is a range, not a pin.
_EXACT_PEP440 = re.compile(
    r"^[A-Za-z0-9._-]+(?:\[[^\]]+\])?==\s*[0-9][^,;\s]*$"
)

# A git URL is pinned only when it names a full commit SHA.
_GIT_COMMIT_PIN = re.compile(r"[@#][0-9a-f]{40}$")

_LOCAL_PREFIXES = ("./", "../", "/", "~", "file:", ".\\", "..\\")


def _is_local_reference(spec: str) -> bool:
    """Whether ``spec`` points at something already on disk."""
    if spec.startswith(_LOCAL_PREFIXES):
        return True
    # Windows drive-absolute path, e.g. C:\servers\thing
    return bool(re.match(r"^[A-Za-z]:[\\/]", spec))


def _is_url(spec: str) -> bool:
    return bool(re.match(r"^(?:https?|git\+https?|git|ssh)://", spec))


def _npm_spec_is_pinned(spec: str) -> bool:
    """Whether an npm package spec names one immutable version."""
    if _is_local_reference(spec):
        return True
    if _is_url(spec):
        return bool(_GIT_COMMIT_PIN.search(spec))

    if spec.startswith("@"):
        # Scoped: @scope/name@version
        body = spec[1:]
        if "/" not in body:
            return False
        after_scope = body.split("/", 1)[1]
        if "@" not in after_scope:
            return False
        version = after_scope.split("@", 1)[1]
    else:
        if "@" not in spec:
            return False
        version = spec.split("@", 1)[1]

    return bool(_EXACT_SEMVER.match(version))


def _pypi_spec_is_pinned(spec: str) -> bool:
    """Whether a PyPI requirement names one immutable version."""
    if _is_local_reference(spec):
        return True
    if _is_url(spec):
        return bool(_GIT_COMMIT_PIN.search(spec))
    if spec.endswith(".*"):
        return False
    return bool(_EXACT_PEP440.match(spec))


def _collect_npm_specs(args: Sequence[str]) -> List[str]:
    """Return the npm package specs ``npx`` would install for ``args``."""
    specs: List[str] = []
    saw_explicit_package = False
    after_separator = False
    index = 0
    while index < len(args):
        token = str(args[index])

        # ``npx [options] -- <pkg>`` is documented usage: the separator
        # ends npx's own options, and the package follows it. Stopping
        # here would report no packages at all, and no packages means
        # nothing to pin — one stray ``--`` would switch the guard off.
        if token == "--" and not after_separator:
            after_separator = True
            index += 1
            continue

        if after_separator:
            if not saw_explicit_package:
                specs.append(token)
            break

        flag, _, inline_value = token.partition("=")
        if flag in _NPM_PACKAGE_FLAGS:
            if inline_value:
                specs.append(inline_value)
                index += 1
            elif index + 1 < len(args):
                specs.append(str(args[index + 1]))
                index += 2
            else:
                index += 1
            saw_explicit_package = True
            continue

        if flag in _NPM_VALUE_FLAGS_TO_SKIP:
            index += 1 if inline_value else 2
            continue

        if token.startswith("-"):
            index += 1
            continue

        # With ``--package``/``-p``, the bare token is the binary to run
        # from that package rather than a separate install. Otherwise the
        # first bare token is the package itself; anything after it is an
        # argument to that package.
        if not saw_explicit_package:
            specs.append(token)
        break

    return specs


def _collect_pypi_specs(args: Sequence[str], launcher: str = "uvx") -> List[str]:
    """Return the PyPI requirements ``uvx``/``pipx`` would install for ``args``."""
    specs: List[str] = []
    saw_explicit_package = False
    after_separator = False
    subcommands = _SUBCOMMAND_LAUNCHERS.get(launcher, set())
    index = 0
    while index < len(args):
        token = str(args[index])

        # See ``_collect_npm_specs``: the separator precedes the package,
        # it does not mean there is none.
        if token == "--" and not after_separator:
            after_separator = True
            index += 1
            continue

        if after_separator:
            if not saw_explicit_package:
                specs.append(token)
            break

        # ``pipx run <spec>`` puts a subcommand where uvx puts the spec.
        if token in subcommands:
            subcommands = set()
            index += 1
            continue

        flag, _, inline_value = token.partition("=")
        if flag in _PYPI_PACKAGE_FLAGS:
            if inline_value:
                specs.append(inline_value)
                index += 1
            elif index + 1 < len(args):
                specs.append(str(args[index + 1]))
                index += 2
            else:
                index += 1
            if flag == "--from":
                saw_explicit_package = True
            continue

        if flag in _PYPI_VALUE_FLAGS_TO_SKIP:
            index += 1 if inline_value else 2
            continue

        if token.startswith("-"):
            index += 1
            continue

        # With ``--from``, the bare token is the command name inside that
        # package rather than a separate requirement.
        if not saw_explicit_package:
            specs.append(token)
        break

    return specs


def describe_package_specs(command: str, args: Sequence[str]) -> List[str]:
    """Return the registry package specs this launch would install.

    Empty when the command is not an auto-installing launcher — there is
    nothing to pin in that case.
    """
    launcher = command.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    if launcher in _NPM_LAUNCHERS:
        return _collect_npm_specs(args or [])
    if launcher in _PYPI_LAUNCHERS:
        return _collect_pypi_specs(args or [], launcher)
    return []


def _spec_is_pinned(command: str, spec: str) -> bool:
    launcher = command.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    if launcher in _NPM_LAUNCHERS:
        return _npm_spec_is_pinned(spec)
    return _pypi_spec_is_pinned(spec)


def validate_server_launch(server_name: str, server_cfg: Dict[str, Any]) -> None:
    """Raise ``UnpinnedServerError`` if this launch auto-installs unpinned code.

    ``allow_unpinned: true`` on the server's own config entry opts that
    one server out. It must be a real boolean — a truthy string would
    make the guard trivially easy to disable by accident.
    """
    command = str(server_cfg.get("command") or "")
    args = server_cfg.get("args") or []
    specs = describe_package_specs(command, args)
    if not specs:
        return

    unpinned = [s for s in specs if not _spec_is_pinned(command, s)]
    if not unpinned:
        return

    if server_cfg.get("allow_unpinned") is True:
        debug_log(
            f"MCP server '{server_name}' launches unpinned {unpinned} "
            "(allow_unpinned is set)",
            "mcp",
        )
        return

    offending = ", ".join(unpinned)
    raise UnpinnedServerError(
        f"MCP server '{server_name}' would install {offending} from a public "
        "registry without an exact version, so its code can change on any "
        "launch. Pin it (e.g. 'package@1.2.3' for npx, 'package==1.2.3' for "
        "uvx), or set \"allow_unpinned\": true on that server in config.json "
        "if you accept the risk."
    )
