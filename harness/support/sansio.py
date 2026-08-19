"""The structural sans-io check.

`parser.py` and `protocol.py` import nothing from `socket`, `select`,
`asyncio`, `ssl`, or `subprocess`, and the constraint is transitive: whatever
those two import must satisfy it too, or the separation is cosmetic. The check
follows the import graph rather than stopping at the two named files.

This is a precondition rather than a scored case, and it is the opposite of the
probe. A probe mismatch means redis-py changed and is nobody's fault, so the
run aborts. A sans-io violation is the implementation's fault, so it scores
zero across all channels: a client that reaches I/O from its parser has not
built the thing the task asks for, and its other results do not mean what they
appear to mean.

The AST is inspected rather than the runtime, so an implementation cannot
satisfy this by deferring an import until after the check has run.
"""

from __future__ import annotations

import ast
import pathlib

__all__ = ["BANNED", "ENTRY_MODULES", "check_sansio"]

BANNED = frozenset({"socket", "select", "asyncio", "ssl", "subprocess"})
ENTRY_MODULES = ("parser", "protocol")

# Reaching outside the standard library, or reaching back into it dynamically,
# is a separate prohibition that this check also covers because the two are
# evaded by the same trick.
DYNAMIC = frozenset({"__import__", "exec", "eval", "compile"})


def _module_imports(path: pathlib.Path) -> tuple[set[str], set[str], list[str]]:
    """Absolute imports, sibling module names, and dynamic-import findings."""
    tree = ast.parse(path.read_text(), filename=str(path))
    absolute: set[str] = set()
    relative: set[str] = set()
    findings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            absolute |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                absolute.add(node.module.split(".")[0])
            elif node.level:
                if node.module:
                    relative.add(node.module.split(".")[0])
                relative |= {alias.name for alias in node.names}
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in DYNAMIC:
                findings.append(
                    f"{path.name}:{node.lineno}: dynamic import via {func.id!r}"
                )
    return absolute, relative, findings


def check_sansio(package_dir: str | pathlib.Path) -> list[str]:
    """Return a list of violations. Empty means the property holds."""
    root = pathlib.Path(package_dir)
    violations: list[str] = []
    for entry in ENTRY_MODULES:
        start = root / f"{entry}.py"
        if not start.exists():
            violations.append(f"{entry}.py is missing from the package")
            continue
        seen: set[str] = set()
        queue = [entry]
        while queue:
            name = queue.pop()
            if name in seen:
                continue
            seen.add(name)
            path = root / f"{name}.py"
            if not path.exists():
                continue
            try:
                absolute, relative, findings = _module_imports(path)
            except SyntaxError as exc:
                violations.append(f"{name}.py does not parse: {exc}")
                continue
            for banned in sorted(absolute & BANNED):
                where = "" if name == entry else f" (reached from {entry}.py)"
                violations.append(
                    f"{name}.py imports {banned!r}{where}; the sans-io "
                    f"constraint is transitive"
                )
            violations.extend(findings)
            queue.extend(sorted(relative))
    return violations
