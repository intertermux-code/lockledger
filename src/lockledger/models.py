"""Data shared between parsers, the store and the diff engine."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PackageRecord:
    name: str
    version: str
    ecosystem: str
    resolved_url: str = ""
    integrity: str = ""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9@._+-]+")


def safe_component(value: str) -> str:
    """Keep names usable as path components (@scope/name is common)."""
    return SAFE_NAME_RE.sub("_", value.replace("/", "__"))


FILENAME_RE = re.compile(r"^[A-Za-z0-9@._+-]+$")


def safe_filename(value: str, fallback: str) -> str:
    """Reduce a registry-supplied name to a plain file name.

    A mirror's response is untrusted input: separators are dropped, and a name
    that is still not usable as a file becomes the caller's fallback.
    """
    candidate = value.rstrip("/").split("/")[-1]
    if candidate and candidate not in (".", "..") and FILENAME_RE.match(candidate):
        return candidate
    return fallback
