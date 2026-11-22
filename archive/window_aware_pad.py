"""ARCHIVED, not used: a window-aware loop-head pad (PENDING item 25, rejected 2026-11-22).

Nothing imports this file and no build or test runs it; it is kept so the experiment can be
read and re-run (see archive/README.md).

For each innermost loop (the `align`, its head label, up to the last back edge), the pad is the
one that minimizes the key (32-byte windows the loop touches, jumps in the loop that cross or
end on a 32-byte boundary, a fused cmp/test counted with its jump). The search is over pads from
HEAD's pad (variant 2; variant 1 started at 0) up to MAXPAD, and HEAD's pad is kept unless the
key improves. It replaces `encode._layout` at run time and writes the sysv NASM file:

    python archive/window_aware_pad.py out.asm [stats] [MAXPAD]

Why it was rejected (full matrix, 104 rows, best of 5, A/A copy of HEAD; numbers in
docs/PENDING.md item 25, "A window-aware loop-head pad, tried and rejected"):
  variant 1 (pads from 0):        geomean +0.99 % (A/A -0.32 %), .text +1.2 %
  variant 2 (pads from HEAD's):   geomean +0.84 % (A/A -0.37 %), .text +2.4 %
Both won and lost rows alike (count 64 B AVX2 -19 %, count_parity 64/256 B AVX2 +42 %). The
worst row's cost was memory stalls (4.0 -> 27.7 cycles per call), which a window model does not
see.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from nvol.asm import encode  # noqa: E402
from nvol.asm.mir import Imm, Lab  # noqa: E402

STATS = []
MAXPAD = int(sys.argv[3]) if len(sys.argv) > 3 else 31


def _loop_end(code, i):
    """Index of the last back edge to the head label right after the align at i, or None."""
    if i + 1 >= len(code) or code[i + 1].op != "label":
        return None
    h = code[i + 1].args[0].name
    end = None
    for j in range(i + 1, len(code)):
        ins = code[j]
        if (ins.op == "jmp" or ins.op in encode.JCC) and ins.args and isinstance(ins.args[0], Lab) \
                and ins.args[0].name == h:
            end = j
    return end


def _key(code, i, end, start):
    """(32-byte windows the loop touches, jumps on a window boundary) with the loop at start."""
    off, hits = start, 0
    for j in range(i + 1, end + 1):
        n = encode.length(code[j])
        ins = code[j]
        if n and (ins.op == "jmp" or ins.op in encode.JCC):
            # the jump, or a cmp/test fused with it, crossing or ending on a 32-byte boundary
            s = off
            if j > i + 1 and code[j - 1].op in ("cmp", "test"):
                s -= encode.length(code[j - 1])
            if s // 32 != (off + n) // 32 or (off + n) % 32 == 0:
                hits += 1
        off += n
    size = off - start
    return ((start % 32 + size + 31) // 32, hits)


def _layout(code):
    labels, pos, off = {}, [], 0
    for idx, ins in enumerate(code):
        pos.append(off)
        if ins.op == "label":
            labels[ins.args[0].name] = off
        elif ins.op == "align":
            a, skip = ins.args[0].v, ins.args[1].v
            pad = (-off) % a
            if pad > skip:
                pad = (-off) % 8
            end = _loop_end(code, idx)
            if end is not None and any(c.op == "align" for c in code[idx + 1:end]):
                end = None                       # only innermost loops
            if end is not None:
                k0 = _key(code, idx, end, off + pad)
                best = (k0, pad)
                for p in range(pad, MAXPAD + 1):     # variant 1: range(0, MAXPAD + 1)
                    k = _key(code, idx, end, off + p)
                    if k < best[0] or (k == best[0] and k < k0 and p < best[1]):
                        best = (k, p)
                if best[1] != pad:
                    STATS.append((k0, best[0], pad, best[1]))
                pad = best[1]
            ins.args[2] = Imm(pad)
        off += encode.length(ins)
    return labels, pos


if __name__ == "__main__":
    encode._layout = _layout
    import nvol.__main__ as m  # noqa: E402
    from nvol.kernels import build_modules  # noqa: E402
    with open(sys.argv[1], "w", encoding="utf8", newline="\n") as fh:
        fh.write(m.asm_files(build_modules(), "nasm")["nvol_x86_64_sysv.asm"])
    if len(sys.argv) > 2 and sys.argv[2] == "stats":
        for s in sorted(set(STATS)):
            print(s)
