"""
Tests that no function body continues past a statement that always exits.

Code after an unconditional `return` or `raise` is legal Python and silent:
no warning at import, no error at runtime, and the suite stays green because
the lines simply never execute. That makes it the shape a mid-method
insertion leaves behind. Inserting a method into the middle of `__init__`
strands the rest of `__init__` after the new method's return, so attributes
the class promises are never assigned and every reader of them raises
`AttributeError` on a path no test happens to walk.

This is structural, so it runs in milliseconds, needs no display and no
audio device, and it holds for every file under `src/` rather than the one
where it last went wrong.
"""

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"


def _always_exits(statement: ast.stmt) -> bool:
    """Whether control can never continue past this statement.

    Two shapes count. An unconditional `return`/`raise` is the obvious one.
    A `try` whose body and every handler end in a return is the one worth
    naming: it reads as a guarded lookup rather than a wall, which is
    exactly why anything left after it looks reachable.
    """
    if isinstance(statement, (ast.Return, ast.Raise)):
        return True

    if isinstance(statement, ast.Try):
        if statement.orelse or statement.finalbody or not statement.handlers:
            return False
        endings = [statement.body[-1]] if statement.body else []
        for handler in statement.handlers:
            if not handler.body:
                return False
            endings.append(handler.body[-1])
        return bool(endings) and all(isinstance(e, ast.Return) for e in endings)

    return False


def _unreachable_blocks(path: Path):
    """Every (function, line, count) where a body continues past an exit."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        # A file that will not parse is a different test's problem.
        return

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        for index, statement in enumerate(body[:-1]):
            if _always_exits(statement):
                yield node.name, statement.lineno, len(body) - index - 1


def test_no_function_body_continues_past_an_unconditional_exit():
    """Unreachable statements mean code the author believed would run."""
    findings = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        for name, lineno, count in _unreachable_blocks(path):
            findings.append(
                f"{path.relative_to(SRC_ROOT.parent)}:{lineno} in {name}(): "
                f"{count} unreachable statement(s)"
            )

    assert not findings, (
        "Statements that can never run. Most often the tail of a method "
        "stranded by an insertion above it:\n  " + "\n  ".join(findings)
    )


def test_the_detector_sees_a_return_wall():
    """The check has to fail on the shape it exists for."""
    tree = ast.parse(
        "def f(self):\n"
        "    return 1\n"
        "    self.attribute = None\n"
    )
    found = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)][0]
    assert _always_exits(found.body[0])


def test_the_detector_sees_a_try_that_always_returns():
    """The shape that actually shipped: a guarded lookup reading as reachable."""
    tree = ast.parse(
        "def f(self):\n"
        "    try:\n"
        "        return self.page.wanted()\n"
        "    except Exception:\n"
        "        return True\n"
        "    self.attribute = None\n"
    )
    found = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)][0]
    assert _always_exits(found.body[0])


def test_the_detector_allows_an_early_return():
    """A return inside a branch is normal control flow, not a wall."""
    tree = ast.parse(
        "def f(self, flag):\n"
        "    if flag:\n"
        "        return 1\n"
        "    return 2\n"
    )
    found = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)][0]
    assert not _always_exits(found.body[0])


def test_the_detector_allows_a_try_that_can_fall_through():
    """A try whose handler does not return leaves the tail reachable."""
    tree = ast.parse(
        "def f(self):\n"
        "    try:\n"
        "        return self.value\n"
        "    except Exception:\n"
        "        pass\n"
        "    return None\n"
    )
    found = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)][0]
    assert not _always_exits(found.body[0])
