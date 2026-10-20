# lockledger

An offline lockfile supply-chain time machine.

When an incident hits — the npm Shai-Hulud worms, the TanStack compromise, any
"package X version Y was malicious" advisory — the questions are always the
same:

1. Which of my trees ever contained that version?
2. When did it get in, and when did it get out?
3. What did that release actually change — specifically, did it start running
   code at install time?

`lockledger` answers those from a local SQLite store and a local tarball
cache. It parses lockfiles, records snapshots, and diffs artifacts. Analysis
never touches the network: the only online command is `lockledger cache`,
which downloads public registry tarballs once. Nothing is ever uploaded.

Stdlib Python only. No dependencies, no account, no vendor cloud.

## Install

From a checkout:

```sh
python -m venv .venv && .venv/bin/pip install -e .
.venv/bin/lockledger --help
```

Or install it anywhere with `pipx install lockledger` / `pip install lockledger`
once the package is published to PyPI. Requires Python 3.10 or newer; there are
no runtime dependencies. Tests additionally need pytest:
`pip install -e ".[dev]"`.

## Where things live

Both default to the directory you run in, so an investigation stays with the
repo it belongs to and you can commit, copy or delete it like any other
artifact:

- `./lockledger.db` — the snapshot store (`--db PATH` or `$LOCKLEDGER_DB`)
- `./lockledger-cache/` — downloaded artifacts (`--cache-dir DIR` or
  `$LOCKLEDGER_CACHE_DIR`)

## Quickstart

```sh
$ lockledger add package-lock.json --label pre-incident
package-lock.json: snapshot #1 (npm), 61 packages, label 'pre-incident'

$ lockledger add package-lock.json --label incident      # after a dependency bump
package-lock.json: snapshot #2 (npm), 62 packages, label 'incident'

$ lockledger add package-lock.json --label fix           # pinned back afterwards
package-lock.json: snapshot #3 (npm), 62 packages, label 'fix'

$ lockledger add requirements.txt                        # also works on the Python side
requirements.txt: snapshot #4 (pip), 3 packages, label '-'
```

`add` accepts `package-lock.json`, `npm-shrinkwrap.json`, `requirements.txt`
and `pip freeze` output, `uv.lock`, several files at once, and any mix of them.
Adding the same file again is a no-op — the digest is compared and the existing
snapshot is reported instead of a second one being written:

```sh
$ lockledger add package-lock.json
package-lock.json: already snapshotted as #3 (label 'fix'), unchanged
```

Ask when a version entered the tree:

```sh
$ lockledger when event-stream 4.0.1
#  created_at            label         version(s)
-  --------------------  ------------  ----------
1  2026-10-20T09:13:05Z  pre-incident  1.0.0
2  2026-10-20T09:13:06Z  incident      4.0.1
3  2026-10-20T09:13:06Z  fix           3.0.20
4  2026-10-20T09:13:07Z  -             -

4.0.1 entered at snapshot #2 (incident)
4.0.1 replaced by 3.0.20 at snapshot #3 (fix)
```

Every snapshot ever recorded is listed, including the Python lockfile that
never contained the package — row 4 is why `when` reads best as one lockfile
evolving over time (see Limitations).

`lockledger when <name>` without a version walks the whole history and prints
each transition, so you can see an unexpected package appear as well as a known
one move.

Package names are matched case-insensitively, and a version that never appears
in the store exits 2 rather than printing an empty table.

Fetch the two artifacts you want to compare, then diff them offline:

```sh
$ lockledger cache event-stream@4.0.1 event-stream@3.0.20
event-stream@4.0.1 (npm): downloaded -> lockledger-cache/npm/event-stream/4.0.1/event-stream-4.0.1.tgz
event-stream@3.0.20 (npm): downloaded -> lockledger-cache/npm/event-stream/3.0.20/event-stream-3.0.20.tgz

$ lockledger diff event-stream 4.0.1 3.0.20
event-stream 4.0.1 -> 3.0.20 (npm)
  files: 25 -> 20 (delta -5, +1 / -6 / 9 changed)
  added (1):
    .npmignore
  removed (6):
    examples/data
    examples/map.js
    examples/split.js
    test/filter.asynct.js
    test/flatmap.asynct.js
    test/parse.asynct.js
  changed (9):
    .travis.yml
    LICENCE
    examples/pretty.js
    index.js
    package.json
    readme.markdown
    test/merge.asynct.js
    test/readable.asynct.js
    test/replace.asynct.js
  install scripts: 1 change(s)
    - prepublish: npm ls && npm test
  SECURITY:
    ! removed install script prepublish
```

`diff` refuses to download. If an artifact is not cached it exits 2 and prints
the exact command that fetches it:

```sh
$ lockledger diff safe 1.0.0 1.0.1 --ecosystem npm
safe@1.0.1 is not in the local cache: run `lockledger cache safe@1.0.1 --ecosystem npm`
```

