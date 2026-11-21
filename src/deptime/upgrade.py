"""`deptime check-upgrade`: what a dependency update breaks in a project's code (DT-CORE-58, DT-CORE-59).

Every Python file in the project is read with DepTime's own reader (`snippet.py`; never the benchmark's independent
`bench/holdout/uses.py`), never imported or run. For each covered package whose version changes, its uses are checked
with `check_symbols` at the new version, and flagged as the holdout scorer counts a flag (`bench/holdout/score.py`): a
name missing, a keyword not accepted, a required argument not passed, more arguments by position than it takes, or a
call to a callable that only raises. A flag the old version also shows isn't caused by the update, so it is listed
apart (`failing_before`), as are uses DepTime can't tell about (`unknown`), and neither fails the check. No model is
asked anything (ADR 0006): the answer is the index's.
"""

from __future__ import annotations

import os
import re
import subprocess
import warnings
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from deptime import snippet
from deptime.models import (
    CheckSymbolsResult,
    DependencyChange,
    Finding,
    PackageUpgrade,
    SymbolCheck,
    SymbolQuery,
    UpgradeBreak,
    UpgradeCheckResult,
    UpgradeNotice,
    UseLocation,
)
from deptime.untrusted import untrusted_text

MAX_FILE_BYTES = 2_000_000  # a larger .py file is generated data, not code someone maintains
MAX_UNREADABLE_LISTED = 20
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".tox",
    ".nox",
    ".venv",
    "venv",
    "env",
    ".env",
    "node_modules",
    "site-packages",
    "__pycache__",
    "build",
    "dist",
    ".eggs",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}

Key = str | tuple[str, str]


# ------------------------------------------------------------------------------------------------- reading the code
def project_files(root: Path) -> list[str]:
    """The project's Python files, relative to `root`: what git tracks or would track (so `.gitignore` applies), else
    a walk that leaves out virtual environments, build output and caches."""
    try:
        out = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
                "--",
                "*.py",
            ],
            capture_output=True,
            check=True,
        )
        files = [f for f in out.stdout.decode("utf-8", "replace").split("\0") if f]
        return sorted(f for f in files if (root / f).is_file() and not SKIP_DIRS & set(Path(f).parts[:-1]))
    except (OSError, subprocess.CalledProcessError):
        pass
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in filenames:
            if name.endswith(".py"):
                found.append((Path(dirpath) / name).relative_to(root).as_posix())
    return sorted(found)


def read_code(root: Path, files: Iterable[str]) -> dict[str, str]:
    """Each file's text, without a byte-order mark (DT-CORE-81). A file over MAX_FILE_BYTES, or one that can't be
    opened, is left out: pass the files that aren't in the result to `check_upgrade(unread=)` to count them."""
    code = {}
    for rel in files:
        path = root / rel
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            code[rel] = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
    return code


def uses_in(text: str, roots: tuple[str, ...]) -> snippet.Uses | None:
    """A project file's uses, read whole; the warnings Python prints about other people's code (an invalid escape
    sequence) stay quiet."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return snippet.uses_in(text, roots, limit=None)


def own_modules(files: Iterable[str]) -> set[str]:
    """Top-level module names the project defines itself (`x.py`, `x/__init__.py`, `src/x/...`): an import of one is
    the project's own code, even when a covered package has the same name."""
    out = set()
    for rel in files:
        parts = rel.split("/")
        if parts[0] == "src" and len(parts) > 1:
            parts = parts[1:]
        if len(parts) == 1:
            out.add(parts[0].removesuffix(".py"))
        elif parts[-1] == "__init__.py" and len(parts) == 2:
            out.add(parts[0])
    return out


