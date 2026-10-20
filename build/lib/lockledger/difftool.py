"""Offline comparison of two cached artifacts, focused on install hooks."""

import configparser
import json
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

NPM_INSTALL_SCRIPTS = ("preinstall", "install", "postinstall", "prepublish", "preprepare")
PYPI_BUILD_FILES = ("setup.py", "setup.cfg", "pyproject.toml")
SAMPLE_LIMIT = 20


class NotCached(Exception):
    """Raised when an artifact has not been fetched into the cache yet."""

    def __init__(self, command: str, artifact: str):
        super().__init__(f"{artifact} is not in the local cache")
        self.command = command
        self.artifact = artifact


@dataclass
class Artifact:
    path: Path
    ecosystem: str
    names: list[str] = field(default_factory=list)
    content: dict[str, bytes] = field(default_factory=dict)

    def read_text(self, name: str) -> str:
        payload = self.content.get(name)
        return payload.decode("utf-8", errors="replace") if payload is not None else ""


def _strip_common_root(members: list[str]) -> str:
    """npm uses package/ and sdists use name-version/ as a single archive root."""
    if not members or any("/" not in member for member in members):
        return ""
    heads = {member.split("/", 1)[0] for member in members}
    return heads.pop() if len(heads) == 1 else ""


def load_artifact(path: Path, ecosystem: str) -> Artifact:
    """Read a cached .tgz/.tar.gz/.zip/.whl into memory for comparison.

    Artifacts are small and a diff needs them side by side, so both are held
    in memory instead of being unpacked to disk.
    """
    artifact = Artifact(path=path, ecosystem=ecosystem)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            entries = [info for info in archive.infolist() if not info.is_dir()]
            root = _strip_common_root([info.filename for info in entries])
            for info in entries:
                relative = _relative(info.filename, root)
                if relative:
                    artifact.names.append(relative)
                    artifact.content[relative] = archive.read(info)
        return artifact

    with tarfile.open(path, "r:*") as archive:
        members = [member for member in archive.getmembers() if member.isfile()]
        root = _strip_common_root([member.name for member in members])
        for member in members:
            relative = _relative(member.name, root)
            if not relative:
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            artifact.names.append(relative)
            artifact.content[relative] = handle.read()
    return artifact


def _relative(name: str, root: str) -> str:
    if not root:
        return name
    if name.startswith(root + "/"):
        return name[len(root) + 1:]
    return ""


def _find(artifact: Artifact, filename: str) -> str:
    """Locate a manifest, tolerating archives that kept their root directory."""
    if filename in artifact.content:
        return filename
    matches = [name for name in artifact.names if name.endswith("/" + filename)]
    return min(matches, key=lambda name: (name.count("/"), name)) if matches else ""


def _npm_scripts(artifact: Artifact) -> dict[str, str]:
    raw = artifact.read_text(_find(artifact, "package.json"))
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return {}
    return {
        hook: str(scripts[hook])
        for hook in NPM_INSTALL_SCRIPTS
        if hook in scripts and str(scripts[hook]).strip()
    }


def _entry_points(artifact: Artifact) -> str:
    for name in sorted(artifact.names):
        if name.endswith(".dist-info/entry_points.txt"):
            return artifact.read_text(name).strip()
    config = artifact.read_text(_find(artifact, "setup.cfg"))
    if config:
        parser = configparser.ConfigParser()
        try:
            parser.read_string(config)
        except configparser.Error:
            return ""
        if parser.has_section("options.entry_points"):
            return json.dumps(dict(parser["options.entry_points"]), sort_keys=True)
    return ""


def _file_delta(before: Artifact, after: Artifact) -> dict:
    old, new = set(before.names), set(after.names)
    added = sorted(new - old)
    removed = sorted(old - new)
    changed = sorted(path for path in old & new if before.content[path] != after.content[path])
    return {
        "before_files": len(old),
        "after_files": len(new),
        "file_delta": len(new) - len(old),
        "added_count": len(added),
        "removed_count": len(removed),
        "changed_count": len(changed),
        "added": added,
        "removed": removed,
        "changed": changed,
    }


def _npm_diff(before: Artifact, after: Artifact) -> dict:
    old_scripts, new_scripts = _npm_scripts(before), _npm_scripts(after)
    added = sorted(hook for hook in new_scripts if hook not in old_scripts)
    removed = sorted(hook for hook in old_scripts if hook not in new_scripts)
    changed = sorted(
        hook for hook in new_scripts
        if hook in old_scripts and new_scripts[hook] != old_scripts[hook]
    )
    return {
        "install_scripts": {
            "added": [{"hook": hook, "command": new_scripts[hook]} for hook in added],
            "removed": [{"hook": hook, "command": old_scripts[hook]} for hook in removed],
            "changed": [
                {"hook": hook, "before": old_scripts[hook], "after": new_scripts[hook]}
                for hook in changed
            ],
        },
        "flags": _npm_flags(added, removed, changed, new_scripts),
    }


