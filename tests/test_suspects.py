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


def write_pip_lock(directory, index, packages):
    """Write a requirements.txt style lockfile for pypi packages."""
    path = directory / f"requirements-{index}.txt"
    lines = [f"{name}=={version}" for name, version in packages]
    path.write_text("\n".join(lines) + "\n")
    return path


def add_pip(lock_dir, db, index, packages, label=""):
    path = write_pip_lock(lock_dir, index, packages)
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


def test_suspects_ecosystem_filter_single_snapshot(tmp_path, locks, db, capsys):
    """--ecosystem filters typosquats in a single snapshot to that ecosystem."""
    # Snapshot 1: npm packages with a typosquat (lodahs ~ lodash)
    add(locks, db, 0, [("lodahs", "1.0.0"), ("lodash", "4.17.21")])
    # Snapshot 2: pypi packages with a typosquat (requestz ~ requests)
    add_pip(locks, db, 1, [("requestz", "1.0.0"), ("requests", "2.31.0")])
    capsys.readouterr()

    # No filter: both typosquats appear in their respective snapshots
    code = run_cli("--db", str(db), "suspects", "--snapshot", "1", "--json")
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert any(t["name"].lower() == "lodahs" for t in result["typosquats"])

    code = run_cli("--db", str(db), "suspects", "--snapshot", "2", "--json")
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert any(t["name"].lower() == "requestz" for t in result["typosquats"])

    # Filter to npm on snapshot 1: lodahs should appear
    code = run_cli("--db", str(db), "suspects", "--snapshot", "1", "--ecosystem", "npm", "--json")
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert any(t["name"].lower() == "lodahs" for t in result["typosquats"])

    # Filter to pypi on snapshot 1: should be empty (no pypi packages in snapshot 1)
    code = run_cli("--db", str(db), "suspects", "--snapshot", "1", "--ecosystem", "pypi", "--json")
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["typosquats"] == []

    # Filter to npm on snapshot 2: should be empty (no npm packages in snapshot 2)
    code = run_cli("--db", str(db), "suspects", "--snapshot", "2", "--ecosystem", "npm", "--json")
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["typosquats"] == []

    # Filter to pypi on snapshot 2: requestz should appear
    code = run_cli("--db", str(db), "suspects", "--snapshot", "2", "--ecosystem", "pypi", "--json")
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert any(t["name"].lower() == "requestz" for t in result["typosquats"])


def test_suspects_ecosystem_filter_two_snapshots(tmp_path, locks, db, capsys):
    """--ecosystem filters new-package typosquats when comparing two snapshots."""
    # Snapshot 1: npm packages
    add(locks, db, 0, [("lodash", "4.17.21")])
    # Snapshot 2: npm packages + new npm typosquat (expresss ~ express)
    add(locks, db, 1, [("lodash", "4.17.21"), ("expresss", "1.0.0")])
    # Snapshot 3: pypi packages + new pypi typosquat (requestz ~ requests)
    add_pip(locks, db, 2, [("requests", "2.31.0"), ("requestz", "1.0.0")])
    capsys.readouterr()

    # Compare 1->2 (both npm): expresss should appear
    code = run_cli("--db", str(db), "suspects", "1", "2", "--json")
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert any(t["name"].lower() == "expresss" for t in result["typosquats"])

    # Compare 1->2 with --ecosystem npm: expresss should appear
    code = run_cli("--db", str(db), "suspects", "1", "2", "--ecosystem", "npm", "--json")
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert any(t["name"].lower() == "expresss" for t in result["typosquats"])

    # Compare 1->2 with --ecosystem pypi: should be empty
    code = run_cli("--db", str(db), "suspects", "1", "2", "--ecosystem", "pypi", "--json")
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["typosquats"] == []

    # Compare 2->3 (npm -> pypi) without filter: requestz appears (new in 3)
    code = run_cli("--db", str(db), "suspects", "2", "3", "--json")
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert any(t["name"].lower() == "requestz" for t in result["typosquats"])

    # Compare 2->3 with --ecosystem npm: should be empty (no new npm packages in 3)
    code = run_cli("--db", str(db), "suspects", "2", "3", "--ecosystem", "npm", "--json")
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["typosquats"] == []

    # Compare 2->3 with --ecosystem pypi: requestz should appear
    code = run_cli("--db", str(db), "suspects", "2", "3", "--ecosystem", "pypi", "--json")
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert any(t["name"].lower() == "requestz" for t in result["typosquats"])