@dataclass
class _Read:
    """One package's uses across the project, with where each happens."""

    uses: snippet.Uses = field(default_factory=snippet.Uses)
    at: dict[Key, list[UseLocation]] = field(default_factory=dict)
    kwarg_at: dict[tuple[Key, str], list[UseLocation]] = field(default_factory=dict)
    call_at: dict[Key, list[UseLocation]] = field(default_factory=dict)
    files: set[str] = field(default_factory=set)

    def merge(self, rel: str, found: snippet.Uses) -> None:
        total = self.uses
        for path, kwargs in found.paths.items():
            total.add(path, kwargs)
        for (built_by, name), kwargs in found.members.items():
            total.add_member(built_by, name, kwargs)
        for key, count in found.positional.items():
            # A call that spreads `*args` or `**kwargs` may cover any parameter, in this file or an earlier one.
            if count is None or (key in total.positional and total.positional[key] is None):
                total.positional[key] = None
            else:
                total.positional[key] = max(total.positional.get(key) or 0, count or 0)
        for src, dst in ((found.lines, self.at), (found.call_lines, self.call_at)):
            for key, lines in src.items():
                dst.setdefault(key, []).extend(UseLocation(file=rel, line=n) for n in lines)
        for key, lines in found.kwarg_lines.items():
            self.kwarg_at.setdefault(key, []).extend(UseLocation(file=rel, line=n) for n in lines)
        if found.paths or found.members:
            self.files.add(rel)


def _keys(path: str, uses: snippet.Uses) -> list[Key]:
    """The reader's keys a checked path came from: the path itself, and members read off an object a call to a class
    built (`client.get` is `httpx.Client.get`)."""
    owner, _, attr = path.rpartition(".")
    keys: list[Key] = [path]
    if (owner, attr) in uses.members:
        keys.append((owner, attr))
    return keys


def _locations(table: dict, keys: list) -> list[UseLocation]:
    seen: dict[tuple[str, int], UseLocation] = {}
    for key in keys:
        for loc in table.get(key, []):
            seen.setdefault((loc.file, loc.line), loc)
    return sorted(seen.values(), key=lambda loc: (loc.file, loc.line))


# ----------------------------------------------------------------------------------------------------- the check
def _flags(r: SymbolCheck, called: bool) -> list[tuple[str, str | None]]:
    """What check_symbols' answer flags, by `bench/holdout/score.py`'s rule: (kind, keyword)."""
    if r.status != "present" and r.status != "missing":
        return []
    if r.status == "missing":
        return [("missing", None)]
    out: list[tuple[str, str | None]] = [("rejected_kwarg", k) for k in r.rejected_kwargs]
    if r.missing_args:
        out.append(("missing_args", None))
    if called and r.raises:
        out.append(("raises", None))
    if called and r.max_positional is not None:
        out.append(("too_many_positional", None))
    return out


def _explain(kind: str, kwarg: str | None, r: SymbolCheck, at: str) -> str:
    if kind == "missing":
        return f"`{r.path}` is missing at {at}" + (f" ({r.hint})" if r.hint else "") + "."
    if kind == "rejected_kwarg":
        return f"`{r.path}` doesn't take the keyword `{kwarg}` at {at}."
    if kind == "missing_args":
        args = ", ".join(f"`{a}`" for a in r.missing_args)
        return f"`{r.path}` requires {args} at {at}, which the calls don't pass."
    if kind == "raises":
        return f"`{r.path}` does nothing but raise at {at}: {untrusted_text(r.raises)}"
    return f"`{r.path}` {r.hint}." if r.hint else f"`{r.path}` takes fewer arguments by position at {at}."


def _version(raw: str) -> Version | None:
    try:
        return Version(raw)
    except InvalidVersion:
        return None


def _alias(dt, name: str, change: DependencyChange):
    """The covered package whose code `name` installs with the same version numbers (`installed_as`: pydantic-ai is
    pydantic-ai-slim's code, DT-CORE-82), when both versions are in the range it names; else None."""
    spec = dt.coverage.installed_under(name)
    if spec is None:
        return None
    for req in spec.installed_as:
        if canonicalize_name(req.name) == name and all(
            req.specifier.contains(v, prereleases=True) for v in (change.from_version, change.to_version)
        ):
            return spec
    return None


