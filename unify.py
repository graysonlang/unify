#!/usr/bin/env python3
"""unify.py  (dedupe_movies_unified)

Use the FIRST source root as the unified, canonical tree.

  - The canonical root keeps all unique files (by content hash).
  - Additional roots are deduped into the canonical tree.
  - Duplicates are moved into:
        TIMESTAMP/Duplicates/<hash>/<original-filename>
    with extra dupes deleted if same hash + same filename + same size.

HASH CACHE:
  The canonical root maintains a persistent hash cache:
      CANONICAL_ROOT/.hash_map.tsv
  Format (with header):
      hash<TAB>relpath<TAB>size<TAB>mtime
  On each run:
    - For each canonical file, relpath is looked up in the cache.
    - If size & mtime match, the cached hash is reused.
    - Otherwise, the file is rehashed and the cache is updated.

TIMESTAMP FOLDER (created in the current working directory):
  TIMESTAMP/
    Duplicates/
    move_log.tsv

IMPORTANT: This program MOVES files. Run with -n first.

This module is structured so that `unify(config)` is the importable, testable
entry point that performs the actual work, while `main()` only parses the
command line and handles top-level errors before delegating to it.
"""
import argparse
import hashlib
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

# Type aliases for readability.
RelPath = str
HashStr = str

CACHE_FILENAME = ".hash_map.tsv"
DUPLICATES_DIRNAME = "Duplicates"
LOG_FILENAME = "move_log.tsv"
SKIP_NAMES = {".DS_Store"}
CHUNK_SIZE = 1024 * 1024  # 1 MiB

HASH_ALGOS = {"md5": hashlib.md5, "sha1": hashlib.sha1}


# ---------------------------------------------------------------------------
# Hashing / stat helpers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Entry:
    """A cached record of a file's content hash and stat signature."""
    hash: HashStr
    size: int
    mtime: int


def hash_file(path: str, algo: str) -> HashStr:
    """Compute a streaming file hash (md5 or sha1)."""
    try:
        h = HASH_ALGOS[algo.lower()]()
    except KeyError:
        raise ValueError(f"Unsupported hash algorithm: {algo}")
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def stat_signature(path: str) -> Tuple[int, int]:
    """Return (size, mtime) for cache-validation purposes.

    Mirrors the shell's `get_size_mtime`, which returned "-1 -1" rather than
    failing when stat could not read the file.
    """
    try:
        st = os.stat(path)
    except OSError:
        return -1, -1
    return st.st_size, int(st.st_mtime)


def iter_files(root: str) -> Iterator[Tuple[str, RelPath]]:
    """Yield (absolute_path, relpath_from_root) for every non-skipped file."""
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name in SKIP_NAMES:
                continue
            path = os.path.join(dirpath, name)
            yield path, os.path.relpath(path, root)


# ---------------------------------------------------------------------------
# Persistent hash cache
# ---------------------------------------------------------------------------

class Cache:
    """Maps relpath -> Entry and hash -> primary relpath for the canonical tree."""

    def __init__(self) -> None:
        self.by_rel: Dict[RelPath, Entry] = {}
        self.by_hash: Dict[HashStr, RelPath] = {}

    @classmethod
    def load(cls, cache_path: str) -> "Cache":
        """Load a canonical cache file: hash<TAB>relpath<TAB>size<TAB>mtime."""
        cache = cls()
        if not os.path.isfile(cache_path):
            return cache
        with open(cache_path, "r", encoding="utf-8") as f:
            next(f, None)  # skip header
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 4:
                    continue
                h, rel, size_s, mtime_s = parts
                try:
                    cache.add(rel, Entry(h, int(size_s), int(mtime_s)))
                except ValueError:
                    continue
        return cache

    def add(self, rel: RelPath, entry: Entry) -> None:
        self.by_rel[rel] = entry
        # Only the first rel seen for a given hash is treated as canonical.
        self.by_hash.setdefault(entry.hash, rel)

    def cached_hash(self, rel: RelPath, size: int, mtime: int) -> Optional[HashStr]:
        """Return the cached hash if the stat signature still matches."""
        entry = self.by_rel.get(rel)
        if entry and entry.size == size and entry.mtime == mtime:
            return entry.hash
        return None

    def write(self, cache_path: str) -> None:
        """Atomically rewrite the cache file from the in-memory state."""
        tmp_path = f"{cache_path}.new.{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write("hash\trelpath\tsize\tmtime\n")
            for rel, e in self.by_rel.items():
                f.write(f"{e.hash}\t{rel}\t{e.size}\t{e.mtime}\n")
        os.replace(tmp_path, cache_path)


# ---------------------------------------------------------------------------
# Path mapping helpers
# ---------------------------------------------------------------------------

