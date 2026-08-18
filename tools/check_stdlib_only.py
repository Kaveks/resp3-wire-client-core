#!/usr/bin/env python3
"""Fail if the client package reaches outside the standard library."""
import ast
import pathlib
import sys

STDLIB = set(sys.stdlib_module_names)
BANNED_CALLS = {"__import__", "exec", "eval", "compile"}
DEFAULT_ROOTS = ["reference/resp3_wire", "starter/resp3_wire"]


def check(root: pathlib.Path) -> list[str]:
    problems: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    names = [node.module.split(".")[0]]
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in BANNED_CALLS:
                    problems.append(
                        f"{path}:{node.lineno}: dynamic import call {func.id!r}"
                    )
                continue
            for name in names:
                if name and name not in STDLIB:
                    problems.append(
                        f"{path}:{node.lineno}: non-stdlib import {name!r}"
                    )
    return problems


def main() -> int:
    roots = [pathlib.Path(p) for p in (sys.argv[1:] or DEFAULT_ROOTS)]
    found = [p for r in roots if r.exists() for p in check(r)]
    for p in found:
        print(p, file=sys.stderr)
    print(f"check_stdlib_only: {'FAIL' if found else 'PASS'}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
