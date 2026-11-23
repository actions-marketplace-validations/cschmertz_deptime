# How often a real update is to a covered package (DT-QA-28)

**Question:** when a Python project gets a dependency update, how often is the package one of the 53 DepTime covers?

Every earlier measurement starts from the covered packages: DT-QA-22's update pull requests were searched for by
their names. So nothing yet says how often the upgrade check, the Action or `check_upgrade` meets an update it can
check, rather than "Not covered by DepTime's index, so not checked". This decides whether coverage should grow
(DT-DATA-74 stays not planned either way: hundreds of packages would make the daily download too large).

This page and `share.py` were committed before any pull request was collected. The sample is collected and scored
once, and that first score is reported, whichever way it comes out. Nothing here runs DepTime's checks or any
project's code.

**What was seen first.** Before this page was written, GitHub's search was asked how many pull requests the query
below matches for 2026-11-10 (pip 6,005, uv 3,761, every ecosystem 98,673) and for one hour of that day (pip 266),
and the branch names of 16 of them were checked to start with the ecosystem's prefix (16 of 16). No title or package
name was read.

## The sample

Dependabot's pull requests in public repositories, from its two Python ecosystems, read from GitHub's issue search
on 2026-11-23:

```
is:pr is:public author:app/dependabot head:dependabot/<eco> created:<start>..<end>   sort: created, oldest first
```

- **Ecosystems:** `pip` (requirements files, Pipfile, Poetry, `pyproject.toml`, `setup.py`) and `uv`. Dependabot
  names its branches `dependabot/<ecosystem>/...`, so the branch prefix says which manifest the update is in.
- **Windows:** one 20-minute window per day for the 90 days from 2026-08-24 to 2026-11-21 (UTC), its start drawn
  from the day's 72 slots by Python's `random.Random("DT-QA-28")`, in day order (`share.py`'s `windows()`). Both
  ecosystems are read in the same window, so each keeps its share of the traffic.
- **Every result in a window is read**, up to GitHub's 1,000 per query; a window past that is reported.
- **Left out:** DepTime's own repository, and a pull request already read.

**What a title says** (`share.py`'s `parse()`), after an optional `[Security]` and a prefix of up to 40 characters
ending in `:` (`chore(deps):`):

- `bump`: `Bump <name> from <a> to <b>[ in ...]`, one package (`in the pip group` included);
- `requirement`: `Update <name> requirement from <spec> to <spec>[ in ...]`, one package, read as the highest version
  each spec names (`>=2,<4` is 4);
- `group` (`Bump the <x> group ...`), `multi` (`Bump a and b`, `Bump a, b and c`) and `other`: counted, not scored.

Names are compared as PEP 503 normalises them.

**Covered** means `check-upgrade` would look the package up in the index: its name is in `coverage.toml` at this
commit, or it is one of an entry's `installed_as` names with both versions in that entry's range (`pydantic-ai` as
`pydantic-ai-slim`; `pinecone-client` below 6). Whether both releases are indexed isn't asked: this measures the
list of packages, not the build.

**Major:** the first number of the version changes; for 0.x, the second (DT-QA-22's rule). A downgrade isn't. All 48
breaking uses in DT-QA-22's 264 update pull requests were in major updates, so the decision reads majors.

**Major or minor:** the first or second number changes. Some packages remove names in minor releases by policy
(scipy and torch among them), so the ranking for the shortlist counts these.

**Tooling** isn't called by a project's code, so it is left out of the library measures: the list in `share.py`'s
`TOOLING` (build and packaging, linters, formatters and type checkers, coverage, docs, notebook servers, `certifi` and
`tzdata`), names starting `types-`, `flake8-`, `sphinx-`, `sphinxcontrib-`, `mkdocs-`, `mkdocstrings-` or `pylint-`,
and names ending `-stubs`. The list was fixed with this page.

**The unit** is a distinct (repository, package) pair, so a project that gets a package's update every week counts
once for it. The newest pull request of a pair is kept.

## Scoring (fixed now)

```bash
uv run python bench/coverage-share/share.py collect <work dir>     # writes prs.jsonl and windows.json here
uv run python bench/coverage-share/share.py score > bench/coverage-share/results-2026-11-23.md
```

Reported, each with a 95% Wilson interval:

- **C, the headline and the decision:** the covered share of distinct pairs with a major update of a package that
  isn't tooling;
- A: the covered share of every single-package update's pair; B: of major updates' pairs, tooling included; C counted
  per pull request rather than per pair; D: of major or minor updates' pairs, tooling left out; C per ecosystem;
- the sample: windows, matches, pull requests read and kept, title kinds;
- the 40 uncovered packages that aren't tooling, ranked by the number of repositories with a major or minor update of
  them, then with a major update, then with any update, then by name; and the 15 covered packages ranked the same way.

## The decision (fixed now)

- **C at 60% or more:** coverage stays at 53. The ranking is recorded for later; work stays on accuracy and hints.
- **C under 60%:** a shortlist. Walk the ranking in order and keep a package when both hold, stopping at 15 kept or
  after the 40th:
  1. **It breaks code:** its changelog, release notes or a migration guide documents a removal or rename of public
     API (a module, class, function, method or parameter) in a release since 2023-01-01, the index policy's `since`.
     The link is recorded.
  2. **It can be read without importing it** (Invariant 1): its public API is Python source, or ships `.pyi` stubs
     in its own wheel, as numpy does. Left out, as in v1: generated per-service clients (boto3), and packages whose
     API is mostly compiled code without stubs or whose builds would dominate the index (torch, tensorflow).

  Each candidate read is recorded with its verdict and reason. The results then give C again with the kept packages
  counted as covered (`score --as-covered a,b,...`), so the shortlist's gain is a number on the same sample. Adding
  any of them is separate work, one backlog line each, with its build time and download size measured first.

## Known limits

- Dependabot only: Renovate's branch names don't say the ecosystem, and projects without a bot aren't seen.
- Public repositories only, and a repository that renamed Dependabot's branch separator isn't matched.
- An update isn't proof the project's code calls the package. Leaving tooling out is the proxy; security updates can
  also bump a package the project only installs through another one.
- Group updates aren't scored; their count is reported.
- One 20-minute window a day: the hour Dependabot runs varies by repository, and a window samples one slice of it.
