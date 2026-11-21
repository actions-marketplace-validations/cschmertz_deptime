"""Index publishing + download for local mode, fully offline via httpx.MockTransport."""

import gzip
import hashlib
import json
import lzma
import subprocess
import threading
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from deptime.index import distribution as dist
from deptime.index.store import IndexStore
from deptime.query import DepTime
from tests.conftest import HTTPX, RELEASED_AT, SNAPSHOTS
from tests.test_tracebacks import HTTPX_TB


def _published(tmp_path):
    src = tmp_path / "built.sqlite"
    store = IndexStore.open(src, readonly=False)
    store.rebuild_package(HTTPX, SNAPSHOTS, RELEASED_AT)
    store.close()
    out = tmp_path / "dist"
    return out, dist.pack(src, out)


def _client(dist_dir, *, corrupt=False, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        name = str(request.url).rsplit("/", 1)[-1]
        path = dist_dir / name
        if not path.exists():
            return httpx.Response(404)
        data = path.read_bytes()
        if corrupt and name in (dist.INDEX_ASSET, dist.XZ_ASSET):
            data = data[:-16] + b"\0" * 16
        return httpx.Response(200, content=data)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _local(settings, tmp_path):
    return replace(
        settings, index_path=tmp_path / "home" / "index.sqlite", index_url="https://idx.test", offline=False
    )


def test_pack_then_download_installs_a_working_index(tmp_path, settings, coverage):
    out, manifest = _published(tmp_path)
    assert (manifest.packages, manifest.releases, manifest.schema_version) == (1, 3, "2")
    assert json.loads((out / "manifest.json").read_text())["sha256"] == manifest.sha256
    cfg = _local(settings, tmp_path)
    with _client(out) as client:
        result = dist.ensure_index(cfg, client=client)
    assert result.changed and result.path == dist.active_index(cfg.index_path)
    dt = DepTime(IndexStore.open(result.path), coverage)
    assert dt.explain_error(HTTPX_TB, {"httpx": "0.28.1"}).findings[0].issue == "param_removed"
    assert dist.read_local_meta(cfg.index_path)["manifest"]["sha256"] == manifest.sha256


def test_checksum_mismatch_keeps_the_existing_index(tmp_path, settings):
    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    with _client(out) as client:
        dist.ensure_index(cfg, client=client)
    before = dist.active_index(cfg.index_path).read_bytes()
    with _client(out, corrupt=True) as client, pytest.raises(ValueError, match="checksum"):
        dist.download(cfg.index_url, cfg.index_path, client)
    assert dist.active_index(cfg.index_path).read_bytes() == before
    assert not list(cfg.index_path.parent.glob("*.download*"))


def test_incompatible_schema_is_refused(tmp_path, settings):
    out, _ = _published(tmp_path)
    data = json.loads((out / "manifest.json").read_text())
    data["schema_version"] = "999"
    (out / "manifest.json").write_text(json.dumps(data))
    with _client(out) as client:
        result = dist.ensure_index(_local(settings, tmp_path), client=client)
    assert result.path is None and "IndexIncompatible" in result.message


def test_locally_built_index_is_never_fetched_or_replaced(tmp_path, settings):
    cfg = _local(settings, tmp_path)
    store = IndexStore.open(cfg.index_path, readonly=False)
    store.rebuild_package(HTTPX, SNAPSHOTS[:1], RELEASED_AT)
    store.close()
    out, _ = _published(tmp_path)
    calls: list[str] = []
    with _client(out, calls=calls) as client:
        result = dist.ensure_index(cfg, client=client)
    assert result.changed is False and "locally built" in result.message and calls == []


def test_fresh_index_is_not_rechecked_and_stale_one_is(tmp_path, settings):
    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    with _client(out) as client:
        dist.ensure_index(cfg, client=client)
    calls: list[str] = []
    with _client(out, calls=calls) as client:
        assert dist.ensure_index(cfg, client=client).message == "index is fresh"
    assert calls == []
    later = dist._now() + timedelta(hours=25)
    with _client(out, calls=calls) as client:
        result = dist.ensure_index(cfg, client=client, now=later)
    assert result.message == "index is up to date" and calls == ["https://idx.test/manifest.json"]


def test_offline_mode_never_downloads(tmp_path, settings):
    assert dist.ensure_index(replace(_local(settings, tmp_path), offline=True)).path is None


def test_background_refresh_hot_swaps_the_store(tmp_path, settings, coverage):
    out, _ = _published(tmp_path)
    dt = DepTime(None, coverage)
    assert "no index yet" in dt.symbol_history("httpx", "Client").note
    with _client(out) as client:
        thread = dist.start_background_refresh(
            dt, _local(settings, tmp_path), client=client, log=lambda _m: None
        )
        thread.join(timeout=10)
    assert dt.symbol_history("httpx", "Client").ever_seen


def _flaky_client(dist_dir, failures: int, calls: list[str]):
    """Refuses the first `failures` requests, as a network that isn't up yet would."""
    served = _client(dist_dir)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) <= failures:
            raise httpx.ConnectError("network is unreachable", request=request)
        return served.send(request)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_failed_first_download_is_retried_until_it_lands(tmp_path, settings, coverage):
    """DT-OSS-10: a server started without network gets its index once the network is back, without a restart."""
    out, _ = _published(tmp_path)
    dt, calls, logged = DepTime(None, coverage), [], []
    with _flaky_client(out, 2, calls) as client:
        thread = dist.start_background_refresh(
            dt, _local(settings, tmp_path), client=client, log=logged.append, delays=(0, 0)
        )
        thread.join(timeout=10)
    assert not thread.is_alive() and dt.symbol_history("httpx", "Client").ever_seen
    assert sum("could not download" in m for m in logged) == 2 and dt.index_problem is None