def _not_indexed(dt, name: str, change: DependencyChange) -> str | None:
    """Why the index can't compare this update (DT-CORE-79), or None when it can. Answering for the nearest indexed
    release is how a release the policy leaves out is read, but when that is the old version's own release, the
    check would compare a release with itself and report "No breaks found" for an update nobody checked."""
    rows = dt.store.releases(name)
    to_row, to_exact = dt.store.resolve_version(name, change.to_version)
    if to_row is None:
        return (
            f"{name} {change.to_version} is older than the earliest indexed release, {rows[0]['version']}, so the"
            " update wasn't checked."
        )
    from_row, _ = dt.store.resolve_version(name, change.from_version)
    same = Version(change.from_version) == Version(change.to_version)
    if same or from_row is None or from_row["version"] != to_row["version"]:
        return None
    newest = rows[-1]["version"]
    if not to_exact and Version(change.to_version) > Version(newest):
        return (
            f"{name} {change.to_version} isn't in DepTime's index yet: the newest release there is {newest}, and the"
            " index is rebuilt twice a day, so run the check again later."
        )
    gone = change.from_version if to_exact else change.to_version
    return (
        f"{name} {gone} isn't indexed, and {change.from_version} and {change.to_version} are both answered for"
        f" {to_row['version']}, the nearest indexed release, so there was nothing to compare."
    )


def _partial(name: str, to: str, newest: str) -> str:
    return (
        f"So the check of {name} went as far as {newest}: a break {to} brings isn't seen until the index has it (it is"
        " rebuilt twice a day)."
    )


def check_upgrade(
    dt,  # deptime.query.DepTime
    project: str,
    code: dict[str, str],
    changes: list[DependencyChange],
    base: str | None = None,
    unread: Iterable[str] = (),
    pins_unread: Iterable[str] = (),
) -> UpgradeCheckResult:
    """Check each change against the project's code (`code`: relative path -> source). `unread`: the project's Python
    files `read_code` left out (too large, or not opened), counted in `files_unreadable` (DT-CORE-81). `pins_unread`:
    the pin files that couldn't be read, with the reason (DT-CORE-80)."""
    result = UpgradeCheckResult(project=project, base=base, pins_unread=list(pins_unread))
    unread = set(unread) - set(code)
    own = own_modules([*code, *unread])
    unreadable: set[str] = set()
    suppressed = 0
    changed = {canonicalize_name(c.package) for c in changes}
    for change in changes:
        name = canonicalize_name(change.package)
        if change.from_version is None or change.to_version is None:
            result.skipped.append(change)
            continue
        bad = next((v for v in (change.from_version, change.to_version) if _version(v) is None), None)
        if bad is not None:
            note = f"`{bad}` isn't a version, so there is no update to check."
            result.skipped.append(change.model_copy(update={"note": note}))
            continue
        spec, alias = dt.coverage.get(name), None
        if spec is None and (spec := _alias(dt, name, change)) is not None:
            if spec.name in changed:
                continue  # the same code, changing with it: checked under its own name
            alias = f"{name} {change.to_version} is read as {spec.name} {change.to_version}: {name} installs the same code."
        if spec is None or not dt._indexed(spec.name):
            result.not_covered.append(change)
            continue
        if (why := _not_indexed(dt, spec.name, change)) is not None:
            result.not_indexed.append(change.model_copy(update={"note": why}))
            continue
        roots = tuple(r for r in spec.import_names if r.split(".")[0] not in own)
        if not roots:
            result.checked.append(
                PackageUpgrade(
                    change=change,
                    resolved_version=None,
                    exact=False,
                    uses=0,
                    files=0,
                    note=f"The project defines its own `{spec.import_names[0].split('.')[0]}` module, so its imports"
                    f" of that name are its own code and nothing of {spec.name} was read.",
                )
            )
            continue
        read = _Read()
        for rel, text in code.items():
            found = uses_in(text, roots)
            if found is None:
                unreadable.add(rel)
                continue
            read.merge(rel, found)
        upgrade, hidden = _check_package(dt, spec, change, read, alias)
        suppressed += hidden
        result.checked.append(upgrade)
    result.breaks = sum(len(p.breaks) for p in result.checked)
    result.files_read = len(code) - len(unreadable)
    result.files_unreadable = sorted(unreadable | unread)[:MAX_UNREADABLE_LISTED]
    result.suppressed = suppressed
    notes = []
    if unreadable:
        one = len(unreadable) == 1
        notes.append(
            f"{len(unreadable)} Python file{'' if one else 's'} didn't parse, or {'nests' if one else 'nest'} too"
            f" deeply to read, so nothing was read from {'it' if one else 'them'}."
        )
    if unread:
        notes.append(
            f"{len(unread)} Python file{' was' if len(unread) == 1 else 's were'}n't read: over"
            f" {MAX_FILE_BYTES:,} bytes, or couldn't be opened."
        )
    if suppressed:
        notes.append(
            f"{suppressed} break{'' if suppressed == 1 else 's'} or notice{'' if suppressed == 1 else 's'} left out by"
            " this project's suppressions (deptime.toml or pyproject.toml)."
        )
    result.note = " ".join(notes) or None
    return result


