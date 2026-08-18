"""
Tests that each config migration stamps its own version number.

A migration block runs when the stored version is below its number, and
records that it ran by writing that number back. Writing the *current*
`CONFIG_VERSION` instead looks identical while there is no later block: the
value is the same, the tests pass, the upgrade works. It breaks the moment
someone adds the next migration, because the older block now stamps the new
version and everything after it is skipped for good.

Nothing at runtime can catch that. The config claims to be current, so the
skipped migration is not pending, it is lost, and the user's config is
quietly left in a shape no code path expects. This reads the structure
instead, so a block that stamps the wrong number fails when it is written
rather than when the next one is added.
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

CONFIG_PATH = Path(__file__).resolve().parents[1] / "src" / "jarvis" / "config.py"


def _migration_function() -> ast.FunctionDef:
    """The function holding the `migration_version < N` chain.

    Found by shape, not by name. The chain has already moved once — a
    re-entrancy guard took the `_migrate_config` name and the blocks went to
    a helper — and a detector pinned to a name does not fail when that
    happens, it silently inspects the wrong function and finds nothing to
    complain about.
    """
    tree = ast.parse(CONFIG_PATH.read_text(encoding="utf-8"))
    candidates = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(_guarded_version(b.test) is not None
                for b in node.body if isinstance(b, ast.If))
    ]
    assert candidates, (
        "no function in config.py contains a `migration_version < N` block; "
        "the migration chain has moved or changed shape, and this guard is "
        "no longer reading it"
    )
    assert len(candidates) == 1, (
        f"the migration chain appears in {len(candidates)} functions: "
        f"{[c.name for c in candidates]}"
    )
    return candidates[0]


def _migration_blocks():
    """Every `migration_version < N` block, as (version, block) pairs."""
    blocks = [
        (_guarded_version(b.test), b)
        for b in _migration_function().body if isinstance(b, ast.If)
    ]
    found = [(v, b) for v, b in blocks if v is not None]
    assert found, "no migration blocks found; this guard would pass vacuously"
    return found


def _guarded_version(test: ast.expr):
    """The N in `if migration_version < N:`, or None if that isn't the shape."""
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return None
    if not isinstance(test.ops[0], ast.Lt):
        return None
    if not (isinstance(test.left, ast.Name) and test.left.id == "migration_version"):
        return None
    right = test.comparators[0]
    return right.value if isinstance(right, ast.Constant) else None


def _version_stamps(block: ast.If):
    """Every value assigned to cfg_json["_config_version"] inside a block."""
    for node in ast.walk(block):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Subscript):
                continue
            key = target.slice
            if isinstance(key, ast.Constant) and key.value == "_config_version":
                yield node.value, node.lineno


def test_each_migration_stamps_its_own_number():
    """A block that stamps CONFIG_VERSION skips every migration added after it."""
    problems = []
    for version, block in _migration_blocks():
        for value, lineno in _version_stamps(block):
            if isinstance(value, ast.Constant):
                if value.value != version:
                    problems.append(
                        f"config.py:{lineno}: the `migration_version < {version}` block "
                        f"stamps {value.value!r}"
                    )
            else:
                written = ast.unparse(value)
                problems.append(
                    f"config.py:{lineno}: the `migration_version < {version}` block "
                    f"stamps `{written}` instead of the literal {version} — the next "
                    f"migration added after it will never run"
                )

    assert not problems, "\n  " + "\n  ".join(problems)


def test_every_migration_block_records_that_it_ran():
    """A block that never stamps re-runs on every load."""
    unstamped = []
    for version, block in _migration_blocks():
        if not list(_version_stamps(block)):
            unstamped.append(f"the `migration_version < {version}` block writes no version")

    assert not unstamped, "\n  " + "\n  ".join(unstamped)


def test_the_blocks_cover_every_version_up_to_the_constant():
    """A gap means a config at that version migrates through nothing."""
    from jarvis.config import CONFIG_VERSION

    found = sorted(v for v, _block in _migration_blocks())
    assert found == list(range(1, CONFIG_VERSION + 1)), (
        f"migration blocks {found} do not cover 1..{CONFIG_VERSION}"
    )