def canonical_rel_for(src_root_abs: str, path: str, file_hash: str,
                      canonical_abs: str) -> RelPath:
    """Map a non-canonical file into the canonical tree, preserving structure and
    resolving name collisions with a short hash suffix."""
    rel_path = os.path.relpath(path, src_root_abs)
    rel_dir, base = os.path.split(rel_path)
    rel_dir = "" if rel_dir == "." else rel_dir

    candidate = os.path.join(rel_dir, base) if rel_dir else base
    if os.path.exists(os.path.join(canonical_abs, candidate)):
        name, ext = os.path.splitext(base)
        suffixed = f"{name}_{file_hash[:8]}{ext}"
        candidate = os.path.join(rel_dir, suffixed) if rel_dir else suffixed
    return candidate


def is_under(child: str, parent: str) -> bool:
    """True if child is parent or nested inside it (drive/relpath safe)."""
    child = os.path.abspath(child)
    parent = os.path.abspath(parent)
    return child == parent or child.startswith(parent + os.sep)


def unique_dest(directory: str, base_name: str) -> str:
    """Return a non-colliding path inside directory based on base_name."""
    candidate = os.path.join(directory, base_name)
    i = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{base_name}__dupe{i}")
        i += 1
    return candidate


# ---------------------------------------------------------------------------
# Run configuration and shared state
# ---------------------------------------------------------------------------

@dataclass
class Config:
    """Resolved, validated inputs for a single unify run."""
    canonical_abs: str
    other_roots: List[str]          # validated, original (relative) spellings
    timestamp_abs: str
    hash_algo: str
    dry_run: bool

    @property
    def duplicates_dir(self) -> str:
        return os.path.join(self.timestamp_abs, DUPLICATES_DIRNAME)

    @property
    def cache_file(self) -> str:
        return os.path.join(self.canonical_abs, CACHE_FILENAME)

    @property
    def log_file(self) -> str:
        return os.path.join(self.timestamp_abs, LOG_FILENAME)


@dataclass
class Run:
    """Live state for a run: config, the in-progress canonical cache, and a
    failure counter so per-file errors warn-and-continue (like the shell) while
    still letting the process exit non-zero if anything went wrong."""
    config: Config
    cache: Cache = field(default_factory=Cache)
    failures: int = 0

    def warn(self, message: str) -> None:
        """Print a warning, count it as a failure, and let the caller continue."""
        print(f"WARNING: {message}", file=sys.stderr)
        self.failures += 1

    def log(self, file_hash: str, src: str, dest: str) -> None:
        if self.config.dry_run:
            return
        with open(self.config.log_file, "a", encoding="utf-8") as lf:
            lf.write(f"{file_hash}\t{src}\t{dest}\n")


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------

def archive_duplicate(run: Run, path: str, size: int, file_hash: str,
                      label: str) -> None:
    """Move (or delete, if already archived identically) a duplicate file.

    Same hash already present in the canonical set -> the file is redundant.
    If an identically named, identically sized copy is already under
    Duplicates/<hash>/, the source is simply deleted; otherwise it is moved
    there (suffixing the name on a size mismatch). Any filesystem error is
    reported via run.warn and processing continues.
    """
    dup_hash_dir = os.path.join(run.config.duplicates_dir, file_hash)
    dest_path = os.path.join(dup_hash_dir, os.path.basename(path))
    tag = f"{label} DUP" if label else "DUP"

    if run.config.dry_run:
        if os.path.exists(dest_path):
            print(f"{tag} (dry run, would delete):", path)
        else:
            print(f"{tag} (dry run, would move):", path, "->", dest_path)
        return

    try:
        os.makedirs(dup_hash_dir, exist_ok=True)
    except OSError as e:
        run.warn(f"Failed to create duplicates dir '{dup_hash_dir}' for '{path}': {e}")
        return

    if os.path.exists(dest_path):
        if os.path.getsize(dest_path) == size:
            try:
                os.remove(path)
            except OSError as e:
                run.warn(f"Failed to delete duplicate '{path}': {e}")
                return
            run.log(file_hash, path, dest_path)
            print(f"DELETED {tag}:", path, f"(already have {dest_path})")
            return
        # Same hash, different size: keep both under a suffixed name.
        dest_path = unique_dest(dup_hash_dir, os.path.basename(path))

    try:
        # shutil.move (not os.rename) so duplicates can be archived across
        # filesystems/drives; os.rename raises EXDEV across devices.
        shutil.move(path, dest_path)
    except OSError as e:
        run.warn(f"Failed to move duplicate '{path}' to '{dest_path}': {e}")
        return
    run.log(file_hash, path, dest_path)
    print(f"MOVED {tag}:", path, "->", dest_path)


