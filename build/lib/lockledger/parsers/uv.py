"""uv.lock parser."""

import re

from ..models import PackageRecord

try:
    import tomllib
except ModuleNotFoundError:  # tomllib landed in the 3.11 standard library
    tomllib = None

REMOTE_RE = re.compile(r"https?://|git\+")


def _artifact(entry: dict) -> tuple[str, str]:
    """First downloadable URL for a package plus its recorded hash."""
    wheels = entry.get("wheels")
    if isinstance(wheels, list) and wheels:
        first = wheels[0]
        if isinstance(first, dict):
            return str(first.get("url", "")), str(first.get("hash", ""))
    sdist = entry.get("sdist")
    if isinstance(sdist, dict):
        return str(sdist.get("url", "")), str(sdist.get("hash", ""))
    return "", ""


def _source_url(source: object) -> str:
    if not isinstance(source, dict):
        return ""
    for key in ("registry", "git", "url", "path", "editable", "virtual"):
        value = source.get(key)
        if isinstance(value, dict):
            return str(value.get("url") or value.get("path") or key)
        if isinstance(value, str):
            return value if key in ("path", "editable", "virtual") else str(value)
    return ""


def parse_uv_lock(data: bytes, notes: list[str] | None = None) -> list[PackageRecord]:
    if tomllib is None:
        raise ValueError("reading uv.lock needs Python 3.11 or newer (stdlib tomllib)")
    try:
        parsed = tomllib.loads(data.decode("utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"invalid TOML: {error}") from error

    packages = parsed.get("package")
    if not isinstance(packages, list):
        raise ValueError("not a uv.lock: no [[package]] entries")

    records: list[PackageRecord] = []
    for entry in packages:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        version = entry.get("version")
        if not name or not version:
            if notes is not None and name:
                notes.append(f"skipped entry without a pinned version: {name}")
            continue
        source = _source_url(entry.get("source"))
        url, hash_value = _artifact(entry)
        if not url and not REMOTE_RE.search(source):
            # The root project and workspace members resolve to local
            # checkouts, which have no artifact to time-travel over.
            if notes is not None:
                notes.append(f"skipped local package: {name}")
            continue
        records.append(
            PackageRecord(
                name=str(name).lower(),
                version=str(version),
                ecosystem="pypi",
                resolved_url=url or source,
                integrity=hash_value.replace(":", "=", 1) if hash_value else "",
            )
        )
    return records
