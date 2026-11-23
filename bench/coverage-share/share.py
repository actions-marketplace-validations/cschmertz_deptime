"""Collect and score DT-QA-28: how often a real Python dependency update is to a package DepTime covers, by the rules
in README.md here. Nothing here runs DepTime's checks or any project's code: it reads GitHub's search results only.

    uv run python bench/coverage-share/share.py collect <work dir>
    uv run python bench/coverage-share/share.py score [--as-covered a,b,...] > bench/coverage-share/results-<date>.md

`collect` is resumable: each window's search results are kept in <work dir>/windows/, and a window already read is
skipped. When every window is read, it writes prs.jsonl and windows.json here. `score` reads prs.jsonl only.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "holdout"))

from find_pinned import gh  # noqa: E402  (waits out rate limits, retries server errors)

from deptime.config import load_coverage  # noqa: E402

FIRST_DAY, LAST_DAY = date(2026, 8, 24), date(2026, 11, 21)  # creation dates, UTC: 90 days
WINDOW_MINUTES = 20
SEED = "DT-QA-28"
ECOSYSTEMS = (
    "pip",
    "uv",
)  # Dependabot's branch prefixes for Python manifests: dependabot/pip/..., dependabot/uv/...
QUERY = "is:pr is:public author:app/dependabot head:dependabot/{eco} created:{start}..{end}"
PER_PAGE, MAX_PAGES = 100, 10  # GitHub search returns at most 1,000 results per query
PAUSE = 2.2  # seconds between search calls: the search quota is 30 a minute
OWN_REPO = "cschmertz/deptime"

_PREFIX = r"^(?:\[[^\]]+\]\s*)?(?:[^:]{1,40}:\s*)?"  # `[Security] `, then any short prefix ending in `:`
BUMP = re.compile(_PREFIX + r"[Bb]ump (\S+) from (\S+) to (\S+?)(?: in .+?)?\s*$")
REQUIREMENT = re.compile(_PREFIX + r"[Uu]pdate (\S+) requirement from (\S+) to (\S+?)(?: in .+?)?\s*$")
GROUP = re.compile(_PREFIX + r"[Bb]ump the \S+ group\b")
MULTI = re.compile(_PREFIX + r"[Bb]ump \S+(?:, \S+)* and \S+")

# Distributions a project's code doesn't call: building, packaging, linting, type checking, coverage, docs, notebook
# servers and data-only packages. Fixed with the protocol, before any pull request was collected.
TOOLING = {
    # build, packaging, environments, releases
    "pip", "setuptools", "wheel", "build", "twine", "hatchling", "hatch", "hatch-vcs", "flit", "flit-core",
    "poetry", "poetry-core", "poetry-plugin-export", "pdm", "pdm-backend", "uv", "setuptools-scm", "pip-tools",
    "pipenv", "virtualenv", "tox", "nox", "pre-commit", "cython", "maturin", "scikit-build-core", "meson-python",
    "bump-my-version", "bumpversion", "bump2version", "commitizen",
    # linters, formatters, type checkers, scanners
    "ruff", "black", "isort", "flake8", "pylint", "mypy", "pyright", "basedpyright", "ty", "bandit", "codespell",
    "autopep8", "yapf", "pyupgrade", "pydocstyle", "pycodestyle", "pyflakes", "pydoclint", "interrogate", "vulture",
    "safety", "pip-audit", "djlint", "sqlfluff", "yamllint", "detect-secrets", "deptry",
    # coverage
    "coverage", "pytest-cov",
    # docs
    "sphinx", "mkdocs", "mkdocs-material", "mkdocstrings", "furo", "pydata-sphinx-theme", "myst-parser",
    # notebook servers
    "jupyter", "jupyterlab", "notebook", "ipykernel",
    # data only
    "certifi", "tzdata",
}  # fmt: skip
TOOLING_PREFIXES = ("types-", "flake8-", "sphinx-", "sphinxcontrib-", "mkdocs-", "mkdocstrings-", "pylint-")
TOOLING_SUFFIXES = ("-stubs",)


def tooling(name: str) -> bool:
    return name in TOOLING or name.startswith(TOOLING_PREFIXES) or name.endswith(TOOLING_SUFFIXES)


# ------------------------------------------------------------------------------------------------ collection


def windows() -> list[tuple[str, datetime, datetime]]:
    """One 20-minute window per day, at a slot drawn from a generator seeded with SEED, in day order."""
    rng, out, day = random.Random(SEED), [], FIRST_DAY
    while day <= LAST_DAY:
        slot = rng.randrange(24 * 60 // WINDOW_MINUTES)
        start = datetime(day.year, day.month, day.day) + timedelta(minutes=slot * WINDOW_MINUTES)
        out.append((day.isoformat(), start, start + timedelta(minutes=WINDOW_MINUTES, seconds=-1)))
        day += timedelta(days=1)
    return out


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def read_window(eco: str, start: datetime, end: datetime) -> dict:
    query = QUERY.format(eco=eco, start=_stamp(start), end=_stamp(end))
    items, total, incomplete = [], 0, False
    for page in range(1, MAX_PAGES + 1):
        data = json.loads(
            gh(
                "api", "-X", "GET", "search/issues", "-f", f"q={query}", "-f", "sort=created", "-f", "order=asc",
                "-f", f"per_page={PER_PAGE}", "-f", f"page={page}",
            )
        )  # fmt: skip
        time.sleep(PAUSE)
        total, incomplete = data["total_count"], incomplete or bool(data["incomplete_results"])
        items += [
            {
                "repo": item["repository_url"].split("/repos/", 1)[1],
                "number": item["number"],
                "created_at": item["created_at"],
                "title": item["title"],
            }
            for item in data["items"]
        ]
        if len(data["items"]) < PER_PAGE or len(items) >= total:
            break
    return {"query": query, "total_count": total, "incomplete_results": incomplete, "items": items}


def _version(raw: str) -> Version | None:
    try:
        return Version(raw)
    except InvalidVersion:
        return None


def _top(spec: str) -> Version | None:
    """The highest version a requirement spec names (`>=2,<4` -> 4), else None."""
    found = [v for t in re.findall(r"\d+(?:\.\d+)*", spec) if (v := _version(t)) is not None]
    return max(found) if found else None


def is_major(old: Version | None, new: Version | None) -> bool | None:
    """The first number changes; for 0.x, the second (DT-QA-22's rule). A downgrade isn't major; None if unreadable."""
    if old is None or new is None:
        return None
    if new <= old:
        return False
    a, b = (*old.release, 0), (*new.release, 0)
    return b[0] != a[0] or (a[0] == 0 and b[1] != a[1])


def is_nonpatch(old: Version | None, new: Version | None) -> bool | None:
    """The first or second number changes: a major or a minor update. Some packages remove names in minor releases
    by policy (scipy, torch), so the ranking counts these too. A downgrade isn't; None if unreadable."""
    if old is None or new is None:
        return None
    if new <= old:
        return False
    a, b = (*old.release, 0), (*new.release, 0)
    return b[:2] != a[:2]


def parse(title: str) -> dict:
    """What a Dependabot title says: the kind, and for one package its name, versions and whether it's major."""
    if m := BUMP.match(title):
        old, new = _version(m.group(2)), _version(m.group(3))
        kind = "bump"
    elif m := REQUIREMENT.match(title):
        old, new = _top(m.group(2)), _top(m.group(3))
        kind = "requirement"
    else:
        kind = "group" if GROUP.match(title) else "multi" if MULTI.match(title) else "other"
        return {"kind": kind}
    package = canonicalize_name(re.sub(r"\[.*\]$", "", m.group(1)))
    return {
        "kind": kind,
        "package": package,
        "from": str(old) if old else m.group(2),
        "to": str(new) if new else m.group(3),
        "major": is_major(old, new),
        "nonpatch": is_nonpatch(old, new),
    }


def collect(work: Path) -> None:
    folder = work / "windows"
    folder.mkdir(parents=True, exist_ok=True)
    plan = windows()
    for n, (day, start, end) in enumerate(plan, 1):
        for eco in ECOSYSTEMS:
            path = folder / f"{day}-{eco}.json"
            if path.exists():
                continue
            result = read_window(eco, start, end)
            path.write_text(json.dumps(result), encoding="utf-8")
            print(
                f"[{n}/{len(plan)}] {day} {eco}: {len(result['items'])} of {result['total_count']}",
                file=sys.stderr,
            )
    rows, seen, summary = [], set(), []
    for day, start, end in plan:
        for eco in ECOSYSTEMS:
            result = json.loads((folder / f"{day}-{eco}.json").read_text(encoding="utf-8"))
            summary.append(
                {
                    "day": day,
                    "ecosystem": eco,
                    "start": _stamp(start),
                    "end": _stamp(end),
                    "total_count": result["total_count"],
                    "read": len(result["items"]),
                    "incomplete_results": result["incomplete_results"],
                }
            )
            for item in result["items"]:
                key = (item["repo"], item["number"])
                if item["repo"].lower() == OWN_REPO or key in seen:
                    continue
                seen.add(key)
                rows.append({**item, "ecosystem": eco, **parse(item["title"])})
    rows.sort(key=lambda r: (r["created_at"], r["repo"], r["number"]))
    with (HERE / "prs.jsonl").open("w", encoding="utf-8") as out:
        for row in rows:
            out.write(json.dumps(row) + "\n")
    (HERE / "windows.json").write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    print(f"{len(rows)} pull requests from {len(summary)} windows", file=sys.stderr)


# ------------------------------------------------------------------------------------------------ scoring


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - h) / d, (c + h) / d)


