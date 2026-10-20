"""`lockledger add` and `lockledger when`: snapshotting and history walking."""

import json
import sqlite3

from conftest import RECORDS, npm_lock, run_cli, write_npm_lock


def add(db, lockfile, label=""):
    argv = ["--db", str(db), "add", str(lockfile)]
    if label:
        argv += ["--label", label]
    return run_cli(*argv)


def test_add_records_a_snapshot(tmp_path, db, capsys):
    lock = write_npm_lock(tmp_path, 1, RECORDS)
    assert add(db, lock, "main") == 0
    out = capsys.readouterr().out
    assert "snapshot #1" in out and "(npm)" in out and f"{len(RECORDS)} packages" in out

    row = sqlite3.connect(db).execute(
        "SELECT s.source, s.label, s.format, count(p.name) FROM snapshots s"
        " JOIN packages p ON p.snapshot_id = s.id GROUP BY s.id"
    ).fetchone()
    assert row == (str(lock), "main", "npm", len(RECORDS))


def test_add_stores_the_lockfile_digest(tmp_path, db):
    import hashlib

    lock = write_npm_lock(tmp_path, 1, RECORDS)
    add(db, lock)
    stored = sqlite3.connect(db).execute("SELECT sha256 FROM snapshots").fetchone()[0]
    assert stored == hashlib.sha256(lock.read_bytes()).hexdigest()


def test_add_is_idempotent_for_the_same_file(tmp_path, db, capsys):
    lock = write_npm_lock(tmp_path, 1, RECORDS)
    assert add(db, lock, "before") == 0
    capsys.readouterr()

    assert add(db, lock, "after") == 0
    assert "already snapshotted as #1" in capsys.readouterr().out

    store = sqlite3.connect(db)
    assert store.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1
    assert store.execute("SELECT count(*) FROM packages").fetchone()[0] == len(RECORDS)
    assert store.execute("SELECT label FROM snapshots").fetchone()[0] == "before"


def test_add_idempotency_holds_across_separate_processes(tmp_path, db):
    lock = write_npm_lock(tmp_path, 1, RECORDS)
    assert add(db, lock, "once") == 0
    assert add(db, lock, "twice") == 0
    store = sqlite3.connect(db)
    assert store.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1


def test_add_json_marks_a_duplicate_without_rewriting_it(tmp_path, db, capsys):
    lock = write_npm_lock(tmp_path, 1, RECORDS)
    run_cli("--db", str(db), "add", str(lock), "--label", "main", "--json")
    capsys.readouterr()

    assert run_cli("--db", str(db), "add", str(lock), "--label", "main", "--json") == 0
    [entry] = json.loads(capsys.readouterr().out)["snapshots"]
    assert entry["duplicate"] is True and entry["id"] == 1
    assert entry["packages"] == len(RECORDS)


def test_add_detects_a_changed_lockfile(tmp_path, db, capsys):
    same_name = tmp_path / "package-lock.json"
    same_name.write_text(npm_lock(3, RECORDS))
    assert add(db, same_name, "v1") == 0
    same_name.write_text(npm_lock(3, RECORDS + [("evil-pkg", "9.9.9")]))
    assert add(db, same_name, "v2") == 0
    assert "snapshot #2" in capsys.readouterr().out


def test_add_many_lockfiles_of_different_formats(tmp_path, db, capsys):
    npm = write_npm_lock(tmp_path, 1, RECORDS[:4])
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("requests==2.31.0\nurllib3==2.0.7\n")
    assert run_cli("--db", str(db), "add", str(npm), str(requirements)) == 0
    out = capsys.readouterr().out
    assert "snapshot #1 (npm)" in out and "snapshot #2 (pip)" in out


def test_add_reports_unpinned_requirements_on_stderr(tmp_path, db, capsys):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("requests==2.31.0\nrich\nflask>=2.0\n")
    assert add(db, requirements) == 0
    captured = capsys.readouterr()
    assert "unpinned requirement skipped: rich" in captured.err
    assert "rich" not in captured.out


