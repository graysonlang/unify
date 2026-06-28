# AGENTS.md

Context for AI coding agents (Claude in VS Code, etc.) working on this repository.
Read this before proposing changes to `unify.py`. It captures intent, invariants,
and known sharp edges that aren't obvious from the code alone.

## What this tool does

`unify.py` consolidates multiple directory trees of media files into one
**canonical** tree, deduplicating by content hash.

- The **first** positional argument is the canonical root. It is the source of
  truth and the destination; unique content from other roots is *moved into* it.
- Every additional positional argument is an **other root** that gets folded in:
  files whose content already exists in the canonical set are moved aside as
  duplicates; genuinely new files are moved into the canonical tree, preserving
  their relative directory structure.
- Duplicates are relocated to `TIMESTAMP/Duplicates/<hash>/<original-filename>`.
  The first copy of a given hash seen in a run is moved there; any later file
  with the **same hash and the same size** is byte-identical to it and is
  deleted at the source rather than moved — regardless of its filename.
- The canonical root keeps a persistent hash cache at
  `CANONICAL_ROOT/.hash_map.tsv` so repeat runs don't re-hash unchanged files.

This program **moves and deletes real files**. That fact dominates every design
decision below.

## Provenance

This is a Python port of an earlier macOS bash script (`unify.sh`,
"dedupe_movies_unified.sh"). Behavioral parity with that script was a deliberate
goal of the port, with several intentional changes (see "Intentional deviations"
below). The port has since also gained correctness/safety fixes that go beyond
the shell (cross-device moves, content-based dedup, an overlapping-roots guard).
If you find a discrepancy with the shell script's behavior, assume it's
intentional unless it looks like a bug — check the "Intentional deviations"
section before "fixing" it.

## Architecture (and why it's shaped this way)

The module is deliberately split so the real work is importable and testable
without a process boundary:

- `main(argv=None) -> int` — **thin** CLI wrapper. Parses args, resolves/validates
  them, delegates, maps outcomes to an exit code. No business logic lives here.
- `resolve_config(args) -> Config` — all validation; raises `SystemExit` with
  the shell script's exact error wording on fatal, run-wide problems.
- `unify(config) -> int` — the real entry point. Importable, no arg parsing of
  its own. Returns a **failure count** (0 == clean) so callers/tests can assert
  on it without catching `SystemExit`.
- `index_canonical(run, old_cache)` — pass 1 over the canonical tree.
- `merge_other_root(run, src_root)` — pass 2, called once per other root.
- `archive_duplicate(...)` — the shared "move/delete a duplicate" logic both
  passes call (this was duplicated ~40 lines in the original; keep it unified).
- `Config` — resolved, validated inputs (derived paths exposed as properties).
- `Run` — live run state: the in-progress `Cache`, a `failures` counter, the
  `archived` map (`hash -> (path of first archived copy, size)`, which drives
  content-based duplicate collapsing), an open `log_fh` (the move log is kept
  open for the whole run rather than reopened per file), and the `warn()` /
  `log()` helpers.
- `Cache` — the `.hash_map.tsv` model: `by_rel` and `by_hash` dicts, with
  `load` / `add` / `cached_hash` / `write`.
- `Entry` — frozen dataclass for `(hash, size, mtime)`; don't revert this to a
  bare tuple, the named fields are load-bearing for readability.

When adding behavior, prefer extending `unify`/the pass functions and keep
`main` thin. New flags get wired in `build_parser`, validated in
`resolve_config`, and carried on `Config` — not read from globals.

## Invariants — do not break these

These are the things that make the tool safe. A change that violates one is a
bug even if tests pass.

1. **`--dry-run` (`-n`) touches nothing on disk.** No file is moved, deleted, or
   created; not even the timestamp folder, the log, or the cache. Every mutating
   branch must be gated on `not config.dry_run`. The in-memory `Cache` and
   `Run.archived` state *is* still built during a dry run, so the preview
   accurately reflects what a real run would do (a duplicate of content seen
   earlier in the same dry run is reported as "would delete", etc.) — but that
   state is never persisted. This is the user's safety net; they are told to run
   `-n` first.
2. **The timestamp folder must never be inside any source root.** Otherwise the
   walk would recurse into files the run is actively creating. `resolve_config`
   enforces this via `is_under`; keep that check.