def index_canonical(run: Run, old_cache: Cache) -> None:
    """First pass: walk the canonical tree, reuse cached hashes where the stat
    signature still matches, archive any intra-canonical duplicates, and rebuild
    the canonical cache from what actually remains."""
    print("Indexing canonical root (using hash cache)...\n")
    canonical_abs = run.config.canonical_abs

    for path, rel in iter_files(canonical_abs):
        size, mtime = stat_signature(path)
        file_hash = old_cache.cached_hash(rel, size, mtime)
        if file_hash is None:
            try:
                file_hash = hash_file(path, run.config.hash_algo)
            except OSError as e:
                run.warn(f"Failed to compute hash for '{path}', skipping: {e}")
                continue

        if file_hash in run.cache.by_hash:
            existing = os.path.join(canonical_abs, run.cache.by_hash[file_hash])
            if run.config.dry_run:
                print("CANON DUP (dry run):", path, f"(duplicate of {existing})")
            else:
                archive_duplicate(run, path, size, file_hash, label="CANON")
            # Duplicates are never added to the rebuilt cache.
            continue

        # Record the unique even on a dry run so duplicate detection works in
        # both passes; the cache is only persisted to disk on a real run.
        run.cache.add(rel, Entry(file_hash, size, mtime))
        if run.config.dry_run:
            print("CANON UNIQUE (dry run):", path, f"(rel: {rel})")
        else:
            run.log(file_hash, path, path)
            print("CANON UNIQUE:", path)


def merge_other_root(run: Run, src_root: str) -> None:
    """Second pass: fold one additional source root into the canonical set.

    Files whose hash is already canonical are archived as duplicates; genuinely
    new content is moved into the canonical tree (preserving its relative
    hierarchy) and appended to both the in-memory cache and the on-disk cache."""
    config = run.config
    src_abs = os.path.abspath(src_root)
    print("Source root:", src_abs)

    for path, _rel in iter_files(src_abs):
        size, mtime = stat_signature(path)
        try:
            file_hash = hash_file(path, config.hash_algo)
        except OSError as e:
            run.warn(f"Failed to compute hash for '{path}', skipping: {e}")
            continue

        if file_hash in run.cache.by_hash:
            archive_duplicate(run, path, size, file_hash, label="")
            continue

        # New unique content -> move into the canonical tree.
        canon_rel = canonical_rel_for(src_abs, path, file_hash, config.canonical_abs)
        canon_full = os.path.join(config.canonical_abs, canon_rel)

        if config.dry_run:
            # Register the new hash so a later identical file is previewed as a
            # duplicate, matching what the real run would do.
            run.cache.add(canon_rel, Entry(file_hash, size, mtime))
            print("MOVE UNIQUE (dry run):", path, "->", canon_full)
            continue

        try:
            os.makedirs(os.path.dirname(canon_full), exist_ok=True)
            # shutil.move (not os.rename) so unique files can be folded into the
            # canonical tree across filesystems/drives; os.rename raises EXDEV.
            shutil.move(path, canon_full)
        except OSError as e:
            run.warn(f"Failed to move '{path}' to '{canon_full}': {e}")
            continue

        new_size, new_mtime = stat_signature(canon_full)
        run.cache.add(canon_rel, Entry(file_hash, new_size, new_mtime))
        with open(config.cache_file, "a", encoding="utf-8") as cf:
            cf.write(f"{file_hash}\t{canon_rel}\t{new_size}\t{new_mtime}\n")
        run.log(file_hash, path, canon_full)
        print("MOVED UNIQUE:", path, "->", canon_full)


# ---------------------------------------------------------------------------
# Library entry point
# ---------------------------------------------------------------------------

def unify(config: Config) -> int:
    """Run a full unify pass for an already-validated Config.

    This is the real entry point: importable and testable, doing no argument
    parsing of its own. Returns the number of per-file failures encountered
    (0 means a fully clean run), so callers can translate that into an exit
    code. Fatal, run-wide problems are raised as exceptions for main() to
    report.
    """
    print(f"Canonical unified root: {config.canonical_abs}")
    if config.other_roots:
        print("Additional source roots:")
        for r in config.other_roots:
            print(f"  {r}")
    else:
        print("No additional source roots provided (only canonical will be scanned).")
    print(f"Timestamp folder:        {config.timestamp_abs}")
    print(f"  Duplicates dir:        {config.duplicates_dir}")
    print(f"Hash algorithm:          {config.hash_algo}")
    print(f"Dry run:                 {config.dry_run}\n")

    if not config.dry_run:
        os.makedirs(config.duplicates_dir, exist_ok=True)
        os.makedirs(config.timestamp_abs, exist_ok=True)
        with open(config.log_file, "w", encoding="utf-8") as lf:
            lf.write("hash\tsrc_path\tdest_path\n")

    run = Run(config=config)
    old_cache = Cache.load(config.cache_file)

    # Pass 1: canonical tree (rebuilds run.cache from surviving uniques).
    index_canonical(run, old_cache)
    if not config.dry_run:
        run.cache.write(config.cache_file)

    # Pass 2: fold each additional root into the canonical set.
    print("\nProcessing additional source roots...\n")
    for src_root in config.other_roots:
        merge_other_root(run, src_root)

    print("\nDone.")
    if config.dry_run:
        print("Dry run only; no files were moved or deleted.")
    else:
        print("Canonical unified tree:", config.canonical_abs)
        print("Duplicates under:      ", config.duplicates_dir)
        print("Canonical hash cache:  ", config.cache_file)
        print("Move log:              ", config.log_file)

    if run.failures:
        print(f"\nCompleted with {run.failures} warning(s); some files were skipped.",
              file=sys.stderr)
    return run.failures