def _npm_flags(added: list[str], removed: list[str], changed: list[str],
               new_scripts: dict[str, str]) -> list[str]:
    flags = []
    for hook in added:
        flags.append(f"NEW install script {hook}: {new_scripts[hook]}")
    for hook in changed:
        flags.append(f"CHANGED install script {hook}")
    for hook in removed:
        flags.append(f"removed install script {hook}")
    return flags


def _pypi_diff(before: Artifact, after: Artifact) -> dict:
    def build_files(artifact: Artifact) -> dict[str, bool]:
        return {name: bool(_find(artifact, name)) for name in PYPI_BUILD_FILES}

    old_build, new_build = build_files(before), build_files(after)
    appeared = [name for name in PYPI_BUILD_FILES if new_build[name] and not old_build[name]]
    vanished = [name for name in PYPI_BUILD_FILES if old_build[name] and not new_build[name]]
    old_points, new_points = _entry_points(before), _entry_points(after)

    flags = [f"NEW build hook file {name}" for name in appeared]
    flags += [f"removed build hook file {name}" for name in vanished]
    if old_points != new_points:
        flags.append("changed entry_points")
    return {
        "build_files": {
            "before": old_build,
            "after": new_build,
            "added": appeared,
            "removed": vanished,
        },
        "entry_points": {
            "before": old_points,
            "after": new_points,
            "changed": old_points != new_points,
        },
        "flags": flags,
    }


def diff_artifacts(before: Artifact, after: Artifact) -> dict:
    delta = _file_delta(before, after)
    body = _npm_diff(before, after) if before.ecosystem == "npm" else _pypi_diff(before, after)
    return {
        "ecosystem": before.ecosystem,
        "from_artifact": str(before.path),
        "to_artifact": str(after.path),
        "files": delta,
        **body,
    }


def compare(lookup, ecosystem: str, name: str, version_a: str, version_b: str) -> dict:
    """lookup(version) -> cached Path, raising NotCached with the cache command."""
    before = load_artifact(lookup(version_a), ecosystem)
    after = load_artifact(lookup(version_b), ecosystem)
    result = diff_artifacts(before, after)
    result.update({"name": name, "from": version_a, "to": version_b})
    return result


def format_report(result: dict, sample_limit: int = SAMPLE_LIMIT) -> str:
    """Human-readable report; every field it prints also exists in --json output."""
    lines = []
    name = result.get("name", "")
    lines.append(f"{name} {result['from']} -> {result['to']} ({result['ecosystem']})")
    files = result["files"]
    lines.append(
        f"  files: {files['before_files']} -> {files['after_files']} "
        f"(delta {files['file_delta']:+d}, +{files['added_count']} / -{files['removed_count']}"
        f" / {files['changed_count']} changed)"
    )
    for label, key in (("added", "added"), ("removed", "removed"), ("changed", "changed")):
        entries = files[key]
        if not entries:
            continue
        shown = entries[:sample_limit]
        lines.append(f"  {label} ({len(entries)}):")
        lines += [f"    {entry}" for entry in shown]
        if len(entries) > len(shown):
            lines.append(f"    ... {len(entries) - len(shown)} more")

    scripts = result.get("install_scripts")
    if scripts:
        total = sum(len(group) for group in scripts.values())
        summary = "none changed" if total == 0 else f"{total} change(s)"
        lines.append(f"  install scripts: {summary}")
        for entry in scripts["added"]:
            lines.append(f"    + {entry['hook']}: {entry['command']}")
        for entry in scripts["changed"]:
            lines.append(f"    ~ {entry['hook']}: {entry['before']}  ->  {entry['after']}")
        for entry in scripts["removed"]:
            lines.append(f"    - {entry['hook']}: {entry['command']}")

    build = result.get("build_files")
    if build:
        lines.append(
            f"  build files added: {', '.join(build['added']) or 'none'}"
            f" / removed: {', '.join(build['removed']) or 'none'}"
        )
        changed_points = "yes" if result["entry_points"]["changed"] else "no"
        lines.append(f"  entry_points changed: {changed_points}")

    if result["flags"]:
        lines.append("  SECURITY:")
        lines += [f"    ! {flag}" for flag in result["flags"]]
    return "\n".join(lines)