def test_no_index_note_names_the_cause_while_retrying(tmp_path, settings, coverage):
    dt, stop = DepTime(None, coverage), threading.Event()
    with _flaky_client(tmp_path, 10**6, []) as client:
        thread = dist.start_background_refresh(
            dt, _local(settings, tmp_path), client=client, log=lambda _m: None, delays=(60,), stop=stop
        )
        for _ in range(100):
            if dt.index_problem:
                break
            time.sleep(0.05)
        note = dt.symbol_history("httpx", "Client").note
        stop.set()
        thread.join(timeout=10)
    assert "could not download the index (ConnectError: network is unreachable)" in note
    assert "retrying in the background in 1 min" in note
    assert "network is unreachable" in dt.explain_error(HTTPX_TB, {"httpx": "0.28.1"}).note
    assert not thread.is_alive()


def test_offline_mode_without_an_index_says_so_and_never_retries(tmp_path, settings, coverage):
    dt, calls = DepTime(None, coverage), []
    with _flaky_client(tmp_path, 0, calls) as client:
        thread = dist.start_background_refresh(
            dt, replace(_local(settings, tmp_path), offline=True), client=client, log=lambda _m: None
        )
        thread.join(timeout=10)
    assert calls == [] and not thread.is_alive()
    assert "DEPTIME_OFFLINE" in dt.symbol_history("httpx", "Client").note


# ---------------------------------------------------------------- .xz beside .gz (DT-DATA-35)
def _edit_manifest(out, **changes):
    data = json.loads((out / "manifest.json").read_text())
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    (out / "manifest.json").write_text(json.dumps(data))


def test_pack_writes_xz_beside_gz_and_the_manifest_describes_both(tmp_path):
    out, manifest = _published(tmp_path)
    gz, xz = out / dist.INDEX_ASSET, out / dist.XZ_ASSET
    assert gz.read_bytes() and xz.read_bytes() and not list(out.glob("*.tmp"))
    assert gzip.decompress(gz.read_bytes()) == lzma.decompress(xz.read_bytes())
    data = json.loads((out / "manifest.json").read_text())
    # the keys released clients read keep their meaning: they describe the .gz
    assert data["sha256"] == hashlib.sha256(gz.read_bytes()).hexdigest() and data["size"] == gz.stat().st_size
    assert data["xz_sha256"] == hashlib.sha256(xz.read_bytes()).hexdigest()
    assert data["xz_size"] == xz.stat().st_size
    assert (manifest.xz_sha256, manifest.xz_size) == (data["xz_sha256"], data["xz_size"])


def test_download_prefers_the_xz_when_the_manifest_lists_it(tmp_path, settings, coverage):
    out, manifest = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    calls: list[str] = []
    with _client(out, calls=calls) as client:
        assert dist.ensure_index(cfg, client=client).changed
    assert calls == ["https://idx.test/manifest.json", "https://idx.test/index.sqlite.xz"]
    dt = DepTime(IndexStore.open(dist.active_index(cfg.index_path)), coverage)
    assert dt.explain_error(HTTPX_TB, {"httpx": "0.28.1"}).findings[0].issue == "param_removed"
    meta = dist.read_local_meta(cfg.index_path)
    assert meta["manifest"]["sha256"] == manifest.sha256 and meta["asset"] == dist.XZ_ASSET
    assert not list(cfg.index_path.parent.glob("*.download*"))


def test_a_manifest_without_xz_keys_downloads_the_gz(tmp_path, settings):
    out, _ = _published(tmp_path)
    _edit_manifest(out, xz_sha256=None, xz_size=None)  # as published before DT-DATA-35
    calls: list[str] = []
    with _client(out, calls=calls) as client:
        assert dist.ensure_index(_local(settings, tmp_path), client=client).changed
    assert calls[-1] == "https://idx.test/index.sqlite.gz"


def test_without_lzma_the_client_downloads_the_gz(tmp_path, settings, monkeypatch):
    out, _ = _published(tmp_path)
    monkeypatch.setattr(dist, "lzma", None)  # a Python built without liblzma
    calls: list[str] = []
    with _client(out, calls=calls) as client:
        assert dist.ensure_index(_local(settings, tmp_path), client=client).changed
    assert calls[-1] == "https://idx.test/index.sqlite.gz"


def test_without_lzma_pack_writes_the_gz_only(tmp_path, monkeypatch):
    monkeypatch.setattr(dist, "lzma", None)
    out, manifest = _published(tmp_path)
    assert (out / dist.INDEX_ASSET).exists() and not (out / dist.XZ_ASSET).exists()
    assert manifest.xz_sha256 is None and manifest.xz_size is None


def test_xz_checksum_mismatch_keeps_the_existing_index(tmp_path, settings):
    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    with _client(out) as client:
        dist.ensure_index(cfg, client=client)
    before = dist.active_index(cfg.index_path).read_bytes()
    calls: list[str] = []
    with _client(out, corrupt=True, calls=calls) as client, pytest.raises(ValueError, match="checksum"):
        dist.download(cfg.index_url, cfg.index_path, client)
    assert calls[-1] == "https://idx.test/index.sqlite.xz"  # no second download of the .gz
    assert dist.active_index(cfg.index_path).read_bytes() == before
    assert not list(cfg.index_path.parent.glob("*.download*"))


