# AGENTS.md

Context for AI coding agents (Claude in VS Code, etc.) working on this repository.
Read this before proposing changes to `unify.py`. It captures intent, invariants,
and known sharp edges that aren't obvious from the code alone.

## What this tool does

See the [README](README.md) for the user-facing description, CLI, and output
layout. In short: `unify.py` folds multiple directory trees into the **first**
(canonical) root, deduplicating by content hash — unique files are moved in
preserving their relative paths, duplicates are archived under
`TIMESTAMP/Duplicates/<hash>/`, and the canonical root keeps a persistent
`.hash_map.tsv` cache.

This program **moves and deletes real files**. That fact dominates every design
decision below.

## Architecture (and why it's shaped this way)

The module is deliberately split so the real work is importable and testable
without a process boundary:

- `main(argv=None) -> int` — **thin** CLI wrapper. Parses args, resolves/validates
  them, delegates, maps outcomes to an exit code. No business logic lives here.
- `resolve_config(args) -> Config` — all validation; raises `SystemExit` with a
  clear error message on fatal, run-wide problems.
- `unify(config) -> int` — the real entry point. Importable, no arg parsing of
  its own. Returns a **failure count** (0 == clean) so callers/tests can assert
  on it without catching `SystemExit`.
- `index_canonical(run, old_cache)` — pass 1 over the canonical tree.
- `merge_other_root(run, src_root)` — pass 2, called once per other root.
- `archive_duplicate(...)` — the shared "move/delete a duplicate" logic both
  passes call (keep it unified — both passes must behave identically here).
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

This is a deliberate design decision:

- A failure on a **single file** (unreadable, failed `mkdir`/`mv`/`rm`, failed
  `stat`) is reported via `Run.warn`, which prints a `WARNING:` to stderr,
  increments `Run.failures`, and **continues** to the next file.
- `stat_signature` returns `(-1, -1)` instead of raising. A file whose stat fails
  (size `< 0`) is warned-and-skipped by both passes, so the `-1` sentinel never
  flows into size comparisons or the cache.
- At the end, if `failures > 0`, the process exits **non-zero** even though it
  ran to completion. So "warn and continue" does not mean "pretend it succeeded."
- **Fatal, run-wide** problems (canonical root isn't a directory, timestamp
  folder already exists, timestamp folder inside a source root, an other root
  overlapping the canonical root) raise `SystemExit` from `resolve_config` and
  stop everything before any mutation.

The exit-code table is in the README; the mapping itself lives in `main()`.

When you add a new filesystem operation, wrap it the same way: catch `OSError`,
call `run.warn(...)`, and `continue`. Don't let a single bad file kill a
long-running consolidation of a large library.

## Deliberate design choices — don't undo these

Each of these is intentional; don't "simplify" it back:

- **Timestamp precision is seconds** (`%Y%m%d%H%M%S`). The `-d`/`--dest` help text
  says `YYYYMMDDHHMMSS` to match.
- **Pass 2 dedup lookup is in-memory** (`Run.cache.by_hash`), not re-read from the
  cache file per file. Uniques moved earlier in the same pass are reflected
  because they're added to the in-memory cache immediately.
- **The cache is written, not appended.** `Cache.write` rewrites `.hash_map.tsv`
  atomically after pass 1 and again after pass 2 — no per-file append, and no
  partial-line-on-crash window.
- **The move log is held open for the whole run** (one line-buffered handle on
  `Run`) instead of reopening it per logged operation.
- **argparse exit codes** (0 for `-h`, 2 for bad flags) follow Python convention.

## Conventions

- **Python 3.8+**, standard library only. No third-party runtime dependencies —
  this is meant to be a drop-in script. Don't add a dependency without flagging
  the tradeoff explicitly; `argparse` over `click`, `hashlib` over anything, etc.
- Type hints throughout; keep them.
- Keep the module docstring in sync with actual behavior; if behavior changes,
  update the docstring, the README, and this file too.
- Filenames/dirs and tunables are module constants near the top
  (`CACHE_FILENAME`, `DUPLICATES_DIRNAME`, `LOG_FILENAME`, `SKIP_NAMES`,
  `CHUNK_SIZE`, `HASH_ALGOS`). Add new ones there, not as inline literals.
- `SKIP_NAMES` currently skips `.DS_Store` and the cache file (`CACHE_FILENAME`,
  `.hash_map.tsv`) so the cache isn't itself indexed/hashed/moved. It's a set, so
  it's cheap to extend.

## How to sanity-check changes

```bash
# Must always compile cleanly:
python3 -m py_compile unify.py
```

See the README for how to run it. There is **no automated test suite yet** (see
below); until there is, smoke-test with `-n`, then a real run, against a fixture
that exercises every branch:

- a canonical tree with a nested unique file **and** a second file with identical
  content (intra-canonical dup);
- an other root with one file that duplicates canonical content and one with
  brand-new content.

Expected: both dups land under `TIMESTAMP/Duplicates/<hash>/`, the new file moves
into the canonical tree, and `.hash_map.tsv` + `move_log.tsv` are written. A
second run should be a near-no-op (cache hits).

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
   serial so the order-dependent invariants above hold.
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