def _results(dt, name: str, spec, version: str, queries: list[SymbolQuery]) -> CheckSymbolsResult:
    return dt._check_symbols(name, spec, version, queries)


def _check_package(
    dt, spec, change: DependencyChange, read: _Read, alias: str | None = None
) -> tuple[PackageUpgrade, int]:
    name, to, before = spec.name, change.to_version, change.from_version
    queries, count = dt._code_queries(name, to, [], read.uses)
    at_to = _results(dt, name, spec, to, queries)
    newest = dt.store.releases(name)[-1]["version"]
    newer = to is not None and (v := _version(to)) is not None and v > Version(newest)
    notes = [alias, at_to.note]
    if newer:
        # DT-CORE-79: the breaks up to the newest indexed release are real; the rest of the way isn't checked yet.
        notes.append(_partial(name, to, newest))
    upgrade = PackageUpgrade(
        change=change,
        resolved_version=at_to.resolved_version,
        exact=at_to.exact,
        uses=count,
        files=len(read.files),
        note=" ".join(n for n in notes if n) or None,
        newer_than_index=newer,
    )
    if at_to.resolved_version is None or not queries:
        return upgrade, 0
    from_row = dt.store.resolve_version(name, before)[0] if before else None
    at_from = _results(dt, name, spec, before, queries) if from_row is not None else None
    if at_from is None and before:
        extra = f"{before} is older than the earliest indexed release, so uses that fail there too aren't set apart."
        upgrade.note = " ".join(n for n in (upgrade.note, extra) if n)
    row, exact = dt.store.resolve_version(name, to)
    hidden = 0
    called_keys = set(read.call_at)
    for i, (q, r) in enumerate(zip(queries, at_to.results, strict=True)):
        keys = _keys(q.path, read.uses)
        called = any(k in called_keys for k in keys)
        where = _locations(read.at, keys)
        if r.status in ("unknown", "not_indexed"):
            upgrade.notices.append(
                UpgradeNotice(path=r.path, status="unknown", detail=r.hint or r.status, locations=where)
            )
            continue
        before_r = at_from.results[i] if at_from is not None else None
        before_flags = (
            set(_flags(before_r, called))
            if before_r is not None and before_r.status in ("present", "missing")
            else set()
        )
        flags = _flags(r, called)
        for kind, kwarg in flags:
            if kind == "rejected_kwarg":
                locs = _locations(read.kwarg_at, [(k, kwarg) for k in keys])
            elif kind in ("missing_args", "too_many_positional", "raises"):
                locs = _locations(read.call_at, keys) or where
            else:
                locs = where
            explanation = _explain(kind, kwarg, r, at_to.resolved_version)
            if (kind, kwarg) in before_flags:
                upgrade.notices.append(
                    UpgradeNotice(
                        path=r.path,
                        status="failing_before",
                        detail=f"Flagged at {before} too: {explanation}",
                        locations=locs,
                    )
                )
                continue
            finding = _finding(dt, spec, kind, kwarg, r, row, exact, to)
            issue = finding.issue if finding else kind
            symbol = finding.symbol if finding else r.path
            if any(s.matches(pkg, symbol, issue) for s in dt.suppressions for pkg in {name, change.package}):
                hidden += 1
                continue
            upgrade.breaks.append(
                UpgradeBreak(
                    path=r.path,
                    kind=kind,
                    kwarg=kwarg,
                    missing_args=r.missing_args if kind == "missing_args" else [],
                    explanation=explanation,
                    locations=locs,
                    finding=finding,
                    internal=r.internal,
                )
            )
        if not flags and r.deprecated:
            if any(
                s.matches(pkg, r.path, "deprecated")
                for s in dt.suppressions
                for pkg in {name, change.package}
            ):
                hidden += 1
                continue
            since, _ = dt._deprecation(name, r.path)
            upgrade.notices.append(
                UpgradeNotice(
                    path=r.path,
                    status="deprecated",
                    detail=r.deprecated,
                    new=not (before_r is not None and before_r.deprecated),
                    deprecated_in=since,
                    locations=where,
                )
            )
    return upgrade, hidden


