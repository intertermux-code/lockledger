"""`lockledger cache` and the download helpers, against a fake transport."""

import hashlib
import json
from pathlib import Path

import pytest

from conftest import npm_tarball, pypi_wheel, run_cli, write_npm_lock

from lockledger import fetch as fetch_module


class FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


@pytest.fixture
def artifacts(tmp_path):
    directory = tmp_path / "artifacts"
    directory.mkdir()
    return directory


@pytest.fixture
def registry(monkeypatch):
    """An in-memory registry: responses keyed by exact URL, recording every request."""

    class Transport:
        def __init__(self):
            self.responses = {}
            self.requests = []

        def add(self, url, payload):
            self.responses[url] = payload

        def urlopen(self, request, timeout=None):
            url = request.full_url if hasattr(request, "full_url") else request
            self.requests.append(url)
            if url not in self.responses:
                raise _http_error(404, url)
            return FakeResponse(self.responses[url])

    transport = Transport()
    monkeypatch.setattr(fetch_module.urllib.request, "urlopen", transport.urlopen)
    return transport


def _http_error(code, url):
    error = fetch_module.urllib.error.HTTPError(url, code, "Not Found", {}, None)
    return error


REGISTRY = "http://registry.test"


def test_cache_downloads_an_npm_tarball(artifacts, registry, cache_dir, db, capsys):
    artifact = npm_tarball(artifacts / "widget-1.0.0.tgz", "widget", "1.0.0")
    url = f"{REGISTRY}/widget/-/widget-1.0.0.tgz"
    registry.add(url, artifact.read_bytes())

    code = run_cli(
        "--db", str(db), "--cache-dir", str(cache_dir),
        "cache", "widget@1.0.0", "--ecosystem", "npm", "--registry", REGISTRY,
    )
    assert code == 0
    assert registry.requests == [url]
    destination = cache_dir / "npm" / "widget" / "1.0.0" / "widget-1.0.0.tgz"
    assert destination.read_bytes() == artifact.read_bytes()
    assert str(destination) in capsys.readouterr().out


def test_cache_is_a_no_op_when_the_artifact_is_already_there(artifacts, registry, cache_dir, db, capsys):
    artifact = npm_tarball(artifacts / "widget-1.0.0.tgz", "widget", "1.0.0")
    registry.add(f"{REGISTRY}/widget/-/widget-1.0.0.tgz", artifact.read_bytes())
    argv = [
        "--db", str(db), "--cache-dir", str(cache_dir),
        "cache", "widget@1.0.0", "--ecosystem", "npm", "--registry", REGISTRY,
    ]
    assert run_cli(*argv) == 0
    capsys.readouterr()

    requests = len(registry.requests)
    assert run_cli(*argv) == 0
    assert len(registry.requests) == requests
    assert "already cached" in capsys.readouterr().out


def test_cache_scoped_npm_package(artifacts, registry, cache_dir, db):
    artifact = npm_tarball(artifacts / "pkg-1.0.0.tgz", "@scope/pkg", "1.0.0")
    registry.add(f"{REGISTRY}/@scope/pkg/-/pkg-1.0.0.tgz", artifact.read_bytes())
    code = run_cli(
        "--db", str(db), "--cache-dir", str(cache_dir),
        "cache", "@scope/pkg@1.0.0", "--ecosystem", "npm", "--registry", REGISTRY,
    )
    assert code == 0
    assert (cache_dir / "npm" / "@scope__pkg" / "1.0.0" / "pkg-1.0.0.tgz").is_file()


def test_cache_uses_the_resolved_url_recorded_in_the_lockfile(artifacts, registry, db, cache_dir, locks):
    lock = write_npm_lock(locks, 1, [("left-pad", "1.3.0")])
    assert run_cli("--db", str(db), "add", str(lock)) == 0
    artifact = npm_tarball(artifacts / "left-pad-1.3.0.tgz", "left-pad", "1.3.0")
    registry.add("https://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz", artifact.read_bytes())

    code = run_cli(
        "--db", str(db), "--cache-dir", str(cache_dir),
        "cache", "left-pad@1.3.0", "--registry", REGISTRY,
    )
    assert code == 0
    # The lockfile says where the package came from, so that URL wins.
    assert registry.requests == ["https://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz"]


