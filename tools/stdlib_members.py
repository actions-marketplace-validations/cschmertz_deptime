"""Build `src/deptime/stdlib_members.txt`: the members of standard-library classes whose presence changes between
Python 3.9 and 3.15 (DT-CORE-92), read statically from typeshed's stubs. Nothing from an indexed package is imported.

  python tools/stdlib_members.py <typeshed e8ba06f>/stdlib <typeshed e2609bd>/stdlib [--check PYTHON ...] \
      > src/deptime/stdlib_members.txt

check_symbols asks a standard-library base class directly whether it has a name (DT-CORE-32), and the answer came from
whichever Python runs DepTime: django's `SimpleTestCase.assertStartsWith` was missing on 3.12 and present on 3.14. A
member listed here is `unknown` instead, with the versions that have it; every other member is the same on every
Python this table covers, so the running Python's answer stands.

The classes are those in the modules check_symbols reads (`SAFE_STDLIB`), under each public path a library can
subclass them by (`unittest.TestCase` and `unittest.case.TestCase`). A class's members are its own (its body, read with
the stubs' `sys.version_info` checks; platform checks count both ways), its bases' and its metaclass's (`type`'s when
none is named), the way attribute lookup on a class finds them. 3.9 comes from the older commit only where its own
checks change between 3.9 and 3.10, as in tools/stdlib_names.py.

A class that one of the Pythons DepTime runs on (3.12 and later) lacks gets a `*` line with the versions that have it,
so that no Python answers for its members while another can't import it; a class added by 3.12 doesn't, since a
library that subclasses it can't run on an older Python anyway (`enum.StrEnum`, 3.11).

Dunder names are left out: check_symbols answers those from fixed lists before it asks a base (DT-CORE-38).

`--check` takes CPython interpreters (the standard library only), and a version one of them runs is read from it: a
member is there when that interpreter's class has it by the test check_symbols uses, among the class's `dir()` and the
names the stubs list, which is the answer check_symbols gives on that Python. The stubs differ from it both ways:
typeshed drops deprecated names CPython still has (`UserDict.get`), and types `memoryview` as a `Sequence`, whose
`count` the class only has from 3.14. Where no interpreter's class has a name the stubs show coming or going (an
instance attribute, `ArgumentParser.color` from 3.14), the stubs' versions stand.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from stdlib_names import MINORS, OLD_MINOR, Stubs, _absolute, _ranges, _type_check_only, _version_test

from deptime import stdlib
from deptime.query import SAFE_STDLIB

HEADER = """\
# Members of standard-library classes whose presence changes between Python 3.9 and 3.15 (DT-CORE-92), from
# typeshed's stubs, read statically by tools/stdlib_members.py, and from CPython: 3.10-3.15 from typeshed commit
# e2609bd14f7352cb5aa713a8b5c0515295945fd1 (2026-10-27, the one stdlib_versions.txt comes from), and 3.9 from commit
# e8ba06f710d5fe4f9219fafd77abd79a6e3414f4 (2025-08-24), the last before typeshed dropped 3.9; a change between 3.9
# and 3.10 counts only where that commit's own version checks show it.
# typeshed is licensed under the Apache License 2.0: https://github.com/python/typeshed/blob/main/LICENSE
#
# `class member ranges`: `class` is a public path for the class, `ranges` the minor versions of 3.x whose class has
# `member` (its own, its bases' or its metaclass's), `9-10` or `11-` (still there); a range starting at 9 may start
# earlier, and only versions that have the class count. Member `*` is the class itself, listed when a Python from 3.12
# on (the ones DepTime runs on) lacks it. A version one of these interpreters runs is read from its classes, unless
# none of them has the member: CPython {checked}.
"""
PROBE = """
import importlib, json, sys, warnings
out = {}
for path, names in json.load(sys.stdin).items():
    module, _, attr = path.rpartition(".")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            obj = getattr(importlib.import_module(module), attr)
    except Exception:
        continue
    if isinstance(obj, type):
        names = set(names) | set(dir(obj)) | set(dir(type(obj)))
        out[path] = sorted(n for n in names if any(n in vars(k) for k in obj.__mro__) or hasattr(obj, n))
