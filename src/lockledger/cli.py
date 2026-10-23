"""Command line interface: add, when, cache, diff."""

import argparse
import json
import os
import sys
import tarfile
import zipfile
from pathlib import Path

from . import __version__
from .difftool import NotCached, compare, format_report, load_artifact
from .fetch import FetchError, cached_file, default_cache_dir, fetch
from .models import file_sha256
from .parsers import FORMATS, FormatError, detect_format, parse_lockfile
from .store import Store, default_db_path
from .suspects import diff_install_hooks, find_typosquats_in_names

USAGE_ERROR = 1
NOT_FOUND_ERROR = 2

ECOSYSTEMS = ("npm", "pypi")


class CommandError(Exception):
    def __init__(self, message: str, code: int = USAGE_ERROR):
        super().__init__(message)
        self.code = code


def resolve_db_path(argument: str | None) -> Path:
    if argument:
        return Path(argument).expanduser()
    environment = os.environ.get("LOCKLEDGER_DB")
    if environment:
        return Path(environment).expanduser()
    return default_db_path()


def resolve_cache_dir(argument: str | None) -> Path:
    if argument:
        return Path(argument).expanduser()
    environment = os.environ.get("LOCKLEDGER_CACHE_DIR")
    if environment:
        return Path(environment).expanduser()
    return default_cache_dir()