3. **`is_under` must stay drive/relpath-safe.** It intentionally does *not* use
   `os.path.commonpath` (which raises on mixed drives / abs-vs-rel and only
   catches exact-parent cases). It compares absolute paths with a separator
   guard. Don't "simplify" it back to `commonpath`.
4. **Duplicates are archived, never silently deleted — except the one safe case.**
   Dedup is keyed on **content**, not filename. A source file is only
   `os.remove`'d when a copy of the **same hash and same size** has already been
   archived *during this run* (tracked in `Run.archived`), which guarantees a
   verified, byte-identical copy survives. The first copy of each hash is moved
   into `Duplicates/<hash>/` (basename suffixed with `__dupeN` only if that name
   is already taken in the dir). A same-hash/different-size file (possible only on
   a genuine hash collision) is kept alongside, never deleted. Don't widen the
   deletion condition — in particular, never delete against a file the run didn't
   archive itself.
5. **Only canonical uniques go in the rebuilt cache.** Intra-canonical
   duplicates are archived and deliberately *not* added to `Run.cache`. Pass 2's
   "is this already canonical?" check relies on that set being uniques-only.
6. **Cache writes stay atomic.** `Cache.write` writes to a temp file then
   `os.replace`s it. Don't write the cache in place.
7. **Canonical and other roots must be disjoint.** `resolve_config` rejects any
   other root that equals, contains, or is nested inside the canonical root —
   otherwise pass 2 would re-walk the canonical tree and archive its own
   originals as "duplicates". Keep that check.
8. **Moves use `shutil.move`, not `os.rename`.** The whole point is merging trees
   that often live on different drives; `os.rename` raises `EXDEV` across
   filesystems. `shutil.move` falls back to copy+unlink. Don't swap it back to
   `os.rename`.
9. **Per-file errors warn and continue; they do not abort the run.** See below.

## Error-handling model

This was a specific design decision (matching the shell, with a twist):

- A failure on a **single file** (unreadable, failed `mkdir`/`mv`/`rm`, failed
  `stat`) is reported via `Run.warn`, which prints a `WARNING:` to stderr,
  increments `Run.failures`, and **continues** to the next file.
- `stat_signature` returns `(-1, -1)` instead of raising, mirroring the shell's
  `get_size_mtime` fallback. A file whose stat fails (size `< 0`) is warned-and-
  skipped by both passes, so the `-1` sentinel never flows into size comparisons
  or the cache.
- At the end, if `failures > 0`, the process exits **non-zero** even though it
  ran to completion. So "warn and continue" does not mean "pretend it succeeded."
- **Fatal, run-wide** problems (canonical root isn't a directory, timestamp
  folder already exists, timestamp folder inside a source root, an other root
  overlapping the canonical root) raise `SystemExit` from `resolve_config` and
  stop everything before any mutation.

Exit codes: `0` clean, `1` completed-with-failures or a fatal `OSError`, `2`
argparse usage error (Python convention), `130` on `KeyboardInterrupt`.

When you add a new filesystem operation, wrap it the same way: catch `OSError`,
call `run.warn(...)`, and `continue`. Don't let a single bad file kill a
long-running consolidation of a large library.

## Intentional deviations from the shell precursor

Don't "fix" these — they're chosen:

- **Timestamp precision is seconds** (`%Y%m%d%H%M%S`), where the shell used
  minutes. The `-d`/`--dest` help text says `YYYYMMDDHHMMSS` to match.
- **Pass 2 dedup lookup is in-memory** (`Run.cache.by_hash`) rather than
  re-`grep`/`awk`-ing the cache file per file as the shell did. Uniques moved
  earlier in the same pass are reflected because they're added to the in-memory
  cache immediately. Equivalent result, far faster.
- **The cache is written, not appended.** `Cache.write` rewrites `.hash_map.tsv`
  atomically after pass 1 and again after pass 2. The shell (and an earlier
  version of this port) appended each pass-2 move to the file individually; that
  per-file I/O is gone, and so is the partial-line-on-crash window it created.
- **The move log is held open for the whole run** (one line-buffered handle on
  `Run`) instead of reopening it per logged operation.
- **argparse exit codes** (0 for `-h`, 2 for bad flags) follow Python convention
  rather than the shell's `1`.