def _finding(
    dt, spec, kind: str, kwarg: str | None, r: SymbolCheck, row, exact: bool, to: str
) -> Finding | None:
    """The change as explain_error describes it, with the release, the replacements and a proven fix."""
    name = spec.name
    if kind == "missing":
        f = dt._symbol_finding(spec, r.path, row, exact, to)
    elif kind in ("rejected_kwarg", "missing_args"):
        param = kwarg if kind == "rejected_kwarg" else r.missing_args[0]
        signal = "unexpected_kwarg" if kind == "rejected_kwarg" else "missing_argument"
        f = dt._param_finding(spec, dt._constructor(name, r.path), param, signal, row, exact, to)
    else:
        return None
    return dt._act_on(f, {name: to}) if f is not None else None


# ------------------------------------------------------------------------------------------------------ the report
MAX_LOCATIONS_SHOWN = 5


def _md(text: str | None) -> str:
    """Text for a Markdown report: library text is untrusted, so nothing in it renders as markup."""
    if not text:
        return ""
    out = []
    for c in text:
        out.append("\\" + c if c in "\\`*_[]<>|#!~" else c)
    return "".join(out)


def _code(text: str) -> str:
    return "`" + text.replace("`", "'") + "`"


def _where(locs: list[UseLocation]) -> str:
    shown = ", ".join(_code(f"{loc.file}:{loc.line}") for loc in locs[:MAX_LOCATIONS_SHOWN])
    more = len(locs) - MAX_LOCATIONS_SHOWN
    return shown + (f" and {more} more" if more > 0 else "")


def _break_line(b: UpgradeBreak, at: str) -> str:
    use = _code(f"{b.path}({b.kwarg}=)" if b.kind == "rejected_kwarg" else b.path)
    reason = {
        "missing": f"missing at {at}",
        "rejected_kwarg": f"the keyword isn't accepted at {at}",
        "missing_args": f"requires {', '.join(_code(a) for a in b.missing_args)} at {at}, which the calls don't pass",
    }.get(b.kind) or _md(b.explanation.replace("`", ""))
    parts = [f"- {use}: {reason}."]
    f = b.finding
    if f is not None:
        if f.first_version_without and f.last_version_with:
            parts.append(f"Last in {f.last_version_with}, gone in {f.first_version_without}.")
        elif f.first_version_with and f.last_version_without:
            parts.append(f"Added in {f.first_version_with}.")
        if f.fix is not None:
            parts.append(f"Fix: {_code(f.fix.old)} → {_code(f.fix.new)} ({f.fix.source}).")
        elif f.replacements:
            r = f.replacements[0]
            parts.append(f"Try {_code(r.target)} ({r.source}, confidence {r.confidence:.2f}).")
    elif b.kind == "missing" and "; try " in b.explanation:
        parts.append(f"Try {_code(b.explanation.split('; try ', 1)[1].rstrip(').'))}.")
    if b.locations:
        parts.append(f"At {_where(b.locations)}.")
    return " ".join(parts)


