"""Tarball fetching into a local cache. Network use happens only here."""

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .integrity import check as check_integrity
from .models import PackageRecord, safe_component, safe_filename

DEFAULT_NPM_REGISTRY = "https://registry.npmjs.org"
DEFAULT_PYPI_INDEX = "https://pypi.org/pypi"
USER_AGENT = "lockledger/0.1 (offline lockfile forensics)"

NAME_RE = re.compile(r"^(@[A-Za-z0-9._-]+/[A-Za-z0-9._-]+|[A-Za-z0-9._-]+)$")
ARTIFACT_SUFFIXES = (".tgz", ".tar.gz", ".whl", ".zip")
# Prefer the source distribution: install hooks live there.
SUFFIX_RANK = (".tar.gz", ".tgz", ".zip", ".whl")


class FetchError(RuntimeError):
    pass


def default_cache_dir() -> Path:
    return Path.cwd() / "lockledger-cache"


def package_dir(cache_dir: Path, ecosystem: str, name: str, version: str) -> Path:
    return cache_dir / ecosystem / safe_component(name) / safe_component(version)


def cached_file(cache_dir: Path, ecosystem: str, name: str, version: str) -> Path | None:
    directory = package_dir(cache_dir, ecosystem, name, version)
    if not directory.is_dir():
        return None
    candidates = sorted(
        path for path in directory.iterdir() if path.name.endswith(ARTIFACT_SUFFIXES)
    )
    return candidates[0] if candidates else None


def artifact_url(ecosystem: str, name: str, version: str, registry: str) -> str:
    if ecosystem == "npm":
        base = registry or DEFAULT_NPM_REGISTRY
        bare = name.split("/")[-1] if name.startswith("@") else name
        return f"{base.rstrip('/')}/{name}/-/{bare}-{version}.tgz"
    return ""


def pypi_api_url(name: str, version: str, registry: str) -> str:
    base = registry or DEFAULT_PYPI_INDEX
    return f"{base.rstrip('/')}/{name}/{version}/json"


def _get(url: str, timeout: float = 60.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise FetchError(f"HTTP {error.code} from {url}") from error
    except urllib.error.URLError as error:
        raise FetchError(f"could not reach {url}: {error.reason}") from error
    except OSError as error:
        raise FetchError(f"could not read from {url}: {error}") from error


def _select_pypi_artifact(payload: bytes, name: str, version: str) -> tuple[str, str]:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as error:
        raise FetchError(
            f"registry returned invalid JSON for {name}=={version}: {error}"
        ) from error

    def rank(entry: dict) -> tuple:
        filename = entry.get("filename", "")
        index = next(
            (position for position, suffix in enumerate(SUFFIX_RANK) if filename.endswith(suffix)),
            len(SUFFIX_RANK),
        )
        return (index, filename)

    files = sorted((entry for entry in data.get("urls") or [] if entry.get("url")), key=rank)
    if not files:
        raise FetchError(f"no downloadable artifact listed for {name}=={version}")
    chosen = files[0]
    listed = chosen.get("filename") or chosen["url"]
    return chosen["url"], _artifact_name(listed, f"{safe_component(name)}-{version}.tar.gz")


def _artifact_name(value: str, fallback: str) -> str:
    """A cached artifact must still look like one, whatever the registry said."""
    candidate = safe_filename(value, fallback)
    return candidate if candidate.endswith(ARTIFACT_SUFFIXES) else fallback


def fetch(cache_dir: Path, ecosystem: str, name: str, version: str, registry: str = "",
          record: PackageRecord | None = None) -> tuple[Path, bool]:
    """Download one artifact into the cache. Returns (path, already_cached)."""
    if not NAME_RE.match(name):
        raise FetchError(f"refusing to fetch unsafe package name: {name}")

    existing = cached_file(cache_dir, ecosystem, name, version)
    if existing:
        return existing, True

    if ecosystem == "npm":
        url = (record.resolved_url if record and record.resolved_url else "") or artifact_url(
            "npm", name, version, registry
        )
        payload = _get(url)
        filename = _artifact_name(url, f"{safe_component(name)}-{version}.tgz")
    elif ecosystem == "pypi":
        api = pypi_api_url(name, version, registry)
        url, filename = _select_pypi_artifact(_get(api), name, version)
        payload = _get(url)
    else:
        raise FetchError(
            f"{name}@{version}: unsupported ecosystem '{ecosystem}', expected npm or pypi"
        )

    if record and record.integrity:
        ok, note = check_integrity(payload, record.integrity)
        if not ok:
            raise FetchError(f"{name}=={version}: {note}")
        if note:
            print(f"note: {name}@{version}: {note}", file=sys.stderr)

    directory = package_dir(cache_dir, ecosystem, name, version)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / filename
    destination.write_bytes(payload)
    (directory / "origin.txt").write_text(f"{url}\n", encoding="utf-8")
    return destination, False