def test_add_json_output_is_parseable(tmp_path, db, capsys):
    lock = write_npm_lock(tmp_path, 1, RECORDS)
    assert run_cli("--db", str(db), "add", str(lock), "--label", "main", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    [entry] = payload["snapshots"]
    assert entry["id"] == 1 and entry["format"] == "npm"
    assert entry["packages"] == len(RECORDS) and entry["duplicate"] is False


def test_add_rejects_a_file_that_is_not_a_lockfile(tmp_path, db, capsys):
    broken = tmp_path / "package-lock.json"
    broken.write_text("{ not json")
    assert add(db, broken) == 1
    assert "invalid JSON" in capsys.readouterr().err


def test_add_missing_lockfile_is_a_usage_error(tmp_path, db, capsys):
    assert add(db, tmp_path / "absent.json") == 1
    assert "lockfile not found" in capsys.readouterr().err


def test_when_lists_enter_and_replace_events(locks, db, capsys):
    history = [
        ("baseline", [("left-pad", "1.3.0")]),
        ("incident", [("left-pad", "1.3.0"), ("event-stream", "4.0.1")]),
        ("remediation", [("left-pad", "1.3.0"), ("event-stream", "3.0.20")]),
    ]
    for index, (label, packages) in enumerate(history, start=1):
        add(db, write_npm_lock(locks, index, packages), label)
    capsys.readouterr()

    assert run_cli("--db", str(db), "when", "event-stream", "4.0.1") == 0
    out = capsys.readouterr().out
    assert "4.0.1 entered at snapshot #2 (incident)" in out
    assert "4.0.1 replaced by 3.0.20 at snapshot #3 (remediation)" in out

    assert run_cli("--db", str(db), "when", "left-pad") == 0
    out = capsys.readouterr().out
    assert "snapshot #1 (baseline): 1.3.0 entered the tree" in out
    assert "no further changes through snapshot #3 (remediation)" in out


def test_when_table_shows_one_row_per_snapshot(locks, db, capsys):
    for index, packages in enumerate(
        [[("a", "1.0.0")], [("a", "1.1.0")], [("b", "1.0.0")]], start=1
    ):
        add(db, write_npm_lock(locks, index, packages), f"s{index}")
    capsys.readouterr()

    assert run_cli("--db", str(db), "when", "a") == 0
    out = capsys.readouterr().out
    assert "#1" in out and "#2" in out and "#3" in out
    assert "snapshot #1 (s1): 1.0.0 entered the tree" in out
    assert "snapshot #2 (s2): added 1.1.0; removed 1.0.0" in out
    assert "snapshot #3 (s3): removed 1.1.0" in out


def test_when_reports_removal_from_the_tree(locks, db, capsys):
    for index, packages in enumerate(
        [[("evil", "9.9.9")], [("evil", "9.9.9")], [("other", "1.0.0")]], start=1
    ):
        add(db, write_npm_lock(locks, index, packages), f"s{index}")
    capsys.readouterr()

    assert run_cli("--db", str(db), "when", "evil", "9.9.9") == 0
    out = capsys.readouterr().out
    assert "9.9.9 entered at snapshot #1" in out
    assert "dropped from the tree at snapshot #3" in out


def test_when_still_present_in_the_latest_snapshot(locks, db, capsys):
    for index in (1, 2):
        add(db, write_npm_lock(locks, index, [("left-pad", "1.3.0")]), f"s{index}")
    capsys.readouterr()

    assert run_cli("--db", str(db), "when", "left-pad", "1.3.0") == 0
    assert "still present at snapshot #2" in capsys.readouterr().out


def test_when_json_is_machine_readable(locks, db, capsys):
    for index, packages in enumerate(
        [[("left-pad", "1.3.0")], [("left-pad", "1.3.0"), ("event-stream", "4.0.1")]], start=1
    ):
        add(db, write_npm_lock(locks, index, packages), f"s{index}")
    capsys.readouterr()

    assert run_cli("--db", str(db), "when", "event-stream", "4.0.1", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["name"] == "event-stream"
    assert [snapshot["versions"] for snapshot in payload["snapshots"]] == [[], ["4.0.1"]]
    assert [event["type"] for event in payload["events"]] == ["entered", "present"]


def test_when_without_a_version_reports_transitions(locks, db, capsys):
    for index, packages in enumerate(
        [[("a", "1.0.0")], [("a", "1.1.0")], [("a", "1.1.0")]], start=1
    ):
        add(db, write_npm_lock(locks, index, packages))
    capsys.readouterr()

    assert run_cli("--db", str(db), "when", "a", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert [(t["snapshot_id"], t["added"], t["removed"]) for t in payload["transitions"]] == [
        (1, ["1.0.0"], []),
        (2, ["1.1.0"], ["1.0.0"]),
    ]


def test_when_unknown_package_exits_two(locks, db, capsys):
    add(db, write_npm_lock(locks, 1, [("left-pad", "1.3.0")]))
    capsys.readouterr()

    assert run_cli("--db", str(db), "when", "not-there") == 2
    assert "not in the store" in capsys.readouterr().err
    assert run_cli("--db", str(db), "when", "left-pad", "9.9.9") == 2
    assert "never appears in the store" in capsys.readouterr().err


def test_when_shows_a_package_pinned_at_two_versions_at_once(locks, db, capsys):
    """npm nests duplicated subtrees; neither version may be hidden from history."""
    path = locks / "duplicate.json"
    path.write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "root"},
                    "node_modules/ms": {"version": "1.0.0"},
                    "node_modules/a/node_modules/ms": {"version": "2.0.0"},
                },
            }
        )
    )
    add(db, path, "duplicated")
    capsys.readouterr()

    assert run_cli("--db", str(db), "when", "ms", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["snapshots"][0]["versions"] == ["1.0.0", "2.0.0"]

    assert run_cli("--db", str(db), "when", "ms", "2.0.0") == 0
    out = capsys.readouterr().out
    assert "2.0.0 entered at snapshot #1" in out
    assert "still present at snapshot #1" in out


def test_names_match_case_insensitively(locks, db, capsys):
    """npm stores names verbatim; PyPI normalises them, and queries must cope."""
    path = locks / "case.json"
    path.write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "root"},
                    "node_modules/MixedCase": {"version": "1.0.0"},
                },
            }
        )
    )
    add(db, path)
    capsys.readouterr()

    for spelling in ("MixedCase", "mixedcase", "MIXEDCASE"):
        assert run_cli("--db", str(db), "when", spelling) == 0
        assert "snapshot #1: 1.0.0 entered the tree" in capsys.readouterr().out


def test_add_accepts_format_override_for_unusual_filenames(tmp_path, db, capsys):
    odd = tmp_path / "frozen-deps.txt.bak"
    odd.write_text("requests==2.31.0\n")
    assert run_cli("--db", str(db), "add", str(odd), "--format", "pip") == 0
    assert "snapshot #1 (pip)" in capsys.readouterr().out