def _fold(breaks: list[UpgradeBreak]) -> list[tuple[UpgradeBreak, list[UpgradeBreak]]]:
    """Each break with the missing names under it that a missing module or class takes with it (DT-CORE-69): one
    change to fix, listed once, with every line it reaches."""
    gone = sorted((b.path for b in breaks if b.kind == "missing"), key=len)
    top: dict[str, str] = {}
    for path in gone:
        top[path] = next((g for g in gone if path.startswith(g + ".")), path)
    under: dict[str, list[UpgradeBreak]] = {}
    for b in breaks:
        if b.kind == "missing" and top[b.path] != b.path:
            under.setdefault(top[b.path], []).append(b)
    return [
        (b, under.get(b.path, []) if b.kind == "missing" else [])
        for b in breaks
        if b.kind != "missing" or top[b.path] == b.path
    ]


def _member_fix(b: UpgradeBreak) -> tuple[str, str] | None:
    """What replaces a name a missing module or class takes with it, when its own finding says (DT-CORE-76): the
    fold lists it under its parent, which may have no replacement (mcp 2's `mcp.server.fastmcp` module is gone,
    while its `FastMCP` class has a proven new home)."""
    f = b.finding
    if f is None:
        return None
    if f.fix is not None:
        return f.fix.new, "proven"
    if f.replacements:
        return f.replacements[0].target, "try"
    return None


_PLAIN = re.compile(r"[^\w.,=() /-]")
MAX_HANDOFF_LINES = 60


def _plain(text: str) -> str:
    """A path, keyword or version as plain characters: the hand-off block carries no text from library source."""
    return _PLAIN.sub("", text)[:200]


def _handoff(result: UpgradeCheckResult) -> list[str]:
    """The block an agent can act on (DT-GTM-07): each confirmed break with every line it reaches and the index's
    proven fix or best replacement, as plain names. The model writes the fix; DepTime has decided what is broken."""
    items: list[str] = []
    for p in result.checked:
        at = p.resolved_version or p.change.to_version or ""
        for b, under in _fold(p.breaks):
            locs = [*b.locations, *(loc for u in under for loc in u.locations)]
            use = f"{b.path}({b.kwarg}=)" if b.kind == "rejected_kwarg" else b.path
            what = {
                "missing": "gone",
                "rejected_kwarg": "keyword not accepted",
                "missing_args": "now requires " + ", ".join(b.missing_args),
                "too_many_positional": "takes fewer arguments by position",
                "raises": "only raises",
            }[b.kind]
            step = f"{_plain(use)}: {_plain(what)} in {p.change.package} {_plain(at)}"
            f = b.finding
            if f is not None and f.fix is not None:
                step += f"; the index proves {_plain(f.fix.old)} -> {_plain(f.fix.new)}"
            elif f is not None and f.replacements:
                step += f"; try {_plain(f.replacements[0].target)}"
            for u in under:
                if (member := _member_fix(u)) is not None:
                    new, how = member
                    verb = "the index proves" if how == "proven" else "try"
                    step += f"; {verb} {_plain(u.path)} -> {_plain(new)}"
            seen = dict.fromkeys(f"{_plain(loc.file)}:{loc.line}" for loc in locs)  # a line once, in order
            where = ", ".join(seen) or "(no line recorded)"
            items.append(f"- {where}: {step}")
    # DT-GTM-08: an `unknown` is a use DepTime can't rule out, not one it cleared (DT-QA-05 lost 4 sessions on it)
    unsure = [
        f"- {', '.join(f'{_plain(loc.file)}:{loc.line}' for loc in x.locations) or '(no line recorded)'}: "
        f"{_plain(x.path)} in {p.change.package} {_plain(p.resolved_version or p.change.to_version or '')}"
        for p in result.checked
        for x in p.notices
        if x.status == "unknown"
    ]
    if not items and not unsure:
        return []
    more = "- and {} more: run `deptime check-upgrade --format json` for all"
    shown = items[:MAX_HANDOFF_LINES]
    if len(items) > len(shown):
        shown.append(more.format(len(items) - len(shown)))
    room = max(MAX_HANDOFF_LINES - len(shown), 10)
    tail = unsure[:room] + ([more.format(len(unsure) - room)] if len(unsure) > room else [])
    if tail:
        tail.insert(
            0, "DepTime can't tell whether these uses still work at the new version: check each one yourself."
        )
    return [
        "<details><summary>For a coding agent</summary>",
        "",
        "```text",
        # DT-GTM-09 (from DT-QA-26): the block also shows when DepTime confirmed no break but can't tell about
        # some uses, and says what to do without a replacement (the agent left 3 breaks it found as "not clear")
        *(
            [
                "DepTime confirmed these uses break with this update (failing at the new version, not at the old).",
                "Fix these lines. Where no replacement is given, find how the new version does the same thing (its"
                " source, release notes or migration guide) and rewrite the code to it.",
                *shown,
            ]
            if shown
            else ["DepTime confirmed no break in this update."]
        ),
        *tail,
        "Deprecations still work, and uses that fail at the old version too aren't caused by this update.",
        *(["Then run deptime check-upgrade again: it should find no breaks."] if shown else []),
        "```",
        "",
        "</details>",
        "",
    ]