def parse_spec(spec: str) -> tuple[str, str]:
    """Split "name@version", tolerating scoped npm names like "@scope/pkg@1.2.3"."""
    if spec.startswith("@"):
        parts = spec[1:].split("@", 1)
        if len(parts) != 2 or not parts[1]:
            raise CommandError(f"expected name@version, got '{spec}'")
        return f"@{parts[0]}", parts[1]
    name, separator, version = spec.partition("@")
    if not separator or not version or not name:
        raise CommandError(f"expected name@version, got '{spec}'")
    return name, version


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lockledger",
        description="Offline lockfile supply-chain time machine: snapshot lockfiles,"
                    " ask when a version entered the tree, and diff cached tarballs.",
    )
    parser.add_argument("--version", action="version", version=f"lockledger {__version__}")
    parser.add_argument(
        "--db",
        metavar="PATH",
        help="SQLite store location (default: ./lockledger.db, or $LOCKLEDGER_DB)",
    )
    parser.add_argument(
        "--cache-dir",
        metavar="DIR",
        help="downloaded artifact cache (default: ./lockledger-cache, or $LOCKLEDGER_CACHE_DIR)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    add = subparsers.add_parser("add", help="record a snapshot of one or more lockfiles")
    add.add_argument("lockfiles", nargs="+", metavar="LOCKFILE")
    add.add_argument("--label", default="", help="name for these snapshots, e.g. a branch or date")
    add.add_argument("--format", choices=FORMATS, help="override format detection")
    add.add_argument("--json", action="store_true", help="emit machine-readable output")

    when = subparsers.add_parser(
        "when", help="show when a package version entered and left the dependency tree"
    )
    when.add_argument("name")
    when.add_argument("version", nargs="?", default="", help="track one version instead of all")
    when.add_argument("--json", action="store_true", help="emit machine-readable output")

    cache = subparsers.add_parser("cache", help="download artifact tarballs into the local cache")
    cache.add_argument("specs", nargs="+", metavar="NAME@VERSION")
    cache.add_argument(
        "--ecosystem",
        choices=ECOSYSTEMS,
        help="registry to fetch from when the package is not in the store",
    )
    cache.add_argument(
        "--registry",
        default="",
        metavar="URL",
        help="npm registry base URL, or PyPI JSON-API base URL (default: public registries)",
    )
    cache.add_argument("--json", action="store_true", help="emit machine-readable output")

    diff = subparsers.add_parser(
        "diff", help="compare two cached artifacts (never touches the network)"
    )
    diff.add_argument("name")
    diff.add_argument("version_a", metavar="VERSION_A")
    diff.add_argument("version_b", metavar="VERSION_B")
    diff.add_argument(
        "--ecosystem", choices=ECOSYSTEMS, help="required when the package is not in the store"
    )
    diff.add_argument("--limit", type=int, default=20, help="files shown per added/removed list")
    diff.add_argument("--json", action="store_true", help="emit machine-readable output")

    suspects = subparsers.add_parser(
        "suspects", help="check snapshots for typosquats and install-script changes"
    )
    suspects.add_argument("snapshot_a", nargs="?", help="earlier snapshot id")
    suspects.add_argument("snapshot_b", nargs="?", help="later snapshot id")
    suspects.add_argument("--snapshot", type=int, help="check a single snapshot for typosquats")
    suspects.add_argument(
        "--ecosystem", choices=ECOSYSTEMS, help="required when the package is not in the store"
    )
    suspects.add_argument("--json", action="store_true", help="emit machine-readable output")

    return parser


def command_add(args: argparse.Namespace) -> int:
    entries = []
    with Store(resolve_db_path(args.db)) as store:
        for name in args.lockfiles:
            path = Path(name).expanduser()
            if not path.is_file():
                raise CommandError(f"lockfile not found: {path}")
            notes: list[str] = []
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                lock_format = (args.format or detect_format(path, text)).lower()
                records = parse_lockfile(path, lock_format, notes)
            except (FormatError, ValueError, OSError) as error:
                raise CommandError(f"{path}: {error}") from error
            for note in notes:
                print(f"note: {path}: {note}", file=sys.stderr)

            digest = file_sha256(path)
            source = str(path)
            existing = store.find_snapshot(digest, source)
            if existing:
                entries.append(
                    {
                        "id": existing["id"],
                        "source": source,
                        "sha256": digest,
                        "label": existing["label"],
                        "format": existing["format"],
                        "packages": store.package_count(existing["id"]),
                        "duplicate": True,
                    }
                )
                if not args.json:
                    print(
                        f"{source}: already snapshotted as #{existing['id']} "
                        f"(label '{existing['label'] or '-'}'), unchanged"
                    )
                continue

            snapshot_id = store.add_snapshot(source, digest, args.label, lock_format, records)
            entries.append(
                {
                    "id": snapshot_id,
                    "source": source,
                    "sha256": digest,
                    "label": args.label,
                    "format": lock_format,
                    "packages": len(records),
                    "duplicate": False,
                }
            )
            if not args.json:
                print(
                    f"{source}: snapshot #{snapshot_id} ({lock_format}), "
                    f"{len(records)} packages, label '{args.label or '-'}'"
                )

    if args.json:
        print(json.dumps({"snapshots": entries}, indent=2))
    return 0


def _events(history: list[dict], wanted: str) -> list[dict]:
    """Where `wanted` entered the tree and what replaced it."""
    events: list[dict] = []
    previous = False
    for snapshot in history:
        present = wanted in snapshot["versions"]
        if present and not previous:
            others = [version for version in snapshot["versions"] if version != wanted]
            events.append(
                {
                    "type": "entered",
                    "snapshot_id": snapshot["id"],
                    "label": snapshot["label"],
                    "created_at": snapshot["created_at"],
                    "version": wanted,
                    "replaced_on_entry": others,
                }
            )
        elif previous and not present:
            if snapshot["versions"]:
                events.append(
                    {
                        "type": "replaced",
                        "snapshot_id": snapshot["id"],
                        "label": snapshot["label"],
                        "created_at": snapshot["created_at"],
                        "version": wanted,
                        "by": snapshot["versions"],
                    }
                )
            else:
                events.append(
                    {
                        "type": "removed",
                        "snapshot_id": snapshot["id"],
                        "label": snapshot["label"],
                        "created_at": snapshot["created_at"],
                        "version": wanted,
                    }
                )
        previous = present
    if previous:
        last = history[-1]
        events.append(
            {
                "type": "present",
                "snapshot_id": last["id"],
                "label": last["label"],
                "created_at": last["created_at"],
                "version": wanted,
            }
        )
    return events


def _transitions(history: list[dict]) -> list[dict]:
    """Version set changes across the whole history, for `when <name>`."""
    transitions = []
    previous: list[str] = []
    for snapshot in history:
        current = snapshot["versions"]
        added = sorted(set(current) - set(previous))
        removed = sorted(set(previous) - set(current))
        if added or removed:
            transitions.append(
                {
                    "snapshot_id": snapshot["id"],
                    "label": snapshot["label"],
                    "created_at": snapshot["created_at"],
                    "added": added,
                    "removed": removed,
                    "first_seen": not previous,
                }
            )
        previous = current
    return transitions


def _table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    header_line = "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))
    lines = [header_line.rstrip(), "  ".join("-" * width for width in widths)]
    lines += ["  ".join(cell.ljust(widths[index]).rstrip() for index, cell in enumerate(row))
              for row in rows]
    return "\n".join(lines)


def command_when(args: argparse.Namespace) -> int:
    # Package names are matched case-insensitively, the way both registries do.
    with Store(resolve_db_path(args.db)) as store:
        history = store.history(args.name)
        name = args.name
    if not any(snapshot["versions"] for snapshot in history):
        where = f" {args.version}" if args.version else ""
        raise CommandError(
            f"package '{args.name}{where}' is not in the store;"
            " run `lockledger add <lockfile>` first",
            NOT_FOUND_ERROR,
        )
    if args.version and not any(args.version in snapshot["versions"] for snapshot in history):
        seen = sorted({version for snapshot in history for version in snapshot["versions"]})
        raise CommandError(
            f"version '{args.version}' of '{name}' never appears in the store; seen: "
            + ", ".join(seen),
            NOT_FOUND_ERROR,
        )

    if args.version:
        events = _events(history, args.version)
        payload = {
            "name": name,
            "version": args.version,
            "snapshots": history,
            "events": events,
        }
    else:
        payload = {
            "name": name,
            "version": None,
            "snapshots": history,
            "transitions": _transitions(history),
        }

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    rows = [
        [
            str(snapshot["id"]),
            snapshot["created_at"],
            snapshot["label"] or "-",
            ", ".join(snapshot["versions"]) or "-",
        ]
        for snapshot in history
    ]
    print(_table(rows, ["#", "created_at", "label", "version(s)"]))
    print()
    if args.version:
        for event in payload["events"]:
            location = f"snapshot #{event['snapshot_id']}"
            if event["label"]:
                location += f" ({event['label']})"
            if event["type"] == "entered":
                extra = ""
                if event["replaced_on_entry"]:
                    extra = " (replaced " + ", ".join(event["replaced_on_entry"]) + ")"
                print(f"{event['version']} entered at {location}{extra}")
            elif event["type"] == "replaced":
                print(f"{event['version']} replaced by {', '.join(event['by'])} at {location}")
            elif event["type"] == "removed":
                print(f"{event['version']} dropped from the tree at {location}")
            else:
                print(f"{event['version']} still present at {location}")
    else:
        transitions = payload["transitions"]
        for transition in transitions:
            location = f"snapshot #{transition['snapshot_id']}"
            if transition["label"]:
                location += f" ({transition['label']})"
            parts = []
            if transition["added"]:
                if transition["first_seen"]:
                    parts.append(", ".join(transition["added"]) + " entered the tree")
                else:
                    parts.append("added " + ", ".join(transition["added"]))
            if transition["removed"]:
                parts.append("removed " + ", ".join(transition["removed"]))
            print(f"{location}: " + "; ".join(parts))
        if len(transitions) == 1 and len(history) > 1:
            last = history[-1]
            tail = f"snapshot #{last['id']}"
            if last["label"]:
                tail += f" ({last['label']})"
            print(f"no further changes through {tail}")
    return 0


def _resolve_ecosystem(store: Store, name: str, explicit: str, command: str) -> str:
    if explicit:
        return explicit
    found = store.ecosystems(name)
    if len(found) == 1:
        return found[0]
    if not found and name.startswith("@"):
        return "npm"
    if not found:
        raise CommandError(
            f"'{name}' is not in the store; pass --ecosystem npm|pypi to {command}"
        )
    raise CommandError(
        f"'{name}' appears as {', '.join(found)} in the store; pass --ecosystem to {command}"
    )


def command_cache(args: argparse.Namespace) -> int:
    cache_dir = resolve_cache_dir(args.cache_dir)
    db_path = resolve_db_path(args.db)
    results = []
    with Store(db_path) as store:
        for spec in args.specs:
            name, version = parse_spec(spec)
            name = name.lower() if not name.startswith("@") else name
            ecosystem = _resolve_ecosystem(store, name, args.ecosystem, "cache")
            record = store.record_for(name, version)
            try:
                path, already = fetch(cache_dir, ecosystem, name, version, args.registry, record)
            except FetchError as error:
                raise CommandError(str(error)) from error
            results.append(
                {
                    "name": name,
                    "version": version,
                    "ecosystem": ecosystem,
                    "path": str(path),
                    "already_cached": already,
                }
            )
            if not args.json:
                state = "already cached" if already else "downloaded"
                print(f"{name}@{version} ({ecosystem}): {state} -> {path}")
    if args.json:
        print(json.dumps({"cache_dir": str(cache_dir), "artifacts": results}, indent=2))
    return 0


def command_diff(args: argparse.Namespace) -> int:
    cache_dir = resolve_cache_dir(args.cache_dir)
    name = args.name.lower() if not args.name.startswith("@") else args.name
    with Store(resolve_db_path(args.db)) as store:
        ecosystem = _resolve_ecosystem(store, name, args.ecosystem, "diff")

    def lookup(version: str) -> Path:
        path = cached_file(cache_dir, ecosystem, name, version)
        if path is None:
            raise NotCached(
                f"lockledger cache {name}@{version} --ecosystem {ecosystem}",
                f"{name}@{version}",
            )
        return path

    try:
        result = compare(lookup, ecosystem, name, args.version_a, args.version_b)
    except NotCached as error:
        if args.json:
            print(json.dumps({"error": "not_cached", "artifact": error.artifact,
                              "cache_command": error.command, "diff_performed": False}, indent=2))
        else:
            print(f"{error}: run `{error.command}`", file=sys.stderr)
        return NOT_FOUND_ERROR
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as error:
        raise CommandError(f"{cache_dir}: unreadable cached artifact: {error}") from error

    result["sample_limit"] = args.limit
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print(format_report(result, args.limit))
    return 0


def command_suspects(args: argparse.Namespace) -> int:
    # Option 1: check a single snapshot for typosquats
    if args.snapshot is not None:
        with Store(resolve_db_path(args.db)) as store:
            snapshot = store.snapshot_by_id(args.snapshot)
            if not snapshot:
                raise CommandError(f"snapshot #{args.snapshot} not found")
            # Get all package names from this snapshot
            rows = store.connection.execute(
                "SELECT DISTINCT name FROM packages WHERE snapshot_id = ?",
                (args.snapshot,),
            ).fetchall()
            names = [row[0] for row in rows]
        typos = find_typosquats_in_names(names)
        if args.json:
            print(json.dumps({"snapshot": args.snapshot, "typosquats": typos}, indent=2))
            return 0
        if not typos:
            print(f"snapshot #{args.snapshot}: no typosquat suspects found")
            return 0
        print(f"snapshot #{args.snapshot}: typosquat suspects")
        for t in typos:
            print(f"  ! {t['name']} ~ {t['suspected_target']} (edit distance {t['distance']})")
        return 0

    # Option 2: compare two snapshots - check typosquats in new packages and install script changes
    if not args.snapshot_a or not args.snapshot_b:
        raise CommandError("suspects requires either --snapshot ID or SNAPSHOT_A SNAPSHOT_B")
    try:
        snap_a = int(args.snapshot_a)
        snap_b = int(args.snapshot_b)
    except ValueError:
        raise CommandError("snapshot ids must be integers")
    if snap_a > snap_b:
        snap_a, snap_b = snap_b, snap_a  # compare in order

    cache_dir = resolve_cache_dir(args.cache_dir)
    results = {"snapshot_a": snap_a, "snapshot_b": snap_b, "typosquats": [], "install_script_changes": []}

    with Store(resolve_db_path(args.db)) as store:
        s_a = store.snapshot_by_id(snap_a)
        s_b = store.snapshot_by_id(snap_b)
        if not s_a or not s_b:
            raise CommandError("one or both snapshots not found")
        # Get packages in each snapshot
        rows_a = store.connection.execute(
            "SELECT name, version, ecosystem FROM packages WHERE snapshot_id = ?", (snap_a,)
        ).fetchall()
        rows_b = store.connection.execute(
            "SELECT name, version, ecosystem FROM packages WHERE snapshot_id = ?", (snap_b,)
        ).fetchall()
        pkgs_a = {(r[0].lower(), r[1]): (r[0], r[1], r[2]) for r in rows_a}
        pkgs_b = {(r[0].lower(), r[1]): (r[0], r[1], r[2]) for r in rows_b}
        # New packages (in b but not in a)
        new_pkgs = []
        for key, (name, ver, eco) in pkgs_b.items():
            if key not in pkgs_a:
                new_pkgs.append(name)
        typos = find_typosquats_in_names(new_pkgs)
        results["typosquats"] = typos
        # Hook comparison must key by name, not (name, version): a version bump
        # changes the key, so an intersection of (name, version) pairs never
        # contains the package we actually want to diff.
        by_name_a = {r[0].lower(): (r[0], r[1], r[2]) for r in rows_a}
        by_name_b = {r[0].lower(): (r[0], r[1], r[2]) for r in rows_b}
        for lname in set(by_name_a) & set(by_name_b):
            name_a, ver_a, eco_a = by_name_a[lname]
            name_b, ver_b, eco_b = by_name_b[lname]
            if ver_a == ver_b:
                continue
            name_resolved = name_b.lower() if not name_b.startswith("@") else name_b
            ecosystem = eco_b or eco_a
            path_a = cached_file(cache_dir, ecosystem, name_resolved, ver_a)
            path_b = cached_file(cache_dir, ecosystem, name_resolved, ver_b)
            if path_a and path_b and ecosystem == "npm":
                try:
                    art_a = load_artifact(path_a, ecosystem)
                    art_b = load_artifact(path_b, ecosystem)
                    hook_diff = diff_install_hooks(art_a, art_b)
                    if hook_diff["has_changes"]:
                        results["install_script_changes"].append({
                            "package": name_b,
                            "version_a": ver_a,
                            "version_b": ver_b,
                            "ecosystem": ecosystem,
                            **hook_diff,
                        })
                except Exception:
                    pass  # skip if can't load

    if args.json:
        print(json.dumps(results, indent=2))
        return 0
    if not results["typosquats"] and not results["install_script_changes"]:
        print(f"snapshots #{snap_a} -> #{snap_b}: no suspects found")
        return 0
    print(f"snapshots #{snap_a} -> #{snap_b}: suspects")
    for t in results["typosquats"]:
        print(f"  ! typosquat: {t['name']} ~ {t['suspected_target']} (edit distance {t['distance']})")
    for isc in results["install_script_changes"]:
        print(f"  ! install scripts ({isc['package']} {isc['version_a']} -> {isc['version_b']}):")
        for flag in isc["flags"]:
            print(f"    {flag}")
    return 0


HANDLERS = {
    "add": command_add,
    "when": command_when,
    "cache": command_cache,
    "diff": command_diff,
    "suspects": command_suspects,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_info:
        # argparse signals usage errors with exit code 2, which this tool
        # reserves for "not found"; --help and --version still exit 0.
        return 0 if exit_info.code in (0, None) else USAGE_ERROR

    handler = HANDLERS[args.command]
    try:
        return handler(args)
    except CommandError as error:
        print(f"error: {error}", file=sys.stderr)
        return error.code
    except KeyboardInterrupt:
        return 130
