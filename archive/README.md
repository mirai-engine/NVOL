# Archive

Implementations that NVOL no longer uses, or tried and rejected, kept as documentation. The
rule (CONTRIBUTING, "Implementations are archived, not deleted"): an implementation is never
deleted outright. It stays readable here, or in place but unreachable, and nothing uses it.

- Nothing in `nvol/`, `tests/` or `harness/` imports a file of this folder, and no build or test
  runs it.
- Each file starts with **ARCHIVED, not used**, what it did, how to run it again, and why it
  left, with the numbers and where they are recorded.
- An archived file may stop working as the generator changes. It is documentation, not
  maintained code; re-running it may need the commit it names.

| file | what | why it is here |
|---|---|---|
| [window_aware_pad.py](window_aware_pad.py) | loop-head pad that minimizes 32-byte windows and boundary jumps (two variants) | rejected 2026-11-22: geomean +0.99 % and +0.84 % (PENDING item 25) |

## Kept in place, unreachable

Some code stays where it is, because it is generated with its siblings and removing it would
only change their table indices:

| code | why nothing reaches it |
|---|---|
| `nvol_teddy_fat_avx2_1` (`nvol/kernels/teddy.py`) | Fat needs > 32 patterns, and a 1-byte mask with > 16 patterns is rejected at build time, as in aho-corasick (PENDING 1.2c) |

## Rejected before this rule

Earlier rejected changes were reverted before they were committed. Their code is not in the
tree; their description and numbers are in [PENDING](../docs/PENDING.md)'s Done list.
Examples: the cmp folded from its spill slot, the
`_fold_cmp_reloads` pass, the Rust aligned-pointer rewrite, and the byte-table popcount.