def _update(c: DependencyChange) -> str:
    return _md(f"{c.package} {c.from_version} → {c.to_version}")


def _headline(result: UpgradeCheckResult) -> list[str]:
    """The verdict, then each update that wasn't checked and why (DT-CORE-79, DT-CORE-80): "No breaks found" only
    ever names updates that were checked."""
    n = result.breaks
    checked = ", ".join(
        _update(p.change) + (f" (checked up to {_md(p.resolved_version)})" if p.newer_than_index else "")
        for p in result.checked
    )
    lines = []
    if n:
        lines += [f"**{n} break{'' if n == 1 else 's'} found** in this project's code: {checked}.", ""]
    elif result.checked:
        lines += [f"**No breaks found** in this project's code: {checked}.", ""]
    for c in result.not_indexed:
        lines += [f"**Not checked:** {_update(c)}. {_md(c.note)}", ""]
    if result.pins_unread:
        files = "; ".join(
            f"{_code(where)} ({_md(why)})"
            for where, _, why in (x.partition(": ") for x in result.pins_unread)
        )
        lines += [
            f"**Not read:** {files}. The updates in {'it' if len(result.pins_unread) == 1 else 'them'}"
            " weren't checked.",
            "",
        ]
    if not lines and any(c.note for c in result.skipped):
        lines += ["**Nothing was checked.**", ""]  # a version that isn't one: the line below says which
    elif not lines:
        lines += ["No covered package's version changed, so nothing was checked.", ""]
    return lines