print(json.dumps([sys.version_info[1], sys.version_info[2], out]))
"""
RUNS_ON = range(12, 16)  # the Pythons DepTime runs on (requires-python), up to the newest the table covers
BUILTINS = frozenset(n for n in dir(builtins) if isinstance(getattr(builtins, n), type))


class Classes:
    """Classes in one typeshed commit's stubs, and their members on each Python version."""

    def __init__(self, stubs: Stubs):
        self.stubs = stubs
        self.trees: dict[str, ast.Module] = {}
        self.memo: dict[tuple[str, int], tuple[frozenset[str], str | None] | None] = {}

    def _tree(self, module: str) -> ast.Module | None:
        if module not in self.stubs.files:
            return None
        if module not in self.trees:
            self.trees[module] = ast.parse(self.stubs.files[module].read_text(encoding="utf-8"))
        return self.trees[module]

    def _active(self, body: list[ast.stmt], minor: int):
        """The statements of `body` that hold on Python 3.<minor>, through `if` blocks."""
        for st in body:
            if isinstance(st, ast.If):
                holds = _version_test(st.test, minor)
                if holds is not False:
                    yield from self._active(st.body, minor)
                if holds is not True:
                    yield from self._active(st.orelse, minor)
            else:
                yield st

    def _bound(self, module: str, minor: int) -> dict[str, str]:
        """What each module-level name refers to, private ones included: local classes, `from m import n`."""
        tree = self._tree(module)
        if tree is None:
            return {}
        is_package = self.stubs.files[module].name == "__init__.pyi"
        out: dict[str, str] = {}
        for st in self._active(tree.body, minor):
            if isinstance(st, ast.ClassDef):
                out[st.name] = f"{module}.{st.name}"
            elif isinstance(st, ast.ImportFrom):
                source = _absolute(module, is_package, st.level, st.module)
                for a in st.names:
                    if a.name != "*":
                        out[a.asname or a.name] = f"{source}.{a.name}"
            elif isinstance(st, ast.Import):
                for a in st.names:
                    out[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
            elif (
                isinstance(st, ast.Assign)
                and len(st.targets) == 1
                and isinstance(st.targets[0], ast.Name)
                and isinstance(st.value, (ast.Name, ast.Attribute))
            ):
                out.setdefault(st.targets[0].id, f"={ast.unparse(st.value)}")  # `EnumType = EnumMeta`
        return out

    def resolve(self, module: str, expr: ast.expr, minor: int, depth: int = 0) -> str | None:
        """The class a base or metaclass expression names, as `module.Class` where the stubs define it."""
        if isinstance(expr, ast.Subscript):
            expr = expr.value
        text = ast.unparse(expr)
        if depth > 8 or not all(p.isidentifier() for p in text.split(".")):
            return None
        head, _, rest = text.partition(".")
        bound = self._bound(module, minor)
        if head in bound:
            target = bound[head]
            if target.startswith("="):
                return self.resolve(module, ast.parse(target[1:], mode="eval").body, minor, depth + 1)
            path = f"{target}.{rest}" if rest else target
        elif head in BUILTINS and not rest:
            path = f"builtins.{head}"
        else:
            return None
        return self._definition(path, minor, depth + 1)

    def _definition(self, path: str, minor: int, depth: int) -> str | None:
        """Follow re-exports until `path` names a class statement; None when it names anything else."""
        parts = path.split(".")
        for n in range(len(parts) - 1, 0, -1):
            module, names = ".".join(parts[:n]), parts[n:]
            if module in self.stubs.files:
                break
        else:
            return None
        if len(names) != 1:
            return None
        name = names[0]
        tree = self._tree(module)
        assert tree is not None
        for st in self._active(tree.body, minor):
            # A `@type_check_only` class counts here: `public()` already leaves out one named directly, and a public
            # alias to one is a real class (3.13's `asyncio.AbstractEventLoopPolicy`).
            if isinstance(st, ast.ClassDef) and st.name == name:
                return f"{module}.{name}"
        target = self._bound(module, minor).get(name) or self.stubs.public(module, minor).get(
            name
        )  # `import *`
        if target is None or target == f"{module}.{name}":
            return None
        if target.startswith("="):
            return self.resolve(module, ast.parse(target[1:], mode="eval").body, minor, depth)
        return self._definition(target, minor, depth + 1) if depth < 8 else None

    def _classdef(self, origin: str, minor: int) -> ast.ClassDef | None:
        module, _, name = origin.rpartition(".")
        tree = self._tree(module)
        found = None
        for st in self._active(tree.body if tree else [], minor):
            if isinstance(st, ast.ClassDef) and st.name == name:
                found = st
        return found

    def members(self, origin: str, minor: int) -> tuple[frozenset[str], str | None] | None:
        """A class's members on Python 3.<minor> and its metaclass; None when the stubs don't define it then."""
        key = (origin, minor)
        if key in self.memo:
            return self.memo[key]
        self.memo[key] = (frozenset(), None)  # a cycle reads as nothing
        node = self._classdef(origin, minor)
        if node is None:
            self.memo[key] = None
            return None
        module = origin.rpartition(".")[0]
        own: set[str] = set()
        for st in self._active(node.body, minor):
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if not _type_check_only(st):
                    own.add(st.name)
            elif isinstance(st, (ast.Assign, ast.AnnAssign)):
                for t in st.targets if isinstance(st, ast.Assign) else [st.target]:
                    if isinstance(t, ast.Name):
                        own.add(t.id)
        meta = None
        for kw in node.keywords:
            if kw.arg == "metaclass":
                meta = self.resolve(module, kw.value, minor)
        bases = [b for b in (self.resolve(module, e, minor) for e in node.bases) if b]
        if not bases and origin != "builtins.object":
            bases = ["builtins.object"]
        for base in bases:
            got = self.members(base, minor)
            if got:
                own |= got[0]
                meta = meta or got[1]
        if meta is None and origin not in ("builtins.object", "builtins.type"):
            meta = "builtins.type"
        if meta and meta != origin:
            got = self.members(meta, minor)
            if got:
                own |= got[0]
        result = (frozenset(own), meta)
        self.memo[key] = result
        return result


def _paths(stubs: Stubs, classes: Classes, minor: int) -> dict[str, str]:
    """Each public path of a class in the modules check_symbols reads, with the class it names."""
    out = {}
    for module in sorted(stubs.files):
        parts = module.split(".")
        if parts[0] not in SAFE_STDLIB or any(p.startswith("_") for p in parts):
            continue
        entry = stdlib.lookup(module)
        if module != "builtins" and (entry is None or not entry.has(minor)):
            continue
        for name in stubs.public(module, minor):
            origin = classes._definition(f"{module}.{name}", minor, 0)
            if origin:
                out[f"{module}.{name}"] = origin
    return out


def _runtime(
    pythons: list[str], wanted: dict[str, list[str]]
) -> tuple[list[str], dict[int, dict[str, list[str]]]]:
    names, seen = [], {}
    for python in pythons:
        done = subprocess.run(
            [python, "-I", "-c", PROBE], input=json.dumps(wanted), capture_output=True, text=True, check=True
        )
        minor, micro, found = json.loads(done.stdout.strip().splitlines()[-1])
        names.append(f"3.{minor}.{micro}")
        seen[minor] = found
    return names, seen


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("old", type=Path, help="typeshed's stdlib/ at the commit that still models 3.9")
    ap.add_argument("new", type=Path, help="typeshed's stdlib/ at the commit stdlib_versions.txt comes from")
    ap.add_argument(
        "--check", nargs="*", default=[], help="CPython interpreters whose classes decide their version"
    )
    args = ap.parse_args()
    old_stubs, new_stubs = Stubs(args.old), Stubs(args.new)
    old, new = Classes(old_stubs), Classes(new_stubs)
    # Each version's members of each public path, then 3.9's from the older commit's own 3.9-to-3.10 change.
    views: dict[str, dict[int, frozenset[str]]] = {}
    for minor in MINORS:
        for path, origin in _paths(new_stubs, new, minor).items():
            got = new.members(origin, minor)
            if got:
                views.setdefault(path, {})[minor] = got[0]
    old9, old10 = _paths(old_stubs, old, OLD_MINOR), _paths(old_stubs, old, 10)
    for path, origin in old9.items():
        nine = old.members(origin, OLD_MINOR)
        if nine is None:
            continue
        if path in old10 and 10 in views.get(path, {}):
            ten = old.members(old10[path], 10)
            added, gone = (ten[0] - nine[0], nine[0] - ten[0]) if ten else (frozenset(), frozenset())
            views[path][OLD_MINOR] = (views[path][10] - added) | gone
        elif path not in old10:
            views.setdefault(path, {})[OLD_MINOR] = nine[0]
    # What each interpreter's class has (its `dir()` and the names the stubs list), by the test check_symbols uses.
    stubbed = {p: sorted(frozenset.union(*v.values())) for p, v in views.items()}
    checked, seen = _runtime(args.check, stubbed)
    lines = []
    for path in sorted(views):
        span = sorted(views[path])
        found = {m: set(f.get(path, ())) for m, f in seen.items() if m in span and path in f}
        for name in sorted(set(stubbed[path]).union(*found.values())):
            if len(name) > 4 and name.startswith("__") and name.endswith("__"):
                continue  # check_symbols answers dunder names from fixed lists (DT-CORE-38)
            stub = {m for m in span if name in views[path][m]}
            run = {m for m in span if name in (found[m] if m in found else views[path][m])}
            # The interpreters' answer when they differ; when none has it on the class, the stubs' (an instance
            # attribute, `ArgumentParser.color` from 3.14); when every one has it, it's there.
            have = run if run and run != set(span) else stub if not run else set(span)
            if have and have != set(span):
                lines.append(f"{path} {name} {_ranges(sorted(have))}")
        # A class that a Python DepTime runs on lacks: `*`, so no version answers for its members while another
        # can't (3.13's `asyncio.AbstractChildWatcher`, gone in 3.14). Platform-only classes read from the stubs.
        run = {m for m in (OLD_MINOR, *MINORS) if (path in seen[m] if m in seen else m in views[path])}
        have = run or set(span)
        if not set(RUNS_ON) <= have:
            lines.append(f"{path} * {_ranges(sorted(have))}")
    sys.stdout.write(HEADER.format(checked=", ".join(checked) or "none") + "\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