That separation is deliberate: fetching is the only step with network access,
so you can hand a directory of snapshots plus a populated cache to a machine
with no route off the network and keep working.

## What `diff` looks at

- file-count delta, and the full added / removed / changed path lists (bounded
  on screen, complete under `--json`)
- **npm**: new, changed or removed install hooks among `preinstall`,
  `install`, `postinstall`, `prepublish`, `preprepare` in `package.json`.
  A newly appearing hook is the single strongest signal of a compromised
  release, so it is called out under a `SECURITY:` heading
- **PyPI**: sdists and wheels; new or removed `setup.py` / `setup.cfg` /
  `pyproject.toml`, and changes to declared entry points
  (`.dist-info/entry_points.txt` in wheels, `[options.entry_points]` in
  `setup.cfg`)

## Lockfile formats

| Format | Files | Notes |
| --- | --- | --- |
| npm | `package-lock.json` v2/v3 (`packages`), v1 (`dependencies`) | `npm-shrinkwrap.json` too; workspace links and `file:` deps are skipped, they have no published artifact |
| pip | `requirements.txt`, `pip freeze` output | `==` pins and `name @ url` direct references; `--hash` values are stored as integrity records |
| uv | `uv.lock` | name, version, source and the sdist/wheel URL, via stdlib `tomllib` |

Format is inferred from the filename and content; `--format npm|pip|uv`
overrides the guess. PyPI names are normalised to lowercase the way pip does
it, npm names are stored exactly as the lockfile writes them, and unpinned or
editable requirements are reported on stderr instead of being guessed at.

## Registry overrides

`cache` hits `https://registry.npmjs.org` and `https://pypi.org/pypi` by
default. Point it at a mirror or an internal proxy with `--registry`:

```sh
lockledger cache chalk@5.3.0 --registry https://npm.internal
```

When a lockfile recorded a `resolved` URL, that URL is used instead of the
default registry, because it is where the tree actually got its bytes. When it
also recorded an `integrity` hash, the download is verified against it and a
mismatch is refused — a substituted artifact never reaches the cache.

## Scripting

Every command takes `--json`. With the flag set, stdout carries exactly one
JSON document and nothing else; notes, warnings and error text go to stderr, so
`lockledger when event-stream 4.0.1 --json 2>/dev/null | jq .events` is always
parseable. The shapes are small and stable:

- `add` → `{"snapshots": [{id, source, sha256, label, format, packages, duplicate}]}`
- `when` → `{"name", "version", "snapshots": [{id, label, source, created_at,
  versions}], "events" | "transitions"}`
- `cache` → `{"cache_dir", "artifacts": [{name, version, ecosystem, path,
  already_cached}]}`
- `diff` → `{"name", "from", "to", "ecosystem", "files": {added, removed,
  changed, *_count}, "install_scripts" | "build_files" + "entry_points",
  "flags"}`; a missing artifact prints
  `{"error": "not_cached", "artifact", "cache_command"}` on stdout and exits 2

`flags` is the list worth alerting on: one entry per newly appearing or changed
install hook.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | ok |
| 1 | usage error or bad input (unreadable lockfile, unknown format, failed download) |
| 2 | not found (package or version absent from the store, artifact not cached) |

## Limitations

Honest list of what `lockledger` does not do:

- **No advisory matching.** It will not tell you that a version is malicious.
  You look that up in the advisory, then ask `when` and `diff`.
- **No typosquat or suspicious-name heuristics.** New dependencies are
  reported as history, not judged.
- **No behavioural analysis.** `diff` compares file lists and install hooks;
  it does not read the code in between, sandbox it, or deobfuscate it.
- **Snapshots are per lockfile.** Adding a second lockfile for a different part
  of the tree makes packages that only live in the first one look absent from
  the second, so `when` reads best when each snapshot is the same lockfile over
  time. Cross-scope correlation is a later feature.
- **`uv.lock` needs Python 3.11+.** It is parsed with the standard library's
  `tomllib`; on 3.10 the rest of the tool works and `uv.lock` is rejected with
  that message rather than a third-party TOML dependency.
- **Artifacts are read into memory** during a diff, which is fine for ordinary
  packages but slow for a tarball with hundreds of megabytes of vendored
  binaries. Added, removed and changed lists are capped at 20 entries on
  screen (`--limit`); `--json` always carries the complete lists.
- **Only lockfiles you already have.** `lockledger` cannot reconstruct history
  for a snapshot you never recorded, and it will not go fetch old lockfiles
  from git for you.
- **Formats seen but not handled yet**: yarn v1/berry lockfiles, pnpm,
  Poetry, Cargo and `flake.lock`.
- **No provenance attestation checking** (SLSA / Sigstore) and no registry
  metadata beyond the artifact download itself.

## Development

```sh
pytest -q
```

The suite runs fully offline: `urlopen` is refused for every test, and the
download paths are exercised against an in-memory registry double.

## License

MIT, see `LICENSE`.
