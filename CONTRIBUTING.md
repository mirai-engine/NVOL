# Contributing to NVOL

Bug reports, fixes, kernels and measurements are welcome.

NVOL is low-level infrastructure. Its output is linked into search tools, parsers and engines
that trust it to read only their bytes and to return the exact answer, so one wrong mask or one
off-by-one in a tail loop is a memory-safety bug in every program that uses it. The rules below
follow from that.

## No vibecoding

Code that its author cannot explain line by line is not accepted, whoever or whatever wrote it.
Pull requests produced by autonomous agents, or by prompting a model until the tests pass, are
closed.

You may use an AI assistant only the way you would use a junior engineer:

- to implement a change you designed, whose every line you then review and can defend;
- to translate your own text from your native language into English;
- to open the issues and pull requests you wrote, under your name.

Nothing else. The design, the reasoning, the measurements and the review are yours, and you
answer for them in the review as their author.

## Evidence in every commit and pull request

Every commit and every pull request carries the evidence of what it claims, as snippets and
formulas: code, input, output and the arithmetic that connects them. A description in prose
alone is not enough.

- **A failure:** the smallest input that shows it, the expected and the actual result, and the
  condition that breaks. For example: `memchr(b'a', hay[0..15])` returns `NULL` with `hay[14] ==
  'a'`, because the tail loop runs while `p + 16 <= end` and never covers the last 15 bytes.
- **A fix:** the snippet of the change and the invariant it restores, as a formula. For example:
  every byte in `[start, end)` is read exactly once, and none outside it, so `reads == end -
  start`. Then the test that fails before the fix and passes after it.
- **An improvement:** the measurement before and after, with its unit, its method and its
  formula. For example: instructions per call `= (instr(N) - instr(0)) / N` with `perf stat`,
  `N = 10^6`, best of 15 runs on an idle machine, on the CPU named in the description; and the
  gap `Δ = (new - old) / old`. Show the instructions that changed: the disassembly or the
  generated code, before and after.

## Issues and pull requests

- Open an issue with what you ran, what happened and what you expected, with the evidence above:
  the command, the input, the output and the CPU and OS.
- A pull request goes to `main`. Keep it focused: one behaviour change per pull request, with
  the test that proves it.
- Before opening it, run:

  ```sh
  bash harness/verify.sh
  ```

  It runs the generator's checks, pytest over the generated C and assembly, the C++ header, the
  Rust and Odin harnesses, aarch64 under QEMU and System V under WSL. Say in the description that
  it passed, and on which platform. A step that cannot run on your machine counts as a failure;
  name it, and why.

## Rules for the generator

- `nvol/` is the product, and `generated/` is its output. Never edit `generated/` by hand:
  change the generator and regenerate. `tests/test_generated_sync.py` fails when they differ.
- A kernel is written once, in the IR. Every backend prints it; no backend gets a hand-written
  version of a kernel.
- A new primitive needs its semantics in `nvol/primitives.py`, its lowering for every language
  and for the assembly backend, and an isolated test against that semantics: a vector
  primitive gets its case in `tests/test_prims.py`. A scalar one (`load64`,
  `first_offset64`, ...) is drawn by `tests/test_asm_fuzz.py` for the assembly backend and is
  reached in each language through the kernels' differential tests, until `test_prims` covers
  the scalar primitives too (PENDING 2.3).
- A new kernel needs its oracle in `nvol/oracle.py` and passes `python -m nvol check` against the
  interpreter, which rejects undefined behaviour.
- The generated code is equal to or better than the implementation it replaces, never worse,
  except for a limit of the target language. A row that is worse is a bug, or a documented
  deviation with its cause.
- Implementations are archived, not deleted. An implementation that is replaced, rejected
  after measuring, or no longer reached is never removed outright. It moves to
  [archive/](archive/README.md), where nothing imports, builds or tests it, or it stays in
  place unreachable and is listed there. Either way it stays readable, with what it did and
  why it left.
- Code adapted from another project gets its entry in
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) in the same change: the project, its
  license, the copyright holder as its repository states it, and where it lives here.

## Commits

One change per commit, in the imperative: what it does, then why it had to be done that way,
with the evidence described above. Tests and the documentation that states the new behaviour
travel with the code. Small follow-ups get their own commit.

## Documentation

The documents in `docs/` are part of the product, and they are held to the same rule as the
code: a claim is true only while something checks it.

- One claim per sentence. Numbers carry their unit and the way they were measured.
- State the present, not a wish. What the tree does not do yet belongs in
  [docs/PENDING.md](docs/PENDING.md).
- Say where the edge is: a feature that works for a subset says which subset.
- Name the proof: a paragraph that claims behaviour ends with the test or the script that shows
  it.
- When a change makes a sentence in `docs/` false, changing that sentence is part of the change.

## License

By contributing, you agree that your contribution is licensed under the terms of both
[MIT](LICENSE-MIT) and [Apache-2.0](LICENSE-APACHE), as the rest of NVOL.
