"""npm package-lock.json v1/v2/v3 parser."""

import json

from ..models import PackageRecord


def _split_key(key: str) -> str:
    """Turn a `packages` map key into the package name it installs."""
    if not key:
        return ""
    parts = key.split("/")
    segments = [index for index, part in enumerate(parts) if part == "node_modules"]
    if not segments:
        return ""  # the root entry and bare workspace folders install nothing
    # npm installs the deepest copy, so the leaf is whatever follows the last
    # node_modules segment; joining keeps a scoped leaf's "@scope" attached.
    # A workspace nests its own node_modules deeper still, so an installed
    # package is not necessarily the first segment of the key.
    return "/".join(parts[segments[-1] + 1:])


def _string_field(entry: dict, field: str) -> str:
    """Return an optional string field, refusing non-string values."""
    value = entry.get(field)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"package entry field '{field}' must be a string")
    return value


def parse_npm_lock(text: str) -> list[PackageRecord]:
    """Every pinned package in a package-lock.json, v1 through v3."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSON: {error}") from error
    if not isinstance(data, dict) or "lockfileVersion" not in data:
        raise ValueError("not a package-lock.json: missing lockfileVersion")

    packages = data.get("packages")
    records: list[PackageRecord] = []

    if isinstance(packages, dict):
        for key, entry in packages.items():
            if not isinstance(entry, dict) or not key:
                continue  # the "" key describes the workspace root
            name = entry.get("name") or _split_key(key)
            pkg_version = entry.get("version")
            if not name or not pkg_version:
                continue
            link = entry.get("link")
            if link is True or (isinstance(link, str) and link.startswith("file:")):
                continue  # workspace links have no published artifact
            if str(pkg_version).startswith("link:"):
                continue
            resolved = _string_field(entry, "resolved")
            if resolved.startswith("file:"):
                continue
            integrity = _string_field(entry, "integrity")
            records.append(
                PackageRecord(
                    name=name,
                    version=str(pkg_version),
                    ecosystem="npm",
                    resolved_url=resolved,
                    integrity=integrity,
                )
            )

    dependencies = data.get("dependencies")
    if isinstance(dependencies, dict):
        # v2 writes both sections; the same package ends up listed twice and
        # the dedupe below collapses it.
        records.extend(_legacy_dependencies(dependencies))

    deduped: dict[tuple[str, str], PackageRecord] = {}
    for record in records:
        deduped.setdefault((record.name, record.version), record)
    return list(deduped.values())


def _legacy_dependencies(dependencies: dict) -> list[PackageRecord]:
    """Flatten the v1 nested `dependencies` tree."""
    if not isinstance(dependencies, dict):
        raise ValueError("dependencies must be an object")
    found: list[PackageRecord] = []
    for name, entry in dependencies.items():
        if not isinstance(entry, dict):
            continue
        pkg_version = entry.get("version")
        resolved = _string_field(entry, "resolved")
        integrity = _string_field(entry, "integrity")
        if pkg_version and not str(pkg_version).startswith("git+") and resolved:
            found.append(
                PackageRecord(
                    name=name,
                    version=str(pkg_version),
                    ecosystem="npm",
                    resolved_url=resolved,
                    integrity=integrity,
                )
            )
        nested = entry.get("dependencies")
        if nested is None:
            nested = {}
        found.extend(_legacy_dependencies(nested))
    return found