## Conventions

- **Python 3.8+**, standard library only. No third-party runtime dependencies —
  this is meant to be a drop-in script. Don't add a dependency without flagging
  the tradeoff explicitly; `argparse` over `click`, `hashlib` over anything, etc.
- Type hints throughout; keep them.
- The module docstring mirrors the shell script's header comment block. If
  behavior changes, update the docstring and this file too.
- Filenames/dirs and tunables are module constants near the top
  (`CACHE_FILENAME`, `DUPLICATES_DIRNAME`, `LOG_FILENAME`, `SKIP_NAMES`,
  `CHUNK_SIZE`, `HASH_ALGOS`). Add new ones there, not as inline literals.
- `SKIP_NAMES` currently skips `.DS_Store` and the cache file (`CACHE_FILENAME`,
  `.hash_map.tsv`) so the cache isn't itself indexed/hashed/moved. It's a set, so
  it's cheap to extend.

## How to run / sanity-check changes

```bash
# Always compiles cleanly:
python3 -m py_compile unify.py

# The safe smoke test — dry run shows intended actions, mutates nothing:
python3 unify.py -n CANONICAL_ROOT OTHER_ROOT_1 OTHER_ROOT_2

# Real run:
python3 unify.py CANONICAL_ROOT OTHER_ROOT_1 OTHER_ROOT_2
```

A good manual test fixture (this exercises every branch) is:
- a canonical tree with a nested unique file **and** a second file with identical
  content (intra-canonical dup),
- an other root containing one file that duplicates canonical content and one
  with brand-new content.

Expected: the intra-canonical dup and the cross-root dup land under
`TIMESTAMP/Duplicates/<hash>/`, the new file moves into the canonical tree, and
`.hash_map.tsv` + `move_log.tsv` are written. Re-running should be a near-no-op
(cache hits, nothing new to move).

There is **no automated test suite yet** — see below.

## Open threads / good next steps

These came up during development and are worth picking up:

1. **Add a test suite.** Highest-value gap. `unify(config)` is designed to be
   called directly against `tmp_path` fixtures (pytest). Cover: dry-run mutates
   nothing (but its preview matches a real run); intra-canonical dedup; cross-root
   dedup; the content-based safe-delete path (same content, *different* filename,
   gets deleted); the `__dupeN` basename-collision path in the duplicates dir;
   the `_<hash8>` / `_<hash8>_N` name-collision suffix for new uniques; the
   overlapping-roots `SystemExit`; cache reuse on a second run; and the
   warn-and-continue + non-zero exit on a simulated hash failure (monkeypatch
   `hash_file`).
2. **Optional `--jobs N` parallel hashing.** Hashing distinct files is
   embarrassingly parallel and is the real bottleneck on large (multi-TB)
   libraries. The intended shape: parallelize *only* the hashing pass with a
   process pool (`concurrent.futures`), keep all move/cache/log bookkeeping
   serial so the order-dependent invariants above hold. This is the main reason
   the tool stays in Python rather than moving to Node — see the note in the repo
   discussion / commit history.
3. **Configurable skip patterns.** `SKIP_NAMES` is hardcoded; a `--skip`/ignore
   mechanism (globs, or reading a `.unifyignore`) would be a natural extension.
4. **Symlink / hardlink policy.** Currently unspecified — `os.walk` follows the
   default, and moves don't special-case links. Worth deciding explicitly before
   someone runs this on a tree full of symlinks.
5. **Cross-run dedup against an existing `Duplicates/` tree.** Content-based
   deletion only trusts copies archived in the *current* run (see invariant 4),
   so re-running with a reused `-d` dir won't dedupe against previously archived
   files (it re-moves them under `__dupeN`). That's the safe choice; a verified
   cross-run mode (re-hash the existing archived copy before deleting) could be
   added if desired.

## Things to be careful about

- This tool is destructive. When in doubt, prefer a design that fails safe
  (leaves files in place, warns) over one that's clever.
- Don't introduce concurrency into the move/cache/log path. The single-threaded
  ordering is what keeps the cache and the on-disk state consistent.
- Preserve the dry-run / real-run symmetry: anything you add that prints an
  action in real mode should print a "(dry run …)" line in dry mode, and vice
  versa.
