# unify

`unify` is a Python command-line utility for merging several directory trees of
files into one **canonical** tree, deduplicating by content hash. It's built for
consolidating overlapping media libraries and backups — for example, a handful of
external drives full of movies — into a single clean tree, with every duplicate
set aside (not silently destroyed) and a log of everything it moved.

It's a single self-contained script (Python 3.8+, standard library only) with no
dependencies to install.

> ⚠️ **This program moves and deletes real files.** Always do a dry run (`-n`)
> first and read what it intends to do.

## Requirements

- Python 3.8+
- Standard library only — no third-party dependencies.

## Install

There's nothing to install; it's a single script.

```bash
git clone <this-repo>
cd unify
python3 unify.py --help
```

Optionally make it executable and put it on your `PATH`:

```bash
chmod +x unify.py
```

## Usage

```
unify.py [options] CANONICAL_ROOT [OTHER_ROOT ...]
```

- `CANONICAL_ROOT` — the unified, canonical tree. It is both the source of truth
  and the destination: unique content from the other roots is *moved into* it.
- `OTHER_ROOT ...` — additional roots to fold in. Must be disjoint from the
  canonical root (not the same as, inside, or containing it).

### Options

| Option | Description |
| --- | --- |
| `-n`, `--dry-run` | Show what would happen; move/delete/create nothing. |
| `-d DIR`, `--dest DIR` | Name of the timestamp/metadata folder (default: `YYYYMMDDHHMMSS`). |
| `-a ALGO`, `--algo ALGO` | Hash algorithm: `md5` (default) or `sha1`. |

### Example

```bash
# Preview first — this mutates nothing:
python3 unify.py -n MoviesUnified /Volumes/Backup1 /Volumes/Backup2

# Then do it for real:
python3 unify.py MoviesUnified /Volumes/Backup1 /Volumes/Backup2
```

## What it does

For each file it computes a content hash and decides:

- **New content** → moved into the canonical tree, preserving its relative
  directory structure. A name clash with an existing canonical file is resolved
  by appending a short hash suffix (`name_<hash8>.ext`).
- **Duplicate content** (hash already in the canonical set) → moved aside into
  the run's `Duplicates/` folder. The *first* copy of each hash is kept there;
  any later file with the **same hash and size** is byte-identical and is deleted
  at the source instead (regardless of its filename).

Deduplication is by content, so identical files with different names are still
collapsed. Files are moved with `shutil.move`, so source and destination can live
on different drives/filesystems.

## What it writes

A timestamp folder is created in the current working directory:

```
TIMESTAMP/
  Duplicates/<hash>/<original-filename>   # archived duplicate files
  move_log.tsv                            # hash <TAB> src_path <TAB> dest_path
```

The canonical root keeps a persistent hash cache so repeat runs don't re-hash
unchanged files:

```
CANONICAL_ROOT/.hash_map.tsv             # hash <TAB> relpath <TAB> size <TAB> mtime
```

On later runs, a canonical file is re-hashed only if its size or mtime changed.

## Safety notes

- **Dry run first.** `-n` prints the full plan and touches nothing on disk; the
  preview reflects exactly what a real run would do.
- **The timestamp folder must not live inside any source root**, and **the other
  roots must be disjoint from the canonical root.** Both are validated up front
  and abort the run before any change.
- **Per-file errors don't abort the run.** An unreadable file or a failed move is
  reported as a `WARNING` and skipped; the process still exits non-zero if any
  file was skipped.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Clean run. |
| `1` | Completed but some files were skipped, or a fatal filesystem error. |
| `2` | Bad command-line usage (argparse). |
| `130` | Interrupted (Ctrl-C). |

## Development

```bash
python3 -m py_compile unify.py
```

`unify(config)` is the importable, testable entry point (no argument parsing of
its own); `main()` is a thin CLI wrapper. See [AGENTS.md](AGENTS.md) for design
intent, invariants, and contributor guidance.
