"""The full investigation flow, on a realistically sized dependency tree."""

import json
import sqlite3
import subprocess
import sys

from conftest import run_cli, npm_tarball, write_npm_lock

# A tree of the size a small service actually carries.
TREE = [
    ("@babel/code-frame", "7.24.7"), ("@babel/generator", "7.24.10"),
    ("@babel/helper-globals", "7.24.7"), ("@babel/parser", "7.24.7"),
    ("@babel/template", "7.24.7"), ("@babel/traverse", "7.24.7"),
    ("@babel/types", "7.24.7"), ("@jridgewell/gen-mapping", "0.3.5"),
    ("@jridgewell/set-array", "1.2.1"), ("@jridgewell/sourcemap-codec", "1.5.0"),
    ("@jridgewell/trace-mapping", "0.3.25"), ("@tootallnate/once", "2.0.0"),
    ("abbrev", "1.1.1"), ("acorn", "8.12.1"), ("agent-base", "6.0.2"),
    ("ansi-regex", "5.0.1"), ("ansi-styles", "4.3.0"), ("anymatch", "3.1.3"),
    ("argparse", "1.0.10"), ("array-flatten", "1.1.1"), ("asap", "2.0.6"),
    ("async", "3.2.5"), ("asynckit", "0.4.0"), ("babel-jest", "29.7.0"),
    ("balanced-match", "1.0.2"), ("base64-js", "1.5.1"), ("binary-extensions", "2.3.0"),
    ("bl", "4.1.0"), ("brace-expansion", "1.1.11"), ("braces", "3.0.3"),
    ("browserslist", "4.23.2"), ("buffer", "5.7.1"), ("busboy", "1.6.0"),
    ("bytes", "3.1.2"), ("call-bind", "1.0.7"), ("callsites", "3.1.0"),
    ("camelcase", "5.3.1"), ("caniuse-lite", "1.0.30001642"), ("chalk", "4.1.2"),
    ("chokidar", "3.6.0"), ("ci-info", "3.9.0"), ("cliui", "8.0.1"),
    ("color-convert", "2.0.1"), ("color-name", "1.1.4"), ("combined-stream", "1.0.8"),
    ("commander", "2.20.3"), ("concat-map", "0.0.1"), ("content-disposition", "0.5.4"),
    ("convert-source-map", "2.0.0"), ("cookie", "0.6.0"), ("debug", "4.3.5"),
    ("dedent", "1.5.3"), ("deepmerge", "4.3.1"), ("delayed-stream", "1.0.0"),
    ("ee-first", "1.1.1"), ("electron-to-chromium", "1.4.827"), ("emoji-regex", "8.0.0"),
    ("escape-html", "1.0.3"), ("escape-string-regexp", "2.0.0"), ("esprima", "4.0.1"),
]


def test_investigate_a_compromised_version(tmp_path, db, locks, cache_dir, capsys):
    """add -> add -> add, then when -> cache -> diff, fully offline."""
    baseline = [entry for entry in TREE if entry[0] != "event-stream"]
    write_npm_lock(locks, 1, baseline)
    incident = baseline + [("event-stream", "4.0.1")]
    write_npm_lock(locks, 2, incident)
    remediation = [
        ("event-stream", "3.0.20") if name == "event-stream" else (name, version)
        for name, version in incident
    ]
    write_npm_lock(locks, 3, remediation)

    for index, label in ((1, "pre-incident"), (2, "incident"), (3, "pinned-back")):
        lock = locks / f"package-lock-{index}.json"
        assert run_cli("--db", str(db), "--cache-dir", str(cache_dir),
                       "add", str(lock), "--label", label) == 0
    capsys.readouterr()

    assert run_cli("--db", str(db), "add", str(locks / "package-lock-2.json"),
                   "--label", "incident") == 0
    assert "already snapshotted as #2" in capsys.readouterr().out

    assert run_cli("--db", str(db), "when", "event-stream", "4.0.1") == 0
    history = capsys.readouterr().out
    assert "4.0.1 entered at snapshot #2 (incident)" in history
    assert "4.0.1 replaced by 3.0.20 at snapshot #3 (pinned-back)" in history

    stored = sqlite3.connect(db).execute("SELECT count(*) FROM packages").fetchone()[0]
    assert stored == len(baseline) + len(incident) + len(remediation)


def test_diff_between_two_versions_of_the_same_package(tmp_path, db, locks, cache_dir, capsys):
    clean = npm_tarball(tmp_path / "safe-1.0.0.tgz", "safe", "1.0.0",
                        scripts={"build": "tsc"}, files={"index.js": "module.exports = {};\n"})
    trojan = npm_tarball(
        tmp_path / "safe-1.0.1.tgz", "safe", "1.0.1",
        scripts={"build": "tsc", "prepublish": "node ./obfuscate.js",
                 "postinstall": "node ./exfil.js"},
        files={
            "index.js": "module.exports = {};\n",
            "obfuscate.js": "// packed payload\n",
            "exfil.js": "// reads ~/.ssh\n",
        },
    )
    for directory, artifact, version in (
        (cache_dir / "npm" / "safe" / "1.0.0", clean, "1.0.0"),
        (cache_dir / "npm" / "safe" / "1.0.1", trojan, "1.0.1"),
    ):
        directory.mkdir(parents=True)
        (directory / artifact.name).write_bytes(artifact.read_bytes())

    assert run_cli("--db", str(db), "--cache-dir", str(cache_dir),
                   "diff", "safe", "1.0.0", "1.0.1", "--ecosystem", "npm", "--json") == 0
    result = json.loads(capsys.readouterr().out)
    hooks = {entry["hook"] for entry in result["install_scripts"]["added"]}
    assert hooks == {"postinstall", "prepublish"}
    assert sorted(result["files"]["added"]) == ["exfil.js", "obfuscate.js"]
    assert len(result["flags"]) == 2


def test_console_script_runs(tmp_path, locks, db):
    """The installed entry point works, not just the in-process call."""
    lock = write_npm_lock(locks, 1, [("left-pad", "1.3.0")])
    completed = subprocess.run(
        [sys.executable, "-m", "lockledger", "--db", str(db), "add", str(lock)],
        capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "snapshot #1 (npm)" in completed.stdout