def test_a_truncated_xz_with_a_matching_checksum_keeps_the_existing_index(tmp_path, settings):
    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    with _client(out) as client:
        dist.ensure_index(cfg, client=client)
    before = dist.active_index(cfg.index_path).read_bytes()
    xz = out / dist.XZ_ASSET
    xz.write_bytes(xz.read_bytes()[: xz.stat().st_size // 2])
    xz_sha256 = hashlib.sha256(xz.read_bytes()).hexdigest()
    _edit_manifest(out, sha256="0" * 64, xz_sha256=xz_sha256, xz_size=xz.stat().st_size)  # a new build
    with _client(out) as client:
        result = dist.ensure_index(cfg, client=client, now=dist._now() + timedelta(hours=25))
    assert result.changed is False and "keeping the current one" in result.message, result.message
    assert "EOFError" in result.message  # failed while decompressing, after the checksum passed
    assert dist.active_index(cfg.index_path).read_bytes() == before
    assert not list(cfg.index_path.parent.glob("*.download*"))


def test_an_xz_above_the_size_cap_is_refused_before_downloading(tmp_path, settings, monkeypatch):
    out, manifest = _published(tmp_path)
    monkeypatch.setattr(dist, "MAX_DOWNLOAD_BYTES", manifest.xz_size - 1)
    calls: list[str] = []
    with _client(out, calls=calls) as client, pytest.raises(ValueError, match="cap"):
        dist.download("https://idx.test", tmp_path / "home" / "index.sqlite", client)
    assert calls == ["https://idx.test/manifest.json"]


# ---------------------------------------------------------------- a new file per build (DT-OSS-08)
def _republished(tmp_path, out):
    """A second build (one more package) published to the same folder."""
    src = tmp_path / "built2.sqlite"
    store = IndexStore.open(src, readonly=False)
    store.rebuild_package(HTTPX, SNAPSHOTS, RELEASED_AT)
    store.rebuild_package(
        replace(HTTPX, name="httpx-two", import_names=("httpx_two",)), SNAPSHOTS[:1], RELEASED_AT
    )
    store.close()
    return dist.pack(src, out)


@pytest.fixture
def windows_locks(monkeypatch):
    """Windows semantics on any OS: a file an IndexStore has open can't be replaced or deleted. On Windows itself
    the OS refuses, so nothing is simulated."""
    import os
    import sys
    from pathlib import Path

    from deptime.index import location

    held: dict[Path, int] = {}
    if sys.platform == "win32":
        return held
    real_open, real_close = IndexStore.open.__func__, IndexStore.close
    real_replace, real_unlink = os.replace, Path.unlink

    def tracking_open(cls, path, *, readonly=True):
        store = real_open(cls, path, readonly=readonly)
        held[Path(path).resolve()] = held.get(Path(path).resolve(), 0) + 1
        return store

    def tracking_close(self):
        if self.path is not None:
            held[Path(self.path).resolve()] -= 1
        real_close(self)

    def guarded_replace(src, dst):
        if held.get(Path(dst).resolve()):
            raise PermissionError(13, "The process cannot access the file because it is being used", str(dst))
        real_replace(src, dst)

    def guarded_unlink(self, missing_ok=False):
        if held.get(self.resolve()):
            raise PermissionError(
                13, "The process cannot access the file because it is being used", str(self)
            )
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(IndexStore, "open", classmethod(tracking_open))
    monkeypatch.setattr(IndexStore, "close", tracking_close)
    monkeypatch.setattr(dist.os, "replace", guarded_replace)
    monkeypatch.setattr(location.Path, "unlink", guarded_unlink)
    return held


def test_a_download_gets_a_file_named_after_its_build(tmp_path, settings):
    out, manifest = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    with _client(out) as client:
        result = dist.ensure_index(cfg, client=client)
    name = f"index-{manifest.sha256[:16]}.sqlite"
    assert (
        result.path == cfg.index_path.with_name(name) and dist.read_local_meta(cfg.index_path)["file"] == name
    )
    assert not cfg.index_path.exists() and dist.remove_stale(cfg.index_path) == []
    # The next build is a new file, and the old one goes once nothing holds it.
    second = _republished(tmp_path, out)
    with _client(out) as client:
        later = dist.ensure_index(cfg, client=client, now=dist._now() + timedelta(hours=25))
    assert later.changed and later.path.name == f"index-{second.sha256[:16]}.sqlite"
    assert sorted(p.name for p in cfg.index_path.parent.glob("index*.sqlite")) == [later.path.name]


def test_a_refresh_succeeds_while_a_server_holds_the_old_file_open(
    tmp_path, settings, coverage, windows_locks
):
    """The AC: on Windows the old file can't be replaced or deleted while open. The new build gets its own file,
    another server keeps answering from the old one, and the old file goes once it is closed."""
    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    with _client(out) as client:
        first = dist.ensure_index(cfg, client=client).path
    other_server = IndexStore.open(first)
    _republished(tmp_path, out)
    with _client(out) as client:
        result = dist.ensure_index(cfg, client=client, now=dist._now() + timedelta(hours=25))
    assert result.changed and result.path != first and first.exists()  # still open, so still there
    assert DepTime(other_server, coverage).explain_error(HTTPX_TB, {"httpx": "0.28.1"}).findings
    assert dist.remove_stale(cfg.index_path) == [first]
    other_server.close()
    assert dist.remove_stale(cfg.index_path) == [] and not first.exists()


def test_the_server_switches_to_a_file_another_process_installed(tmp_path, settings, coverage, windows_locks):
    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    with _client(out) as client:
        first = dist.ensure_index(cfg, client=client).path
    dt = DepTime(IndexStore.open(first), coverage, cfg.index_path)
    _republished(tmp_path, out)
    with _client(out) as client:  # another `deptime mcp` refreshed it
        newer = dist.ensure_index(cfg, client=client, now=dist._now() + timedelta(hours=25)).path
    thread = dist.start_background_refresh(dt, replace(cfg, offline=True), log=lambda _m: None, grace=0)
    thread.join(timeout=10)
    assert dt.store.path == newer and dt.coverage_report().packages[-1].package == "httpx-two"
    for _ in range(100):  # the old store is closed after the grace period, then its file deleted
        if not first.exists():
            break
        time.sleep(0.02)
    assert not first.exists()


def test_an_index_downloaded_before_versioned_files_is_still_used_then_replaced(tmp_path, settings):
    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    with _client(out) as client:
        dist.ensure_index(cfg, client=client)
    # The layout clients before DT-OSS-08 leave: the file at index_path, metadata without "file".
    dist.active_index(cfg.index_path).rename(cfg.index_path)
    meta = dist.read_local_meta(cfg.index_path)
    del meta["file"]
    dist.meta_path(cfg.index_path).write_text(json.dumps(meta))
    assert dist.active_index(cfg.index_path) == cfg.index_path
    with _client(out) as client:
        assert dist.ensure_index(cfg, client=client).message == "index is fresh"
        result = dist.ensure_index(cfg, client=client, now=dist._now() + timedelta(hours=25))
    assert result.message == "index is up to date" and result.path == cfg.index_path
    _republished(tmp_path, out)
    with _client(out) as client:  # checked at +25 h above
        result = dist.ensure_index(cfg, client=client, now=dist._now() + timedelta(hours=50))
    assert result.changed and result.path.name.startswith("index-") and not cfg.index_path.exists()


def test_the_metadata_names_only_a_file_beside_it(tmp_path, settings):
    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    with _client(out) as client:
        dist.ensure_index(cfg, client=client)
    meta = dist.read_local_meta(cfg.index_path)
    elsewhere = tmp_path / "elsewhere.sqlite"
    elsewhere.write_bytes(b"")
    for name in ["../elsewhere.sqlite", str(elsewhere), "index-zz.sqlite", 7]:
        dist.meta_path(cfg.index_path).write_text(json.dumps({**meta, "file": name}))
        assert dist.active_index(cfg.index_path) == cfg.index_path


def test_ingest_into_a_downloaded_index_starts_from_its_file(tmp_path, settings, monkeypatch):
    """CI's publish job pulls the published index, then ingests into it with --into-downloaded."""
    from deptime import cli
    from deptime.ingest.pipeline import IngestReport

    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    with _client(out) as client:
        dist.ensure_index(cfg, client=client)
    monkeypatch.setenv("DEPTIME_HOME", str(cfg.index_path.parent))
    monkeypatch.delenv("DEPTIME_INDEX_PATH", raising=False)
    seen = []

    def fake_ingest(spec, policy, store, *args, **kwargs):
        seen.append([r["name"] for r in store.package_rows()])
        return IngestReport(spec.name, unchanged=True)

    monkeypatch.setattr("deptime.ingest.pipeline.ingest_package", fake_ingest)
    assert cli.main(["ingest", "httpx", "--into-downloaded"]) == 0
    assert seen == [["httpx"]]  # the downloaded rows, not an empty new file
    assert dist.read_local_meta(cfg.index_path) is None and cfg.index_path.exists()
    assert not list(cfg.index_path.parent.glob("index-*.sqlite"))


# ---------------------------------------------------------------- build provenance (DT-OSS-09)
GH_REPO = "acme/deptime"
GH_URL = f"https://github.com/{GH_REPO}/releases/download/index"
WORKFLOW_SAN = f"https://github.com/{GH_REPO}/{dist.BUILD_WORKFLOW}@{dist.BUILD_REF}"


def _gh_result(sha256, statement=None, **cert):
    """The part of `gh attestation verify --format json` output that DepTime reads."""
    certificate = {
        "issuer": dist.OIDC_ISSUER,
        "subjectAlternativeName": WORKFLOW_SAN,
        "sourceRepositoryURI": f"https://github.com/{GH_REPO}",
        "sourceRepositoryRef": dist.BUILD_REF,
        "sourceRepositoryDigest": "0123456789abcdef0123456789abcdef01234567",
        "runnerEnvironment": "github-hosted",
        "runInvocationURI": f"https://github.com/{GH_REPO}/actions/runs/42/attempts/1",
        **cert,
    }
    subject = [{"name": "index.sqlite.xz", "digest": {"sha256": sha256}}]
    result = {
        "signature": {"certificate": certificate},
        "statement": {"predicateType": dist.SLSA_PROVENANCE, "subject": subject, **(statement or {})},
        "verifiedTimestamps": [{"type": "Tlog", "timestamp": "2026-11-07T09:00:00-05:00"}],
    }
    return [{"attestation": {}, "verificationResult": result}]


class FakeGh:
    """Stands in for `gh attestation verify`: records each call, then answers like gh would."""

    def __init__(self):
        self.calls, self.bundles = [], []
        self.returncode, self.stderr = 0, ""
        self.result = _gh_result  # sha256 of the file passed -> verified attestations

    def run(self, args):
        self.calls.append(args)
        bundle = args[args.index("--bundle") + 1] if "--bundle" in args else None
        self.bundles.append(Path(bundle).read_text() if bundle else None)
        if self.returncode:
            return subprocess.CompletedProcess(args, self.returncode, "", self.stderr)
        sha = hashlib.sha256(Path(args[3]).read_bytes()).hexdigest()
        return subprocess.CompletedProcess(args, 0, json.dumps(self.result(sha)), "")


@pytest.fixture
def fake_gh(monkeypatch):
    gh = FakeGh()
    monkeypatch.setattr(dist, "_find_gh", lambda: "gh")
    monkeypatch.setattr(dist, "_run_gh", gh.run)
    return gh


def _installed(settings, tmp_path, out):
    cfg = _local(settings, tmp_path)
    with _client(out) as client:
        dist.ensure_index(cfg, client=client)
    return cfg


@pytest.mark.parametrize(
    "url, repo",
    [
        ("https://github.com/cschmertz/deptime/releases/download/index", "cschmertz/deptime"),
        (f"{GH_URL}/", GH_REPO),
        ("https://idx.test", None),
        ("http://github.com/acme/deptime/releases/download/index", None),
        ("https://github.com/acme/deptime/releases/latest", None),
        ("https://github.com.evil.test/acme/deptime/releases/download/index", None),
    ],
)
def test_the_repository_to_verify_against_comes_from_the_index_url(url, repo):
    assert dist.release_repo(url) == repo


def test_the_default_index_url_verifies_against_this_repository():
    from deptime.config import DEFAULT_INDEX_URL

    assert dist.release_repo(DEFAULT_INDEX_URL) == "cschmertz/deptime"


def test_a_verified_download_is_installed_and_records_its_build(tmp_path, settings, fake_gh):
    out, manifest = _published(tmp_path)
    (out / dist.ATTESTATION_ASSET).write_text('{"mediaType": "bundle"}\n')
    cfg = replace(_local(settings, tmp_path), index_url=GH_URL, verify_index=True)
    with _client(out) as client:
        result = dist.ensure_index(cfg, client=client)
    assert result.changed and result.message.endswith("build provenance verified")
    (args,) = fake_gh.calls
    assert args[:3] == ["gh", "attestation", "verify"] and args[3].endswith(".xz")
    assert args[args.index("--repo") + 1] == GH_REPO
    assert args[args.index("--signer-workflow") + 1] == f"{GH_REPO}/{dist.BUILD_WORKFLOW}"
    assert fake_gh.bundles == ['{"mediaType": "bundle"}\n']  # the published bundle, so gh needs no login
    meta = dist.read_local_meta(cfg.index_path)
    assert meta["manifest"]["sha256"] == manifest.sha256
    assert meta["provenance"]["repository"] == GH_REPO
    assert meta["provenance"]["run"] == f"https://github.com/{GH_REPO}/actions/runs/42/attempts/1"
    assert meta["provenance"]["commit"].startswith("0123456789ab")
    assert not list(cfg.index_path.parent.glob("*.download*"))


def test_without_a_published_bundle_gh_looks_the_attestation_up_itself(tmp_path, settings, fake_gh):
    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    with _client(out) as client:
        dist.download(GH_URL, cfg.index_path, client, verify=True)
    (args,) = fake_gh.calls
    assert "--bundle" not in args and fake_gh.bundles == [None]
    assert dist.read_local_meta(cfg.index_path)["provenance"]["repository"] == GH_REPO


def test_an_unverified_download_records_no_provenance_and_never_runs_gh(tmp_path, settings, fake_gh):
    out, _ = _published(tmp_path)
    cfg = _installed(settings, tmp_path, out)
    assert "provenance" not in dist.read_local_meta(cfg.index_path) and fake_gh.calls == []


@pytest.mark.parametrize(
    "cert, statement",
    [
        (
            {
                "subjectAlternativeName": f"https://github.com/{GH_REPO}/.github/workflows/release.yml@refs/heads/main"
            },
            None,
        ),
        ({"subjectAlternativeName": WORKFLOW_SAN.replace("build-index.yml", "build-index.yml.evil")}, None),
        (
            {
                "subjectAlternativeName": WORKFLOW_SAN.replace("main", "dev"),
                "sourceRepositoryRef": "refs/heads/dev",
            },
            None,
        ),
        (
            {
                "subjectAlternativeName": WORKFLOW_SAN.replace("main", "Main"),
                "sourceRepositoryRef": "refs/heads/Main",
            },
            None,
        ),
        ({"sourceRepositoryRef": "refs/heads/dev"}, None),
        ({"runnerEnvironment": "self-hosted"}, None),
        ({"issuer": "https://accounts.example.test"}, None),
        (
            {
                "sourceRepositoryURI": "https://github.com/evil/deptime",
                "subjectAlternativeName": WORKFLOW_SAN.replace(GH_REPO, "evil/deptime"),
            },
            None,
        ),
        ({}, {"predicateType": "https://example.test/custom/v1"}),
        ({}, {"subject": [{"name": "index.sqlite.xz", "digest": {"sha256": "0" * 64}}]}),
    ],
    ids=[
        "other-workflow",
        "workflow-prefix",
        "other-branch",
        "branch-case",
        "ref-mismatch",
        "self-hosted",
        "other-issuer",
        "other-repo",
        "other-predicate",
        "other-file",
    ],
)
def test_an_attestation_from_another_signer_keeps_the_current_index(
    tmp_path, settings, fake_gh, cert, statement
):
    out, _ = _published(tmp_path)
    cfg = _installed(settings, tmp_path, out)
    before = dist.read_local_meta(cfg.index_path)
    fake_gh.result = lambda sha: _gh_result(sha, statement, **cert)
    with _client(out) as client, pytest.raises(dist.ProvenanceError, match="no attestation for this file"):
        dist.download(GH_URL, cfg.index_path, client, verify=True)
    assert dist.read_local_meta(cfg.index_path) == before
    assert not list(cfg.index_path.parent.glob("*.download*"))


def test_an_owner_named_in_another_case_still_verifies(tmp_path, settings, fake_gh):
    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    url = GH_URL.replace("acme", "ACME")
    with _client(out) as client:
        dist.download(url, cfg.index_path, client, verify=True)
    assert dist.read_local_meta(cfg.index_path)["provenance"]["repository"] == GH_REPO


def test_when_gh_rejects_the_file_its_reason_is_given_and_nothing_is_installed(tmp_path, settings, fake_gh):
    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    fake_gh.returncode, fake_gh.stderr = (
        1,
        'Loaded digest sha256:abc\nError: verifying with issuer "sigstore.dev"\n',
    )
    with (
        _client(out) as client,
        pytest.raises(dist.ProvenanceError, match=r"rejected the index: .*sigstore\.dev"),
    ):
        dist.download(GH_URL, cfg.index_path, client, verify=True)
    assert dist.read_local_meta(cfg.index_path) is None
    assert not list(cfg.index_path.parent.glob("index-*.sqlite"))
    assert not list(cfg.index_path.parent.glob("*.download*"))


def test_verifying_without_gh_says_how_to_get_it(tmp_path, settings, monkeypatch):
    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    monkeypatch.setattr(dist, "_find_gh", lambda: None)
    with _client(out) as client, pytest.raises(dist.ProvenanceError, match="GitHub CLI"):
        dist.download(GH_URL, cfg.index_path, client, verify=True)
    assert dist.read_local_meta(cfg.index_path) is None


def test_verifying_needs_an_index_served_from_github_releases(tmp_path, settings, fake_gh):
    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    with _client(out) as client, pytest.raises(dist.ProvenanceError, match="GitHub repository's releases"):
        dist.download(cfg.index_url, cfg.index_path, client, verify=True)
    assert fake_gh.calls == []


def test_a_refresh_that_must_verify_keeps_the_current_index_when_it_fails(tmp_path, settings, fake_gh):
    out, _ = _published(tmp_path)
    cfg = _installed(settings, tmp_path, out)
    before = dist.active_index(cfg.index_path).read_bytes()
    meta = dist.read_local_meta(cfg.index_path)
    dist.meta_path(cfg.index_path).write_text(
        json.dumps(
            {
                **meta,
                "manifest": {**meta["manifest"], "sha256": "old"},
                "checked_at": "2000-01-01T00:00:00+00:00",
            }
        )
    )
    fake_gh.returncode, fake_gh.stderr = 1, "Error: no attestations found\n"
    cfg = replace(cfg, index_url=GH_URL, verify_index=True)
    with _client(out) as client:
        result = dist.ensure_index(cfg, client=client)
    assert (
        not result.changed
        and "ProvenanceError" in result.message
        and "keeping the current one" in result.message
    )
    assert dist.active_index(cfg.index_path).read_bytes() == before


def test_index_pull_verify_checks_a_current_index_that_was_installed_without_it(
    tmp_path, settings, fake_gh, monkeypatch, capsys
):
    from deptime import cli

    out, _ = _published(tmp_path)
    cfg = _installed(settings, tmp_path, out)
    monkeypatch.setenv("DEPTIME_HOME", str(cfg.index_path.parent))
    monkeypatch.setenv("DEPTIME_INDEX_URL", GH_URL)
    for name in ("DEPTIME_INDEX_PATH", "DEPTIME_VERIFY_INDEX"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(dist, "http_client", lambda: _client(out))

    assert cli.main(["index", "pull"]) == 0  # current, and not asked to verify
    assert fake_gh.calls == []
    assert cli.main(["index", "pull", "--verify"]) == 0  # current but never verified: downloaded and checked
    assert len(fake_gh.calls) == 1
    assert "build provenance verified: acme/deptime .github/workflows/build-index.yml@refs/heads/main" in (
        capsys.readouterr().err
    )
    assert cli.main(["index", "pull", "--verify"]) == 0  # verified already
    assert len(fake_gh.calls) == 1 and "up to date" in capsys.readouterr().err

    assert cli.main(["index", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["provenance"]["repository"] == GH_REPO and status["verify_index"] is False

    meta = dist.read_local_meta(cfg.index_path)
    fake_gh.returncode, fake_gh.stderr = 1, "Error: no attestations found\n"
    assert cli.main(["index", "pull", "--verify", "--force"]) == 1
    assert "Nothing was installed; the current index is unchanged." in capsys.readouterr().err
    assert dist.read_local_meta(cfg.index_path) == meta


def test_index_pull_reports_a_failed_download_and_keeps_the_current_index(
    tmp_path, settings, monkeypatch, capsys
):
    from deptime import cli

    out, _ = _published(tmp_path)
    cfg = _installed(settings, tmp_path, out)
    meta = dist.read_local_meta(cfg.index_path)
    monkeypatch.setenv("DEPTIME_HOME", str(cfg.index_path.parent))
    monkeypatch.setenv("DEPTIME_INDEX_URL", "https://idx.test")
    for name in ("DEPTIME_INDEX_PATH", "DEPTIME_VERIFY_INDEX", "DEPTIME_OFFLINE"):
        monkeypatch.delenv(name, raising=False)
    failing = {dist.XZ_ASSET, dist.INDEX_ASSET}

    def handler(request: httpx.Request) -> httpx.Response:
        name = str(request.url).rsplit("/", 1)[-1]
        if name in failing:
            return httpx.Response(504, text="Gateway Time-out")
        return httpx.Response(200, content=(out / name).read_bytes())

    monkeypatch.setattr(dist, "http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    capsys.readouterr()

    assert cli.main(["index", "pull", "--force"]) == 1  # the asset times out
    err = capsys.readouterr().err
    assert "could not pull the index (HTTP 504 Gateway Timeout from https://idx.test/index.sqlite." in err
    assert "Nothing was installed; the current index is unchanged." in err and "Traceback" not in err
    failing.add(dist.MANIFEST_ASSET)
    assert cli.main(["index", "pull"]) == 1  # so does the manifest
    assert "could not pull the index" in capsys.readouterr().err
    assert dist.read_local_meta(cfg.index_path) == meta
    assert sorted(p.name for p in cfg.index_path.parent.iterdir() if "download" in p.name) == []


# ---------------------------------------------------------------- audit 2026-11-20, session 2
@pytest.mark.parametrize("verify", [False, True], ids=["force", "verify"])
def test_a_damaged_file_of_the_current_build_is_replaced_by_the_fresh_download(
    tmp_path, settings, fake_gh, verify
):
    # B1: the file is named after the build, so a new download of the same build used to be thrown away when a file
    # of that name was there, while the metadata recorded the new download's provenance.
    out, _ = _published(tmp_path)
    cfg = _installed(settings, tmp_path, out)
    active = dist.active_index(cfg.index_path)
    good = active.read_bytes()
    active.write_bytes(good[:4096] + b"\0" * (len(good) - 4096))  # damaged, or installed from other bytes
    with _client(out) as client:
        dist.download(GH_URL if verify else cfg.index_url, cfg.index_path, client, verify=verify)
    assert dist.active_index(cfg.index_path) == active and active.read_bytes() == good
    assert ("provenance" in dist.read_local_meta(cfg.index_path)) is verify


def test_a_second_download_of_the_build_a_server_holds_open_is_not_needed(
    tmp_path, settings, coverage, windows_locks
):
    """Another process installed this build already and serves from it: the same bytes aren't replaced."""
    out, _ = _published(tmp_path)
    cfg = _installed(settings, tmp_path, out)
    server = IndexStore.open(dist.active_index(cfg.index_path))
    with _client(out) as client:
        dist.download(cfg.index_url, cfg.index_path, client)
    assert DepTime(server, coverage).explain_error(HTTPX_TB, {"httpx": "0.28.1"}).findings
    server.close()


def _bomb(out, raw: bytes):
    """Publish `raw` as the .xz of a new build, with a manifest that matches it."""
    xz = out / dist.XZ_ASSET
    xz.write_bytes(lzma.compress(raw))
    _edit_manifest(
        out, sha256="1" * 64, xz_sha256=hashlib.sha256(xz.read_bytes()).hexdigest(), xz_size=xz.stat().st_size
    )


def test_a_download_that_expands_far_beyond_its_size_is_refused_while_decompressing(
    tmp_path, settings, monkeypatch
):
    # B3: decompression had no cap; a small file could fill the disk before the check that it is SQLite.
    out, _ = _published(tmp_path)
    _bomb(out, b"\0" * (8 * 1024**2))
    monkeypatch.setattr(dist, "MIN_EXPANSION_CAP", 1024**2)
    written = []
    real = dist._copy_capped

    def spy(src, dst, cap):
        written.append(cap)
        return real(src, dst, cap)

    monkeypatch.setattr(dist, "_copy_capped", spy)
    with _client(out) as client, pytest.raises(ValueError, match="expands to more than"):
        dist.download("https://idx.test", tmp_path / "home" / "index.sqlite", client)
    assert written and written[0] < 8 * 1024**2
    assert not list((tmp_path / "home").glob("*.download*"))


def test_the_real_index_expands_well_within_the_cap(tmp_path):
    # The published index is ~15x its .xz and ~10x its .gz (808 MB from 52 MB and 80 MB on 2026-11-19).
    assert dist.MAX_EXPANSION >= 3 * 16
    out, manifest = _published(tmp_path)
    raw = lzma.decompress((out / dist.XZ_ASSET).read_bytes())
    assert len(raw) <= dist._expansion_cap(manifest.xz_size)


def _sqlite_with(tmp_path, *statements):
    import sqlite3

    path = tmp_path / "evil.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(";\n".join(statements))
    conn.commit()
    conn.close()
    return path


@pytest.mark.parametrize(
    "statements",
    [
        # a `meta` view that never finishes: the check used to hang, and with it the background refresh
        (
            "CREATE TABLE packages (name TEXT)",
            "CREATE VIEW meta AS WITH RECURSIVE r(key, value) AS (SELECT 'k', 'v' UNION ALL"
            " SELECT key, value FROM r) SELECT key, value FROM r",
        ),
        (
            "CREATE TABLE meta (key TEXT, value TEXT)",
            "INSERT INTO meta VALUES ('schema_version', '2')",
            "CREATE TABLE packages (name TEXT)",
            "CREATE VIEW spans AS SELECT 1",
        ),
        (
            "CREATE TABLE meta (key TEXT, value TEXT)",
            "INSERT INTO meta VALUES ('schema_version', '2')",
            "CREATE TABLE packages (name TEXT)",
            "CREATE TRIGGER t AFTER INSERT ON packages BEGIN DELETE FROM meta; END",
        ),
    ],
    ids=["recursive-view", "view", "trigger"],
)
def test_a_downloaded_file_with_views_or_triggers_is_refused(tmp_path, statements):
    # B3: the index never has views or triggers; a downloaded file that does isn't opened as one.
    with pytest.raises(ValueError, match="views or triggers"):
        dist._validate(_sqlite_with(tmp_path, *statements))


def test_leftovers_of_an_interrupted_download_are_cleaned_up(tmp_path, settings):
    # B4: a download killed midway (the refresh runs in a daemon thread) left its temp files for ever.
    import os

    out, _ = _published(tmp_path)
    cfg = _installed(settings, tmp_path, out)
    home = cfg.index_path.parent
    old = [
        home / "index.sqlite.download-a1b2.xz",
        home / "index.sqlite.download-c3d4.sqlite",
        home / "index.sqlite.download-a1b2.xz.e5f6.sigstore.json",
        home / "index.sqlite.meta.json.g7h8.tmp",
    ]
    fresh = home / "index.sqlite.download-zz99.xz"  # another process may be downloading right now
    hours_ago = time.time() - 3 * 3600
    for path in [*old, fresh]:
        path.write_bytes(b"partial")
    for path in old:
        os.utime(path, (hours_ago, hours_ago))
    with _client(out) as client:
        assert dist.ensure_index(cfg, client=client).message == "index is fresh"
    assert [p for p in old if p.exists()] == [] and fresh.exists()


@pytest.mark.parametrize(
    "stamp",
    [None, "not a date", "2026-11-20T10:00:00", "2999-01-01T00:00:00+00:00", 7],
    ids=["missing", "junk", "naive", "future", "number"],
)
def test_metadata_with_an_unreadable_or_future_check_time_is_checked_again(tmp_path, settings, stamp):
    # B5: the parse raised outside the try (and killed the refresh thread); a future time suppressed refreshing.
    out, _ = _published(tmp_path)
    cfg = _installed(settings, tmp_path, out)
    meta = dist.read_local_meta(cfg.index_path)
    meta.pop("downloaded_at")
    meta.pop("checked_at")
    if stamp is not None:
        meta["checked_at"] = stamp
    dist.meta_path(cfg.index_path).write_text(json.dumps(meta))
    calls: list[str] = []
    with _client(out, calls=calls) as client:
        result = dist.ensure_index(cfg, client=client)
    if stamp == "2026-11-20T10:00:00":  # naive: read as UTC, so it is fresh or stale like any other
        assert result.message in ("index is fresh", "index is up to date")
    else:
        assert result.message == "index is up to date" and calls == ["https://idx.test/manifest.json"]


def _pull(monkeypatch, cfg, client_factory):
    from deptime import cli

    monkeypatch.setenv("DEPTIME_HOME", str(cfg.index_path.parent))
    monkeypatch.setenv("DEPTIME_INDEX_URL", "https://idx.test")
    for name in ("DEPTIME_INDEX_PATH", "DEPTIME_VERIFY_INDEX", "DEPTIME_OFFLINE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(dist, "http_client", client_factory)
    return cli.main(["index", "pull", "--force"])


def test_index_pull_reports_a_truncated_download_without_a_traceback(tmp_path, settings, monkeypatch, capsys):
    # B6: EOFError from a truncated .xz whose checksum matched printed a traceback.
    out, _ = _published(tmp_path)
    cfg = _installed(settings, tmp_path, out)
    meta = dist.read_local_meta(cfg.index_path)
    xz = out / dist.XZ_ASSET
    xz.write_bytes(xz.read_bytes()[: xz.stat().st_size // 2])
    _edit_manifest(out, xz_sha256=hashlib.sha256(xz.read_bytes()).hexdigest(), xz_size=xz.stat().st_size)
    capsys.readouterr()
    assert _pull(monkeypatch, cfg, lambda: _client(out)) == 1
    err = capsys.readouterr().err
    assert "could not pull the index" in err and "didn't decompress" in err and "Traceback" not in err
    assert dist.read_local_meta(cfg.index_path) == meta


@pytest.mark.parametrize(
    "changes",
    [{"size": None}, {"size": "80 MB"}, {"sha256": 5}, {"schema_version": None}],
    ids=["no-size", "text-size", "number-sha", "no-schema"],
)
def test_index_pull_reports_a_malformed_manifest_without_a_traceback(
    tmp_path, settings, monkeypatch, capsys, changes
):
    # B6: a manifest missing `size` raised TypeError.
    out, _ = _published(tmp_path)
    cfg = _installed(settings, tmp_path, out)
    _edit_manifest(out, xz_sha256=None, xz_size=None, **changes)
    capsys.readouterr()
    assert _pull(monkeypatch, cfg, lambda: _client(out)) == 1
    err = capsys.readouterr().err
    assert "could not pull the index" in err and "manifest" in err and "Traceback" not in err


def test_index_pull_reports_a_full_disk_without_a_traceback(tmp_path, settings, monkeypatch, capsys):
    # B6: OSError (ENOSPC) while writing printed a traceback; the index and temp files are left as they were.
    import errno

    out, _ = _published(tmp_path)
    cfg = _installed(settings, tmp_path, out)
    meta = dist.read_local_meta(cfg.index_path)

    def full(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(dist, "_copy_capped", full)
    capsys.readouterr()
    assert _pull(monkeypatch, cfg, lambda: _client(out)) == 1
    err = capsys.readouterr().err
    assert "could not pull the index" in err and "No space left on device" in err and "Traceback" not in err
    assert dist.read_local_meta(cfg.index_path) == meta
    assert not list(cfg.index_path.parent.glob("*.download*"))


@pytest.mark.parametrize("why", ["no-gh", "not-github"])
def test_a_download_that_cant_be_verified_is_refused_before_downloading_it(
    tmp_path, settings, monkeypatch, fake_gh, why
):
    # B7: without gh, or from a server that isn't a GitHub release, the whole file was downloaded first.
    out, _ = _published(tmp_path)
    cfg = _local(settings, tmp_path)
    url = GH_URL if why == "no-gh" else "https://idx.test"
    if why == "no-gh":
        monkeypatch.setattr(dist, "_find_gh", lambda: None)
    calls: list[str] = []
    with _client(out, calls=calls) as client, pytest.raises(dist.ProvenanceError):
        dist.download(url, cfg.index_path, client, verify=True)
    assert calls == [f"{url}/manifest.json"] and fake_gh.calls == []