def to_markdown(result: UpgradeCheckResult) -> str:
    lines = ["## DepTime upgrade check", "", *_headline(result)]
    for p in result.checked:
        c = p.change
        head = f"### {c.package} {c.from_version} → {c.to_version}"
        lines += [head, ""]
        facts = [
            f"{p.uses} name{'' if p.uses == 1 else 's'} used in {p.files} file{'' if p.files == 1 else 's'}"
        ]
        if c.source:
            facts.append(f"read from {_code(c.source)}")
        if p.newer_than_index:
            facts.append(f"checked up to {p.resolved_version}, the newest indexed release")
        elif p.resolved_version and not p.exact:
            facts.append(f"answered for {p.resolved_version}, the nearest indexed release")
        lines += ["; ".join(facts) + ".", ""]
        if p.breaks:
            at = p.resolved_version or c.to_version or ""
            lines.append("**Breaks**")
            for b, under in _fold(p.breaks):
                lines.append(_break_line(b, at))
                if under:
                    shown = "; ".join(
                        _code(u.path)
                        + (
                            f" (use {_code(m[0])})"
                            if (m := _member_fix(u)) and m[1] == "proven"
                            else f" (try {_code(m[0])})"
                            if m
                            else ""
                        )
                        + (f" at {_where(u.locations)}" if u.locations else "")
                        for u in under
                    )
                    lines.append(f"  - Missing with it: {shown}.")
            lines.append("")
        groups = [
            ("deprecated", "Deprecated at the new version", lambda x: x.new, True),
            ("deprecated", "Already deprecated at the old version", lambda x: not x.new, False),
            (
                "failing_before",
                "Flagged at the old version too, so not caused by this update",
                lambda x: True,
                False,
            ),
            (
                "unknown",
                "DepTime can't tell, so check these yourself (never fails the check)",
                lambda x: True,
                False,
            ),
        ]
        for status, title, keep, shown in groups:
            items = [x for x in p.notices if x.status == status and keep(x)]
            if not items:
                continue
            lines.append(f"**{title}**" if shown else f"<details><summary>{title}: {len(items)}</summary>\n")
            for x in items:
                since = f" (since {x.deprecated_in})" if x.deprecated_in else ""
                at = f" At {_where(x.locations)}." if x.locations else ""
                detail = _md(x.detail).rstrip()
                stop = "" if not detail or detail[-1] in ".!?" else "."
                lines.append(f"- {_code(x.path)}{since}: {detail}{stop}{at}")
            lines += [""] if shown else ["", "</details>", ""]
        if p.note:
            lines += [_md(p.note), ""]
    if result.not_covered:
        names = ", ".join(f"{c.package} {c.from_version} → {c.to_version}" for c in result.not_covered)
        lines += [f"Not covered by DepTime's index, so not checked: {_md(names)}.", ""]
    if result.skipped:
        names = ", ".join(
            f"{_md(c.package)} {_md(c.to_version)} (added)"
            if c.from_version is None
            else f"{_md(c.package)} {_md(c.from_version)} (removed)"
            if c.to_version is None
            else f"{_update(c)} ({_md(c.note)})"
            for c in result.skipped
        )
        lines += [f"No update to check: {names}.", ""]
    if result.note:
        lines += [_md(result.note), ""]
    lines += _handoff(result)
    lines.append(
        "DepTime reads the code and the index; it runs nothing and asks no model. A break is a use the index shows"
        " failing at the new version and not at the old one."
    )
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------------------- GitHub annotations
def _escape(text: str, prop: bool = False) -> str:
    """A workflow command's data (or property value, `prop`): a newline in library text can't start a command."""
    out = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return out.replace(":", "%3A").replace(",", "%2C") if prop else out


def _annotation(level: str, loc: UseLocation | None, prefix: str, title: str, message: str) -> str:
    """A workflow command; with no `loc`, it is about the whole update, not a line."""
    if loc is None:
        return f"::{level} title={_escape(title, True)}::{_escape(message)}"
    file = f"{prefix}/{loc.file}" if prefix else loc.file
    return f"::{level} file={_escape(file, True)},line={loc.line},title={_escape(title, True)}::{_escape(message)}"


def to_annotations(result: UpgradeCheckResult, prefix: str = "") -> str:
    """GitHub Actions workflow commands (DT-GTM-06): an error on each line a break reaches, a warning on each line a
    newly deprecated name is used. `prefix` is the project's directory relative to the repository's root."""
    lines = [
        _annotation(
            "warning",
            None,
            prefix,
            f"DepTime: {c.package} {c.from_version} → {c.to_version} wasn't checked",
            c.note or "",
        )
        for c in result.not_indexed
    ]
    for p in result.checked:
        c = p.change
        at = p.resolved_version or c.to_version or ""
        if p.newer_than_index:  # DT-CORE-79
            title = f"DepTime: {c.package} {c.from_version} → {c.to_version} was checked up to {p.resolved_version}"
            message = f"{c.to_version} isn't in DepTime's index yet. " + _partial(
                c.package, c.to_version or "", p.resolved_version or ""
            )
            lines.append(_annotation("warning", None, prefix, title, message))
        for b in p.breaks:
            title = f"DepTime: {c.package} {c.from_version} → {c.to_version} breaks {b.path}"
            message = _break_line(b, at).removeprefix("- ").split(" At `", 1)[0].replace("`", "")
            lines += [_annotation("error", loc, prefix, title, message) for loc in b.locations]
        for x in p.notices:
            if x.status == "deprecated" and x.new:
                title = f"DepTime: {x.path} is deprecated in {c.package} {c.to_version}"
                lines += [
                    _annotation("warning", loc, prefix, title, untrusted_text(x.detail) or "")
                    for loc in x.locations
                ]
    return "\n".join(lines) + ("\n" if lines else "")