def test_cache_chooses_the_ecosystem_from_the_store(artifacts, registry, db, cache_dir, locks):
    requirements = locks / "requirements.txt"
    requirements.write_text("coldpkg==1.0.0\n")
    assert run_cli("--db", str(db), "add", str(requirements)) == 0

    wheel = pypi_wheel(artifacts / "coldpkg-1.0.0-py3-none-any.whl", "coldpkg", "1.0.0")
    registry.add(f"{REGISTRY}/coldpkg/1.0.0/json", json.dumps(
        {"urls": [{"filename": wheel.name, "url": f"{REGISTRY}/files/{wheel.name}",
                   "packaging": "bdist_wheel"}]}
    ).encode())
    registry.add(f"{REGISTRY}/files/{wheel.name}", wheel.read_bytes())

    code = run_cli(
        "--db", str(db), "--cache-dir", str(cache_dir),
        "cache", "coldpkg@1.0.0", "--registry", REGISTRY,
    )
    assert code == 0
    assert (cache_dir / "pypi" / "coldpkg" / "1.0.0" / wheel.name).is_file()
    assert registry.requests[0] == f"{REGISTRY}/coldpkg/1.0.0/json"


def test_cache_prefers_an_sdist_over_a_wheel(artifacts, registry, db, cache_dir):
    sdist = artifacts / "warm-1.0.0.tar.gz"
    sdist.write_bytes(b"tarball")
    registry.add(f"{REGISTRY}/warmpkg/1.0.0/json", json.dumps(
        {"urls": [
            {"filename": "warmpkg-1.0.0-py3-none-any.whl", "url": f"{REGISTRY}/files/w.whl",
             "packaging": "bdist_wheel"},
            {"filename": "warmpkg-1.0.0.tar.gz", "url": f"{REGISTRY}/files/w.tar.gz",
             "packaging": "sdist"},
        ]}
    ).encode())
    registry.add(f"{REGISTRY}/files/w.tar.gz", b"tarball")

    code = run_cli(
        "--db", str(db), "--cache-dir", str(cache_dir),
        "cache", "warmpkg@1.0.0", "--ecosystem", "pypi", "--registry", REGISTRY,
    )
    assert code == 0
    assert (cache_dir / "pypi" / "warmpkg" / "1.0.0" / "warmpkg-1.0.0.tar.gz").is_file()


def test_cache_verifies_the_recorded_integrity(artifacts, registry, db, cache_dir, locks, capsys):
    lock = locks / "verified.json"
    payload = npm_tarball(artifacts / "verified-1.0.0.tgz", "verified", "1.0.0").read_bytes()
    digest = base64_sha512(payload)
    entries = {
        "": {"name": "root"},
        "node_modules/verified": {
            "version": "1.0.0",
            "resolved": f"{REGISTRY}/verified/-/verified-1.0.0.tgz",
            "integrity": f"sha512-{digest}",
        },
    }
    lock.write_text(json.dumps({"lockfileVersion": 3, "packages": entries}))
    assert run_cli("--db", str(db), "add", str(lock)) == 0

    registry.add(f"{REGISTRY}/verified/-/verified-1.0.0.tgz", payload)
    argv = [
        "--db", str(db), "--cache-dir", str(cache_dir),
        "cache", "verified@1.0.0", "--registry", REGISTRY,
    ]
    assert run_cli(*argv) == 0
    assert (cache_dir / "npm" / "verified" / "1.0.0" / "verified-1.0.0.tgz").is_file()

    # A substituted artifact never reaches the cache.
    tampered = npm_tarball(artifacts / "verified-1.0.1.tgz", "verified", "1.0.0",
                           files={"evil.js": "payload"}).read_bytes()
    registry.add(f"{REGISTRY}/verified/-/verified-1.0.0.tgz", tampered)
    (cache_dir / "npm" / "verified" / "1.0.0" / "verified-1.0.0.tgz").unlink()
    capsys.readouterr()

    assert run_cli(*argv) == 1
    assert "sha512 mismatch" in capsys.readouterr().err
    assert not (cache_dir / "npm" / "verified" / "1.0.0" / "verified-1.0.0.tgz").exists()


def test_cache_reports_a_missing_artifact(registry, db, cache_dir, capsys):
    code = run_cli(
        "--db", str(db), "--cache-dir", str(cache_dir),
        "cache", "ghost@9.9.9", "--ecosystem", "npm", "--registry", REGISTRY,
    )
    assert code == 1
    assert "HTTP 404" in capsys.readouterr().err


