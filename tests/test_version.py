"""The published version must be one number, not two that can drift."""

import re
from pathlib import Path

import lockledger
from lockledger import cli

PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


def _pyproject_version():
    match = re.search(r'^version\s*=\s*"([^"]+)"', PYPROJECT.read_text(), re.MULTILINE)
    assert match, "no version field in pyproject.toml"
    return match.group(1)


def test_version_sync():
    assert _pyproject_version() == lockledger.__version__


def test_version_flag_reports_package_version(capsys):
    assert cli.main(["--version"]) == 0
    out = capsys.readouterr().out
    assert lockledger.__version__ in out
    assert "lockledger" in out
