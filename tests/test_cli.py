"""Integrity parsing, package-spec parsing and CLI conventions."""

import json

import pytest

from conftest import RECORDS, run_cli, write_npm_lock

from lockledger.cli import parse_spec
from lockledger.integrity import check, parse
from lockledger.models import safe_component


def test_parse_spec_plain_and_scoped():
    assert parse_spec("left-pad@1.3.0") == ("left-pad", "1.3.0")
    assert parse_spec("@scope/pkg@2.0.0") == ("@scope/pkg", "2.0.0")
    assert parse_spec("pkg@0.1.0-dev.4") == ("pkg", "0.1.0-dev.4")


def test_parse_spec_rejects_missing_version():
    from lockledger.cli import CommandError

    with pytest.raises(CommandError):
        parse_spec("noversion")
    with pytest.raises(CommandError):
        parse_spec("@scope/noversion")
    with pytest.raises(CommandError):
        parse_spec("@scope/pkg@")


def test_integrity_parses_npm_and_pip_spellings():
    assert parse("sha512-ABCdef==") == [("sha512", "ABCdef==")]
    assert parse("sha256=deadbeef") == [("sha256", "deadbeef")]
    assert parse("sha256:deadbeef") == [("sha256", "deadbeef")]
    assert parse("sha256=aa;sha512=bb") == [("sha256", "aa"), ("sha512", "bb")]
    assert parse("md5=nope") == []


def test_integrity_check_accepts_hex_and_base64():
    import base64
    import hashlib

    payload = b"lockledger"
    hex_digest = hashlib.sha256(payload).hexdigest()
    b64_digest = base64.b64encode(hashlib.sha512(payload).digest()).decode("ascii")

    assert check(payload, f"sha256={hex_digest}")[0] is True
    assert check(payload, f"sha512-{b64_digest}")[0] is True
    assert check(payload, "sha256=00000000")[0] is False
    ok, note = check(payload, "md5=whatever")
    assert ok is True and "skipped" in note
    assert check(payload, "") == (True, "")


def test_safe_filename_blocks_path_traversal():
    from lockledger.models import safe_filename

    assert safe_filename("left-pad-1.3.0.tgz", "fallback") == "left-pad-1.3.0.tgz"
    assert safe_filename("https://x/files/a.tgz", "fallback") == "a.tgz"
    assert safe_filename("../../evil.py", "fallback") == "evil.py"
    assert safe_filename("..", "fallback") == "fallback"
    assert safe_filename("", "fallback") == "fallback"
    assert safe_filename("weird name.py", "fallback") == "fallback"


def test_cache_paths_are_flattened_but_reversible():
    assert safe_component("@scope/pkg") == "@scope__pkg"
    assert safe_component("weird/../name") == "weird__..__name"
    assert safe_component("left-pad") == "left-pad"


def test_json_output_is_a_single_document(db, locks, capsys):
    """Human output is plain text; --json output is a single JSON document."""
    lock = write_npm_lock(locks, 1, RECORDS)
    assert run_cli("--db", str(db), "add", str(lock), "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"snapshots"}


def test_help_is_available_for_every_command(capsys):
    for argv in (
        ["--help"], ["add", "--help"], ["when", "--help"],
        ["cache", "--help"], ["diff", "--help"],
    ):
        assert run_cli(*argv) == 0
        assert "usage: lockledger" in capsys.readouterr().out


def test_argument_errors_use_exit_code_one(capsys):
    assert run_cli() == 1
    assert "required" in capsys.readouterr().err.lower()
    assert run_cli("--db", "x", "when") == 1
    assert "the following arguments are required" in capsys.readouterr().err
    assert run_cli("--db", "x", "add", "--format", "cargo", "f.json") == 1


def test_database_and_cache_defaults_are_documented_and_local(tmp_path, monkeypatch):
    from lockledger.fetch import default_cache_dir
    from lockledger.store import default_db_path

    monkeypatch.chdir(tmp_path)
    assert default_db_path() == tmp_path / "lockledger.db"
    assert default_cache_dir() == tmp_path / "lockledger-cache"


def test_environment_variables_override_the_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCKLEDGER_DB", str(tmp_path / "custom.db"))
    monkeypatch.setenv("LOCKLEDGER_CACHE_DIR", str(tmp_path / "custom-cache"))

    from lockledger.cli import resolve_cache_dir, resolve_db_path

    assert resolve_db_path(None) == tmp_path / "custom.db"
    assert resolve_cache_dir(None) == tmp_path / "custom-cache"
    assert resolve_db_path(str(tmp_path / "flag.db")) == tmp_path / "flag.db"


def test_add_creates_the_store_on_demand(tmp_path, locks, capsys):
    database = tmp_path / "deep" / "nested" / "lockledger.db"
    lock = write_npm_lock(locks, 1, RECORDS)
    assert run_cli("--db", str(database), "add", str(lock)) == 0
    assert database.is_file()
