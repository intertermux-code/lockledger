"""npm package-lock.json v1/v2/v3 parser."""

import json

from ..models import PackageRecord


def _split_key(key: str) -> str:
    """Turn a `packages` map key into the package name it installs."""
    if not key or not key.startswith("node_modules/"):
        return ""
    tail = key[len("node_modules/"):]
    if tail.startswith("@"):
        head, _, rest = tail.partition("/")
        return f"{head}/{rest.split('/node_modules/')[0]}" if rest else head
    return tail.rsplit("/node_modules/", 1)[-1]


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
            if str(entry.get("link", "")).startswith("file:"):
                continue  # workspace links have no published artifact
            if str(pkg_version).startswith("link:"):
                continue
            resolved = entry.get("resolved") or ""
            if resolved.startswith("file:"):
                continue
            records.append(
                PackageRecord(
                    name=name,
                    version=str(pkg_version),
                    ecosystem="npm",
                    resolved_url=resolved,
                    integrity=entry.get("integrity") or "",
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
    found: list[PackageRecord] = []
    for name, entry in dependencies.items():
        if not isinstance(entry, dict):
            continue
        pkg_version = entry.get("version")
        resolved = entry.get("resolved") or ""
        if pkg_version and not str(pkg_version).startswith("git+") and resolved:
            found.append(
                PackageRecord(
                    name=name,
                    version=str(pkg_version),
                    ecosystem="npm",
                    resolved_url=resolved,
                    integrity=entry.get("integrity") or "",
                )
            )
        found.extend(_legacy_dependencies(entry.get("dependencies") or {}))
    return found