# ---------------------------------------------------------------------------
# Command-line interface
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """CLI surface, worded after the precursor unify.sh."""
    parser = argparse.ArgumentParser(
        prog="unify.py",
        usage="%(prog)s [options] CANONICAL_ROOT [OTHER_ROOT...]",
        description="The first source root is treated as the unified, canonical "
                    "tree. All additional roots are deduped into it.",
        epilog="IMPORTANT: This program MOVES files. Run with -n first.\n"
               "Example:\n  %(prog)s MoviesUnified Backup1 Backup2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-n", "--dry-run", action="store_true",
        help="dry run (no files moved/deleted, just print what would happen)")
    parser.add_argument(
        "-d", "--dest", dest="timestamp_dir", metavar="DIR",
        help="timestamp/metadata directory name (default: YYYYMMDDHHMMSS)")
    parser.add_argument(
        "-a", "--algo", dest="hash_algo", default="md5",
        choices=sorted(HASH_ALGOS), metavar="ALGO",
        help="hash algorithm: md5 (default) or sha1")
    parser.add_argument(
        "canonical_root", metavar="CANONICAL_ROOT",
        help="canonical unified root directory")
    parser.add_argument(
        "other_roots", nargs="*", metavar="OTHER_ROOT",
        help="additional source roots to dedupe into canonical")
    return parser


def resolve_config(args: argparse.Namespace) -> Config:
    """Validate parsed arguments and resolve them into a Config.

    Raises SystemExit (with the shell script's error wording) on any fatal,
    run-wide problem: a non-directory canonical root, a pre-existing timestamp
    folder, or a timestamp folder that sits inside a source root. Non-directory
    OTHER_ROOTs are warned about and skipped, exactly as in unify.sh.
    """
    if not os.path.isdir(args.canonical_root):
        raise SystemExit(
            f"ERROR: Canonical root '{args.canonical_root}' is not a directory.")
    canonical_abs = os.path.abspath(args.canonical_root)

    timestamp_dir = args.timestamp_dir or time.strftime("%Y%m%d%H%M%S")
    timestamp_abs = os.path.join(os.getcwd(), timestamp_dir)
    if os.path.exists(timestamp_abs) and not args.dry_run:
        raise SystemExit(
            f"ERROR: Timestamp folder '{timestamp_abs}' already exists. "
            f"Use -d to choose another.")

    valid_others: List[str] = []
    for r in args.other_roots:
        if not os.path.isdir(r):
            print(f"WARNING: Source root '{r}' is not a directory, skipping.",
                  file=sys.stderr)
            continue
        other_abs = os.path.abspath(r)
        # An additional root that equals, contains, or sits inside the canonical
        # root would cause canonical originals to be archived/deleted as their
        # own duplicates. Reject overlapping roots outright.
        if is_under(other_abs, canonical_abs) or is_under(canonical_abs, other_abs):
            raise SystemExit(
                f"ERROR: Source root '{r}' overlaps the canonical root "
                f"'{canonical_abs}'.\n"
                f"       Canonical and additional roots must be disjoint trees.")
        valid_others.append(r)

    src_roots_abs = [canonical_abs] + [os.path.abspath(r) for r in valid_others]
    for parent in src_roots_abs:
        if is_under(timestamp_abs, parent):
            raise SystemExit(
                f"ERROR: Timestamp folder '{os.path.abspath(timestamp_abs)}' is "
                f"inside source root '{os.path.abspath(parent)}'.\n"
                f"       Run from a parent directory or use -d to choose a "
                f"different destination.")

    return Config(
        canonical_abs=canonical_abs,
        other_roots=valid_others,
        timestamp_abs=timestamp_abs,
        hash_algo=args.hash_algo,
        dry_run=args.dry_run,
    )


def main(argv: Optional[List[str]] = None) -> int:
    """Thin CLI wrapper: parse arguments, resolve/validate them, and hand off to
    unify(). Fatal errors surface as a message + non-zero exit; otherwise the
    exit code reflects the number of per-file failures."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = resolve_config(args)
        failures = unify(config)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except OSError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
