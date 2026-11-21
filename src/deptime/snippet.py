"""The names a code snippet uses from one package, for `check_symbols(code=...)` (DT-CORE-07).

Read with `ast`, never run. What counts: the package's modules and names it imports, the attribute chains it reads
off them (`np.linalg.norm`), the keyword arguments of each call to one (`httpx.Client(timeout=5)`), and the attributes
it reads off an object it builds by calling one (`client = httpx.Client()` then `client.get(...)`), which only count
once the index shows the call builds a class (`query.py`).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

MAX_CODE_CHARS = 100_000


@dataclass
class Uses:
    paths: dict[str, list[str]] = field(default_factory=dict)  # dotted path -> keyword arguments passed to it
    # (path the object was built by calling, attribute read off the object) -> keyword arguments, if a call
    members: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    # How many arguments the calls to a path (or member) pass by position, the most any call does (DT-CORE-34); None
    # once a call spreads `*args` or `**kwargs`, which may cover any parameter. Only paths that are called.
    positional: dict[str | tuple[str, str], int | None] = field(default_factory=dict)
    # Where each path (or member) is used, and where each keyword is passed to it: line numbers, in order (DT-CORE-58).
    lines: dict[str | tuple[str, str], list[int]] = field(default_factory=dict)
    kwarg_lines: dict[tuple[str | tuple[str, str], str], list[int]] = field(default_factory=dict)
    call_lines: dict[str | tuple[str, str], list[int]] = field(default_factory=dict)  # where it is called

    def _at(self, key: str | tuple[str, str], kwargs: list[str] | None, line: int | None) -> None:
        if line is None:
            return
        seen = self.lines.setdefault(key, [])
        if line not in seen:
            seen.append(line)
        for k in kwargs or ():
            at = self.kwarg_lines.setdefault((key, k), [])
            if line not in at:
                at.append(line)

    def add(self, path: str, kwargs: list[str] | None = None, line: int | None = None) -> None:
        known = self.paths.setdefault(path, [])
        known.extend(k for k in kwargs or () if k not in known)
        self._at(path, kwargs, line)

    def add_member(
        self, built_by: str, name: str, kwargs: list[str] | None = None, line: int | None = None
    ) -> None:
        known = self.members.setdefault((built_by, name), [])
        known.extend(k for k in kwargs or () if k not in known)
        self._at((built_by, name), kwargs, line)

    def called(self, key: str | tuple[str, str], call: ast.Call) -> None:
        spread = any(isinstance(a, ast.Starred) for a in call.args) or any(
            k.arg is None for k in call.keywords
        )
        at = self.call_lines.setdefault(key, [])
        if call.lineno not in at:
            at.append(call.lineno)
        if key in self.positional and self.positional[key] is None:
            return
        self.positional[key] = None if spread else max(self.positional.get(key) or 0, len(call.args))


def _within(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == r or path.startswith(r + ".") for r in roots)


def _toward(path: str, roots: tuple[str, ...]) -> bool:
    """Whether `path` is the package's or a namespace on the way to it: `import google.genai` binds `google`, which
    leads to google-genai, but `google.auth` is another distribution's (DT-CORE-85)."""
    return _within(path, roots) or any(r.startswith(path + ".") for r in roots)


class _Reader(ast.NodeVisitor):
    def __init__(self, roots: tuple[str, ...]) -> None:
        self.roots = roots
        self.bound: dict[str, str] = {}  # local name -> the package path it names
        self.built: dict[str, str] = {}  # local name -> the package path called to build it
        self.uses = Uses()

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            if not _within(a.name, self.roots):
                continue
            self.uses.add(a.name, line=node.lineno)
            if a.asname:
                self.bound[a.asname] = a.name
            else:
                top = a.name.split(".")[0]
                self.bound[top] = top

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level or not node.module:
            return
        if _within(node.module, self.roots):
            # The module itself is a use too: `from langchain.llms import OpenAI` fails when `langchain.llms` is gone,
            # and that is the line to fix (DT-CORE-69).
            self.uses.add(node.module, line=node.lineno)
        for a in node.names:
            if a.name == "*":
                continue
            path = f"{node.module}.{a.name}"
            # `from google import genai` imports the package `google.genai` from a namespace it doesn't own.
            if not _within(path, self.roots):
                continue
            self.uses.add(path, line=node.lineno)
            self.bound[a.asname or a.name] = path

    def _path(self, node: ast.expr) -> str | None:
        """The path an expression names: a bound name, or an attribute chain off one, while it stays toward the
        package (`google`, `google.genai.Client`; not `google.auth`)."""
        if isinstance(node, ast.Name):
            return self.bound.get(node.id)
        if isinstance(node, ast.Attribute):
            base = self._path(node.value)
            path = f"{base}.{node.attr}" if base else None
            return path if path and _toward(path, self.roots) else None
        return None

    def _own(self, node: ast.expr) -> str | None:
        """The package path an expression names; None for a namespace it shares (`google` alone)."""
        path = self._path(node)
        return path if path and _within(path, self.roots) else None

    def _built_by(self, node: ast.expr) -> str | None:
        """The package path called to build the object an expression names: `client` or `httpx.Client()`."""
        if isinstance(node, ast.Name):
            return self.built.get(node.id)
        if isinstance(node, ast.Call):
            return self._own(node.func)
        return None

    @staticmethod
    def _kwargs(call: ast.Call) -> list[str]:
        return [k.arg for k in call.keywords if k.arg is not None]

    def visit_Assign(self, node: ast.Assign) -> None:
        self.generic_visit(node)
        built = self._own(node.value.func) if isinstance(node.value, ast.Call) else None
        for target in node.targets:
            if isinstance(target, ast.Name):
                self.bound.pop(target.id, None)
                self.built.pop(target.id, None)
                if built:
                    self.built[target.id] = built
                elif (path := self._path(node.value)) is not None:
                    self.bound[target.id] = path  # an alias: `Client = httpx.Client`

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            if (
                isinstance(item.optional_vars, ast.Name)
                and isinstance(item.context_expr, ast.Call)
                and (built := self._own(item.context_expr.func)) is not None
            ):
                self.built[item.optional_vars.id] = built
        self.generic_visit(node)

    visit_AsyncWith = visit_With

    def visit_Call(self, node: ast.Call) -> None:
        path = self._own(node.func)
        if path is not None:
            self.uses.add(path, self._kwargs(node), node.lineno)
            self.uses.called(path, node)
        elif isinstance(node.func, ast.Attribute) and (built := self._built_by(node.func.value)):
            self.uses.add_member(built, node.func.attr, self._kwargs(node), node.lineno)
            self.uses.called((built, node.func.attr), node)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load):
            path = self._own(node)
            if path is not None:
                self.uses.add(path, line=node.lineno)
                return  # its parts are part of the path
            if built := self._built_by(node.value):
                self.uses.add_member(built, node.attr, line=node.lineno)
        self.generic_visit(node)


def uses_in(code: str, roots: tuple[str, ...], limit: int | None = MAX_CODE_CHARS) -> Uses | None:
    """What `code` uses from the package whose import names are `roots`; None when it doesn't parse, or nests deeper
    than Python lets the reader follow (a few hundred terms of `'a' + 'a' + ...`, DT-CORE-80). `limit` caps the
    characters read (None: all of it, for a project's own files). A leading byte-order mark is no code (DT-CORE-81)."""
    code = code.removeprefix("\ufeff")
    try:
        tree = ast.parse(code if limit is None else code[:limit])
        reader = _Reader(roots)
        reader.visit(tree)
    except (SyntaxError, ValueError, RecursionError):
        return None
    return reader.uses
