"""Tests for suspects heuristics: typosquats and install-script diffing."""

import json

from conftest import npm_tarball, run_cli, write_npm_lock


def stage(cache_dir, path, ecosystem, name, version):
    directory = cache_dir / ecosystem / name.replace("/", "__") / version
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / path.name
    target.write_bytes(path.read_bytes())
    return target


def add(lock_dir, db, index, packages, label=""):
    path = write_npm_lock(lock_dir, index, packages)
    return run_cli("--db", str(db), "add", str(path), "--label", label)


def test_typosquat_flagged_and_exact_not(tmp_path, locks, db, capsys):
    # Add a snapshot with a typosquat (lodahs is 1 edit from lodash)
    packages = [("lodahs", "1.0.0")]
    add(locks, db, 0, packages)
    capsys.readouterr()
    # Check snapshot 1
    code = run_cli("--db", str(db), "suspects", "--snapshot", "1", "--json")
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert any(t["name"].lower() == "lodahs" for t in result["typosquats"])

    # Exact popular name should NOT be flagged
    packages2 = [("lodash", "4.17.21")]
    add(locks, db, 1, packages2)
    capsys.readouterr()
    code = run_cli("--db", str(db), "suspects", "--snapshot", "2", "--json")
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["typosquats"] == []


def test_suspects_new_changed_install_hooks_flagged(tmp_path, db, cache_dir, capsys):
    # Create two snapshots where a package appears with different versions
    locks = tmp_path / "locks"
    locks.mkdir()
    packages1 = [("event-stream", "3.0.20")]
    packages2 = [("event-stream", "4.0.1")]
    add(locks, db, 0, packages1, label="pre")
    add(locks, db, 1, packages2, label="post")
    capsys.readouterr()

    # Stage cached artifacts
    before = npm_tarball(
        tmp_path / "artifacts" / "event-stream-3.0.20.tgz",
        "event-stream",
        "3.0.20",
        scripts={"test": "echo ok"},
    )
    after = npm_tarball(
        tmp_path / "artifacts" / "event-stream-4.0.1.tgz",
        "event-stream",
        "4.0.1",
        scripts={"test": "echo ok", "preinstall": "node ./install.js"},
    )
    stage(cache_dir, before, "npm", "event-stream", "3.0.20")
    stage(cache_dir, after, "npm", "event-stream", "4.0.1")

    code = run_cli("--db", str(db), "--cache-dir", str(cache_dir), "suspects", "1", "2", "--json")
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["install_script_changes"]
    # New/changed install hooks should be flagged
    assert any("preinstall" in str(x) for x in result["install_script_changes"])
    assert any("NEW install script" in str(x) for x in result["install_script_changes"])


def test_suspects_unchanged_hooks_not_flagged(tmp_path, db, cache_dir, capsys):
    locks = tmp_path / "locks"
    locks.mkdir()
    packages1 = [("left-pad", "1.1.0")]
    packages2 = [("left-pad", "1.2.0")]
    add(locks, db, 0, packages1)
    add(locks, db, 1, packages2)
    capsys.readouterr()

    before = npm_tarball(
        tmp_path / "artifacts" / "left-pad-1.1.0.tgz",
        "left-pad",
        "1.1.0",
        scripts={"postinstall": "echo hi"},
    )
    after = npm_tarball(
        tmp_path / "artifacts" / "left-pad-1.2.0.tgz",
        "left-pad",
        "1.2.0",
        scripts={"postinstall": "echo hi"},
    )
    stage(cache_dir, before, "npm", "left-pad", "1.1.0")
    stage(cache_dir, after, "npm", "left-pad", "1.2.0")

    code = run_cli("--db", str(db), "--cache-dir", str(cache_dir), "suspects", "1", "2", "--json")
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["install_script_changes"] == []
