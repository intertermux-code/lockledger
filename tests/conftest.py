"""Shared fixtures: sample lockfiles, artifact builders and a CLI runner.

Network access is refused for every test: analysis-time code must work fully
offline, and download behaviour is covered with an injected fake transport.
"""

import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from lockledger.cli import main

RECORDS = [
    ("left-pad", "1.3.0"),
    ("event-stream", "4.0.1"),
    ("map-stream", "0.1.0"),
    ("duplexer", "0.1.2"),
    ("pause-stream", "0.0.11"),
    ("split", "1.0.1"),
    ("through", "2.3.8"),
    ("from", "0.1.7"),
    ("mime-types", "2.1.35"),
    ("mime-db", "1.52.0"),
]


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    import urllib.request

    def refuse(*args, **kwargs):
        raise AssertionError("lockledger touched the network during a test")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)


def run_cli(*argv) -> int:
    return main(list(argv))


@pytest.fixture
def db(tmp_path):
    return tmp_path / "state" / "lockledger.db"


@pytest.fixture
def cache_dir(tmp_path):
    return tmp_path / "state" / "lockledger-cache"


@pytest.fixture
def locks(tmp_path):
    directory = tmp_path / "locks"
    directory.mkdir()
    return directory


def npm_lock(version: int, packages: list[tuple[str, str]]) -> str:
    entries = {"": {"name": "app", "version": "1.0.0", "license": "MIT"}}
    for name, release in packages:
        entries[f"node_modules/{name}"] = {
            "version": release,
            "resolved": f"https://registry.npmjs.org/{name}/-/{name}-{release}.tgz",
        }
    return json.dumps(
        {
            "name": "app",
            "version": "1.0.0",
            "lockfileVersion": version,
            "requires": True,
            "packages": entries,
        },
        indent=2,
    )


def write_npm_lock(directory: Path, index, packages, version: int = 3) -> Path:
    """One lockfile per snapshot index, so each add sees a distinct sha256."""
    path = directory / f"package-lock-{index}.json"
    path.write_text(npm_lock(version, packages))
    return path


def npm_tarball(path: Path, name: str, version: str, scripts: dict | None = None,
                files: dict | None = None) -> Path:
    """Hand-built npm tarball: everything lives under the conventional package/ prefix."""
    manifest = {"name": name, "version": version, "scripts": scripts or {}}
    members = {"package/package.json": json.dumps(manifest, indent=2)}
    members.update({f"package/{key}": value for key, value in (files or {}).items()})

    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as archive:
        for member_name, content in sorted(members.items()):
            payload = content.encode("utf-8")
            info = tarfile.TarInfo(member_name)
            info.size = len(payload)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(payload))
    return path


def pypi_wheel(path: Path, name: str, version: str, entry_points: str = "",
               files: dict | None = None) -> Path:
    distribution = f"{name.replace('-', '_')}-{version}"
    members = {
        f"{distribution}/__init__.py": "",
        f"{distribution}/core.py": "VALUE = 1\n",
        f"{distribution}.dist-info/METADATA": f"Name: {name}\nVersion: {version}\n",
        f"{distribution}.dist-info/RECORD": "",
    }
    if entry_points:
        members[f"{distribution}.dist-info/entry_points.txt"] = entry_points
    members.update(files or {})

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for member_name, content in sorted(members.items()):
            archive.writestr(member_name, content)
    return path


def pypi_sdist(path: Path, name: str, version: str, setup_cfg: str = "",
               files: dict | None = None) -> Path:
    root = f"{name}-{version}"
    members = {f"{root}/pyproject.toml": "[build-system]\nrequires = ['setuptools']\n"}
    if setup_cfg:
        members[f"{root}/setup.cfg"] = setup_cfg
    else:
        members[f"{root}/setup.py"] = f"from setuptools import setup\nsetup(name='{name}')\n"
    members.update({f"{root}/{key}": value for key, value in (files or {}).items()})

    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as archive:
        for member_name, content in sorted(members.items()):
            payload = content.encode("utf-8")
            info = tarfile.TarInfo(member_name)
            info.size = len(payload)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(payload))
    return path