def test_cache_rejects_path_traversal_names(registry, db, cache_dir, capsys):
    code = run_cli(
        "--db", str(db), "--cache-dir", str(cache_dir),
        "cache", "../../etc/passwd@1.0", "--ecosystem", "npm",
    )
    assert code == 1
    assert "unsafe package name" in capsys.readouterr().err


def test_cache_requires_a_version(registry, db, cache_dir, capsys):
    assert run_cli("--db", str(db), "cache", "nokey", "--ecosystem", "npm") == 1
    assert "expected name@version" in capsys.readouterr().err


def test_cache_asks_which_ecosystem_when_a_name_is_ambiguous(artifacts, registry, db, cache_dir,
                                                             locks, capsys):
    write_npm_lock(locks, 1, [("dual", "1.0.0")])
    run_cli("--db", str(db), "add", str(locks / "package-lock-1.json"))
    (locks / "requirements.txt").write_text("dual==1.0.0\n")
    run_cli("--db", str(db), "add", str(locks / "requirements.txt"))
    capsys.readouterr()

    code = run_cli("--db", str(db), "--cache-dir", str(cache_dir), "cache", "dual@1.0.0")
    assert code == 1
    assert "appears as npm, pypi" in capsys.readouterr().err

    registry.add(f"{REGISTRY}/dual/-/dual-1.0.0.tgz",
                 npm_tarball(artifacts / "dual-1.0.0.tgz", "dual", "1.0.0").read_bytes())
    assert run_cli("--db", str(db), "--cache-dir", str(cache_dir),
                   "cache", "dual@1.0.0", "--ecosystem", "npm", "--registry", REGISTRY) == 0


def test_cache_json_lists_every_artifact(artifacts, registry, db, cache_dir, capsys):
    for name in ("left-pad", "through"):
        registry.add(
            f"{REGISTRY}/{name}/-/{name}-1.0.0.tgz",
            npm_tarball(artifacts / f"{name}-1.0.0.tgz", name, "1.0.0").read_bytes(),
        )
    code = run_cli(
        "--db", str(db), "--cache-dir", str(cache_dir),
        "cache", "left-pad@1.0.0", "through@1.0.0",
        "--ecosystem", "npm", "--registry", REGISTRY, "--json",
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["cache_dir"] == str(cache_dir)
    assert [entry["name"] for entry in payload["artifacts"]] == ["left-pad", "through"]
    assert all(entry["already_cached"] is False for entry in payload["artifacts"])


def test_cache_refuses_a_registry_filename_that_escapes_the_cache(artifacts, registry, db,
                                                                  cache_dir, capsys):
    """A malicious or broken mirror must not be able to write outside the cache."""
    wheel = pypi_wheel(artifacts / "slip-1.0.0-py3-none-any.whl", "slip", "1.0.0")
    registry.add(f"{REGISTRY}/slip/1.0.0/json", json.dumps(
        {"urls": [{"filename": "../../../../tmp/evil.py", "url": f"{REGISTRY}/files/slip.whl",
                   "packaging": "bdist_wheel"}]}
    ).encode())
    registry.add(f"{REGISTRY}/files/slip.whl", wheel.read_bytes())

    code = run_cli(
        "--db", str(db), "--cache-dir", str(cache_dir),
        "cache", "slip@1.0.0", "--ecosystem", "pypi", "--registry", REGISTRY, "--json",
    )
    assert code == 0
    written = json.loads(capsys.readouterr().out)["artifacts"][0]["path"]
    assert Path(written).parent == cache_dir / "pypi" / "slip" / "1.0.0"
    assert Path(written).name == "slip-1.0.0.tar.gz"


def test_url_construction_is_pure():
    assert fetch_module.artifact_url("npm", "left-pad", "1.3.0", "") == \
        "https://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz"
    assert fetch_module.artifact_url("npm", "@scope/pkg", "2.0.0", "https://npm.corp/") == \
        "https://npm.corp/@scope/pkg/-/pkg-2.0.0.tgz"
    assert fetch_module.pypi_api_url("Django", "4.2.1", "") == "https://pypi.org/pypi/Django/4.2.1/json"


def base64_sha512(payload: bytes) -> str:
    import base64

    return base64.b64encode(hashlib.sha512(payload).digest()).decode("ascii")