def _share(k: int, n: int) -> str:
    if n == 0:
        return "— (0)"
    lo, hi = wilson(k, n)
    return f"{100 * k / n:.1f}% ({k}/{n}; 95% CI {100 * lo:.1f}-{100 * hi:.1f}%)"


def covered_by(extra: set[str]):
    cov = load_coverage()

    def covered(row: dict) -> bool:
        name = row["package"]
        if name in extra or cov.get(name) is not None:
            return True
        spec = cov.installed_under(name)
        versions = [v for v in (row["from"], row["to"]) if _version(v) is not None]
        return bool(
            spec is not None
            and versions
            and any(
                canonicalize_name(req.name) == name
                and all(req.specifier.contains(v, prereleases=True) for v in versions)
                for req in spec.installed_as
            )
        )

    return covered


def score(extra: set[str]) -> str:
    rows = [
        json.loads(line) for line in (HERE / "prs.jsonl").read_text(encoding="utf-8").splitlines() if line
    ]
    summary = json.loads((HERE / "windows.json").read_text(encoding="utf-8"))
    covered = covered_by(extra)
    updates = [r for r in rows if r["kind"] in ("bump", "requirement")]
    for r in updates:
        r["covered"], r["tooling"] = covered(r), tooling(r["package"])

    def pairs(selected: list[dict]) -> dict[tuple[str, str], dict]:
        out: dict[tuple[str, str], dict] = {}
        for r in selected:  # oldest first, so the newest pull request of a pair is kept
            out[(r["repo"].lower(), r["package"])] = r
        return out

    majors = [r for r in updates if r["major"]]
    library_majors = [r for r in majors if not r["tooling"]]
    library_nonpatch = [r for r in updates if r["nonpatch"] and not r["tooling"]]
    a, b, c, d = pairs(updates), pairs(majors), pairs(library_majors), pairs(library_nonpatch)

    def line(label: str, chosen: dict) -> str:
        return f"| {label} | {_share(sum(r['covered'] for r in chosen.values()), len(chosen))} |"

    kinds = Counter(r["kind"] for r in rows)
    out = [
        "## Headline",
        "",
        "| Measure | Covered |",
        "|---|---|",
        line("**C: major updates of libraries, distinct (repository, package) pairs (the decision)**", c),
        line("A: all single-package updates, distinct pairs", a),
        line("B: major updates, distinct pairs (tooling included)", b),
        line("C, pull requests rather than pairs", {i: r for i, r in enumerate(library_majors)}),
        line("D: major or minor updates of libraries, distinct pairs", d),
    ]
    if extra:
        out += ["", f"Counted as covered for this run: {', '.join(sorted(extra))}."]
    out += ["", "## By ecosystem (C)", "", "| Ecosystem | Covered |", "|---|---|"]
    for eco in ECOSYSTEMS:
        out.append(line(eco, pairs([r for r in library_majors if r["ecosystem"] == eco])))
    totals = Counter()
    for w in summary:
        totals[w["ecosystem"]] += w["total_count"]
    capped = sum(1 for w in summary if w["total_count"] > PER_PAGE * MAX_PAGES)
    out += [
        "",
        "## The sample",
        "",
        f"- Windows read: {len(summary)} ({len(summary) // len(ECOSYSTEMS)} days x {len(ECOSYSTEMS)} ecosystems); "
        f"pull requests matched: {sum(totals.values())} ({', '.join(f'{e} {totals[e]}' for e in ECOSYSTEMS)}); read: "
        f"{sum(w['read'] for w in summary)}; windows past the 1,000-result cap: {capped}; with incomplete results: "
        f"{sum(w['incomplete_results'] for w in summary)}.",
        f"- Pull requests kept (DepTime's own and repeats left out): {len(rows)}. By title: "
        + ", ".join(f"{k} {kinds[k]}" for k in ("bump", "requirement", "group", "multi", "other")),
        f"- Single-package updates: {len(updates)}; major: {len(majors)}; major and not tooling: "
        f"{len(library_majors)}; major or minor and not tooling: {len(library_nonpatch)}; versions unreadable: "
        f"{sum(r['major'] is None for r in updates)}.",
        f"- Distinct repositories: {len({r['repo'].lower() for r in rows})}; in C: {len({k[0] for k in c})}.",
    ]
    major_repos: dict[str, set[str]] = defaultdict(set)
    nonpatch_repos: dict[str, set[str]] = defaultdict(set)
    any_repos: dict[str, set[str]] = defaultdict(set)
    for r in updates:
        if r["tooling"]:
            continue
        repo = r["repo"].lower()
        any_repos[r["package"]].add(repo)
        if r["nonpatch"]:
            nonpatch_repos[r["package"]].add(repo)
        if r["major"]:
            major_repos[r["package"]].add(repo)
    is_covered = {r["package"]: r["covered"] for r in updates}

    def ranking(want_covered: bool, limit: int) -> list[str]:
        names = [n for n in any_repos if is_covered[n] == want_covered and nonpatch_repos[n]]
        names.sort(key=lambda n: (-len(nonpatch_repos[n]), -len(major_repos[n]), -len(any_repos[n]), n))
        return [
            f"| {i} | {n} | {len(nonpatch_repos[n])} | {len(major_repos[n])} | {len(any_repos[n])} |"
            for i, n in enumerate(names[:limit], 1)
        ]

    header = [
        "| # | Package | Repositories with a major or minor update | With a major update | With any update |",
        "|---|---|---|---|---|",
    ]
    out += [
        "",
        "## Packages not covered, ranked (the shortlist reads this)",
        "",
        *header,
        *ranking(False, 40),
    ]
    out += ["", "## Covered packages, ranked the same way, for context", "", *header, *ranking(True, 15)]
    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect").add_argument("work", type=Path)
    scoring = sub.add_parser("score")
    scoring.add_argument("--as-covered", default="", help="comma-separated names to count as covered")
    args = parser.parse_args()
    if args.command == "collect":
        collect(args.work)
    else:
        extra = {canonicalize_name(n) for n in args.as_covered.split(",") if n.strip()}
        sys.stdout.write(score(extra))


if __name__ == "__main__":
    main()
