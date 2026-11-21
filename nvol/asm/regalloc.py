"""Register allocation over machine IR, then the stack frame.

The algorithm is target-neutral. What names a target's instructions (scratch registers, copy and
spill instructions, the peephole, the frame) comes from a target module `T` (`x86`).

Positions: instruction i reads its operands at 2i and writes its results at 2i + 1, so a value
defined by `mov v, rcx` never conflicts with rcx, and `mov rcx, v` followed by a use of rcx keeps
v out of rcx only while both are live.

Intervals have holes: a virtual register holds its register only in the segments where it is
live (per block, from the block's start or its first access to the block's end or its last
access), so two values whose segments interleave share one. That matters where a hot path
jumps over code (inlined kernels in a dispatcher) that, in layout order, sits between a value's
definition and its uses.

Physical registers that the code names directly (arguments on entry, rax on return, rcx for
variable shifts, argument registers and clobbers around calls) are fixed ranges an interval may
not overlap. When registers run out, the interval that ends last is spilled to a stack slot and
the whole allocation runs again with the scratch registers spill code needs held back.

A spilled register that a loop only reads is then split there: a copy before the loop's head
takes its reads inside the loop and competes for a register over the loop alone. The allocation
runs again; a split whose copy spilled too is undone, and the next loops inside are tried.
"""
from __future__ import annotations

from bisect import bisect_left

from .abi import ABI
from .mir import CONDJUMPS, PHYS, TERMINATORS, Ins, Lab, Mem, Phys, Reg, VReg


def _blocks(code: list):
    starts = [0]
    for i, ins in enumerate(code):
        if ins.op == "label" and i not in starts:
            starts.append(i)
        if (ins.op in TERMINATORS or ins.op in CONDJUMPS) and i + 1 < len(code):
            starts.append(i + 1)
    starts = sorted(set(starts))
    ranges = [(s, (starts[k + 1] if k + 1 < len(starts) else len(code)) - 1)
              for k, s in enumerate(starts)]
    ranges = [(a, b) for a, b in ranges if a <= b]
    at = {}
    for k, (a, b) in enumerate(ranges):
        if code[a].op == "label":
            at[code[a].args[0].name] = k
    succ = []
    for k, (a, b) in enumerate(ranges):
        last = code[b]
        s = []
        if last.op in ("jmp",) or last.op in CONDJUMPS:
            s.append(at[last.args[0].name])
        if last.op not in TERMINATORS and k + 1 < len(ranges):
            s.append(k + 1)
        succ.append(s)
    return ranges, succ


def _liveness(code, ranges, succ):
    use, defs = [], []
    for a, b in ranges:
        u, d = set(), set()
        for ins in code[a:b + 1]:
            for r in ins.uses:
                if isinstance(r, VReg) and r not in d:
                    u.add(r)
            for r in ins.defs:
                if isinstance(r, VReg):
                    d.add(r)
        use.append(u)
        defs.append(d)
    live_in = [set() for _ in ranges]
    live_out = [set() for _ in ranges]
    changed = True
    while changed:
        changed = False
        for k in reversed(range(len(ranges))):
            out = set().union(*[live_in[s] for s in succ[k]]) if succ[k] else set()
            inn = use[k] | (out - defs[k])
            if out != live_out[k] or inn != live_in[k]:
                live_out[k], live_in[k] = out, inn
                changed = True
    return live_in, live_out


def _intervals(code, ranges, live_in, live_out):
    iv = {}

    def touch(r, p):
        if r in iv:
            a, b = iv[r]
            iv[r] = (min(a, p), max(b, p))
        else:
            iv[r] = (p, p)

    for i, ins in enumerate(code):
        for r in ins.uses:
            if isinstance(r, VReg):
                touch(r, 2 * i)
        for r in ins.defs:
            if isinstance(r, VReg):
                touch(r, 2 * i + 1)
    for k, (a, b) in enumerate(ranges):
        for r in live_in[k]:
            touch(r, 2 * a)
        for r in live_out[k]:
            touch(r, 2 * b + 1)
    return iv


def _segments(code, ranges, live_in, live_out) -> dict:
    """v -> its live segments [(start, end)], sorted and merged: per block, from the block's
    start (live in) or v's first access there, to the block's end (live out) or v's last
    access. A value is held only where it is live, so another can use the register in the
    holes (the code between, in layout order, that no path from a definition to a use
    crosses)."""
    seg = {}
    for k, (a, b) in enumerate(ranges):
        lo, hi = {}, {}
        for i in range(a, b + 1):
            ins = code[i]
            for r in ins.uses:
                if isinstance(r, VReg):
                    lo.setdefault(r, 2 * i)
                    hi[r] = 2 * i
            for r in ins.defs:
                if isinstance(r, VReg):
                    lo.setdefault(r, 2 * i + 1)
                    hi[r] = max(hi.get(r, 0), 2 * i + 1)
        for r in set(lo) | live_in[k] | live_out[k]:
            x = 2 * a if r in live_in[k] else lo[r]
            y = 2 * b + 1 if r in live_out[k] else hi[r]
            seg.setdefault(r, []).append((x, y))
    out = {}
    for r, ss in seg.items():
        ss.sort()
        m = [list(ss[0])]
        for x, y in ss[1:]:
            if x <= m[-1][1] + 1:
                m[-1][1] = max(m[-1][1], y)
            else:
                m.append([x, y])
        out[r] = [tuple(t) for t in m]
    return out


def _overlap(A, B) -> bool:
    i = j = 0
    while i < len(A) and j < len(B):
        if A[i][1] < B[j][0]:
            i += 1
        elif B[j][1] < A[i][0]:
            j += 1
        else:
            return True
    return False


def _fixed(code):
    """Busy ranges of the physical registers the code names directly."""
    busy, cur = {}, {}

    def close(p):
        if p in cur:
            busy.setdefault(p, []).append(tuple(cur.pop(p)))

    for i, ins in enumerate(code):
        for r in ins.uses:
            if isinstance(r, Phys):
                if r in cur:
                    cur[r][1] = 2 * i
                else:
                    busy.setdefault(r, []).append((-1, 2 * i))
        for r in list(ins.defs) + list(ins.clobbers):
            if isinstance(r, Phys):
                close(r)
                cur[r] = [2 * i + 1, 2 * i + 1]
    for p in list(cur):
        close(p)
    return busy


def _hints(code, assign_of, T):
    """v -> preferred physical register, from copies (mov v, x / mov x, v)."""
    hints = {}
    for ins in code:
        if ins.op in T.MOVES and len(ins.args) == 2 \
                and isinstance(ins.args[0], Reg) and isinstance(ins.args[1], Reg):
            d, s = ins.args[0].r, ins.args[1].r
            if isinstance(d, VReg) and isinstance(s, Phys):
                hints.setdefault(d, []).append(s)
            elif isinstance(d, Phys) and isinstance(s, VReg):
                hints.setdefault(s, []).append(d)
            elif isinstance(d, VReg) and isinstance(s, VReg):
                hints.setdefault(d, []).append(s)
                hints.setdefault(s, []).append(d)
    return hints


def _freq(code) -> list:
    """A static frequency per instruction: x8 per loop around it (a backward jump to a label
    before it), /64 per rare region (`coldbegin`..`coldend`, a loop exit; `rarebegin`..
    `rareend`, an arm that loops or returns inside a loop)."""
    at = {ins.args[0].name: i for i, ins in enumerate(code) if ins.op == "label"}
    depth = [0] * (len(code) + 1)
    for i, ins in enumerate(code):
        if (ins.op == "jmp" or ins.op in CONDJUMPS) and isinstance(ins.args[0], Lab):
            h = at.get(ins.args[0].name)
            if h is not None and h < i:
                depth[h] += 1
                depth[i + 1] -= 1
    out, d, rare = [], 0, 0
    for i, ins in enumerate(code):
        d += depth[i]
        if ins.op in ("coldbegin", "rarebegin"):
            rare += 1
        elif ins.op in ("coldend", "rareend"):
            rare -= 1
        out.append(8.0 ** d / 64.0 ** rare)
    return out


def _scan(code, abi: ABI, T, reserve: dict, late=frozenset()):
    ranges, succ = _blocks(code)
    live_in, live_out = _liveness(code, ranges, succ)
    iv = _intervals(code, ranges, live_in, live_out)
    busy = _fixed(code)
    hints = _hints(code, None, T)
    order = {"g": [PHYS[r] for r in abi.gpr_order], "v": [PHYS[r] for r in abi.vec_order]}
    for c in order:            # the scratch registers spill code needs, held back
        order[c] = [p for p in order[c] if p.name not in T.SCRATCH[c][:reserve.get(c, 0)]]

    # a fixed range inside one block conflicts with v only where v is live: read later in the
    # block or live out of it, and already written (`mov rax, x; jmp .Lret` at a return site
    # does not clash with a value only later code in the layout reads)
    blk = [0] * len(code)
    for k, (s0, e0) in enumerate(ranges):
        for i in range(s0, e0 + 1):
            blk[i] = k
    upos, dpos = {}, {}
    for i, ins in enumerate(code):
        for r in ins.uses:
            if isinstance(r, VReg):
                upos.setdefault(r, []).append(2 * i)
        for r in ins.defs:
            if isinstance(r, VReg):
                dpos.setdefault(r, []).append(2 * i + 1)

    def dead(v, x, y):
        k = blk[max(x, 0) // 2]
        s0, e0 = ranges[k]
        if y > 2 * e0 + 1:
            return False
        if v not in live_out[k]:
            us = upos.get(v, ())
            j = bisect_left(us, x)
            if j == len(us) or us[j] > 2 * e0 + 1:
                return True
        if v not in live_in[k]:
            ds = dpos.get(v, ())
            j = bisect_left(ds, 2 * s0)
            if j == len(ds) or ds[j] > y:
                return True
        return False

    def fixed_ok(p, a, b, v=None):
        return all(b < x or y < a or (v is not None and dead(v, x, y))
                   for x, y in busy.get(p, ()))

    freq = _freq(code)
    weight = {}
    for i, ins in enumerate(code):
        for r in dict.fromkeys(ins.uses + ins.defs):
            if isinstance(r, VReg):
                weight[r] = weight.get(r, 0.0) + freq[i]
    segs = _segments(code, ranges, live_in, live_out)

    def fixed_fits(p, v):
        """No fixed range of p where v is live."""
        sv = segs[v]
        return all(not _overlap(sv, [(x, y)]) or dead(v, x, y) for x, y in busy.get(p, ()))

    held = {}                       # register -> the intervals in it
    assign, spilled = {}, set()

    def clash(p, v):
        return [w for w in held.get(p, ()) if _overlap(segs[v], segs[w])]

    def pick(v, free):
        for h in hints.get(v, ()):
            hp = h if isinstance(h, Phys) else assign.get(h)
            if hp in free:
                return hp
        return free[0]

    for v in sorted(iv, key=lambda r: (iv[r][0], iv[r][1])):
        if v in late:
            continue
        a, b = iv[v]
        regs = [p for p in order[v.cls] if fixed_fits(p, v)]
        free = [p for p in regs if not clash(p, v)]
        # a register the code names inside v's interval (where v is dead) last: rax at the
        # return sites is better left to the values returned
        free.sort(key=lambda p: not fixed_ok(p, a, b))
        if free:
            r = pick(v, free)
            assign[v] = r
            held.setdefault(r, []).append(v)
            continue
        # spill the cheapest: v, or the intervals clashing with v in the register where they
        # cost least (the static frequency of their reads and writes, `_freq`; the loop depth
        # alone kept Teddy's verification loops in registers and spilled the scan pointer)
        best = None
        for p in regs:
            ws = clash(p, v)
            cost = sum(weight.get(w, 0.0) for w in ws)
            if best is None or cost < best[0]:
                best = (cost, p, ws)
        if best is not None and best[0] < weight.get(v, 0.0):
            cost, p, ws = best
            for w in ws:
                del assign[w]
                spilled.add(w)
                held[p].remove(w)
            assign[v] = p
            held[p].append(v)
            continue
        spilled.add(v)
    # split copies last, and only into a register free wherever they live: they never take
    # one from an interval the first pass placed
    for v in sorted((r for r in iv if r in late), key=lambda r: iv[r]):
        free = [p for p in order[v.cls] if fixed_fits(p, v) and not clash(p, v)]
        if not free:
            spilled.add(v)
            continue
        r = pick(v, free)
        assign[v] = r
        held.setdefault(r, []).append(v)

    def free(cls, a, b):
        return [p for p in order[cls] if fixed_ok(p, a, b)
                and not any(_overlap([(a, b)], segs[w]) for w in held.get(p, ()))]
    return assign, spilled, free



def _need(code, spilled, T) -> dict:
    """Scratch registers per class the spill code of `spilled` needs (at most 3)."""
    need = {"g": 0, "v": 0}
    for ins in code:
        kind = T.direct(ins, spilled)
        if kind:
            if kind == "both":
                c = ins.args[0].r.cls
                need[c] = max(need[c], 1)
            continue
        regs = [r for r in dict.fromkeys(ins.uses + ins.defs) if isinstance(r, VReg) and r in spilled]
        for c in need:
            need[c] = max(need[c], sum(1 for r in regs if r.cls == c))
    return need


def _subst(ins: Ins, assign) -> Ins:
    return _map(ins, lambda r: assign[r] if isinstance(r, VReg) else r)


def _map(ins: Ins, m) -> Ins:
    """`ins` with every register r replaced by m(r) (the same object when nothing changes)."""
    regs = ins.uses + ins.defs + [r for a in ins.args for r in
                                  ([a.r] if isinstance(a, Reg) else [a.base, a.index] if isinstance(a, Mem) else [])
                                  if r is not None]
    if all(m(r) is r for r in regs):
        return ins

    def op(a):
        if isinstance(a, Reg):
            return Reg(m(a.r), a.w)
        if isinstance(a, Mem):
            return Mem(m(a.base) if a.base is not None else None,
                       m(a.index) if a.index is not None else None,
                       a.scale, a.disp, a.sym, a.w, a.arg, a.spill)
        return a
    return Ins(ins.op, [op(a) for a in ins.args], [m(r) for r in ins.defs],
               [m(r) for r in ins.uses], list(ins.clobbers), ins.hint)


def layout_cold(code: list) -> list:
    """Move every region the lowering marked cold (`coldbegin` .. `coldend`: a loop exit,
    ending in a jump) to the end of the function, after its ret, and invert the conditional
    jump before it to go there. Registers are already allocated, so only the order changes.
    A region not preceded by a single conditional jump (a `lor` condition ends in a label)
    stays in place."""
    from .lower import NEG
    code = list(code)
    while True:
        begins = [i for i, ins in enumerate(code) if ins.op == "coldbegin"]
        if not begins:
            break
        # the innermost: no other region inside it
        for i in begins:
            j = next(k for k in range(i + 1, len(code)) if code[k].op in ("coldbegin", "coldend"))
            if code[j].op == "coldend":
                break
        cold = code[i].args[0]
        region = code[i + 1:j]
        prev = code[i - 1]
        if prev.op in CONDJUMPS and prev.op[1:] in NEG:
            del code[i:j + 1]
            code[i - 1] = Ins("j" + NEG[prev.op[1:]], [cold], prev.defs, prev.uses, prev.clobbers)
            code += region
        else:
            del code[j]
            del code[i]
    return code


def _alloc(code, abi, T, late=frozenset()):
    """assign, spilled; once anything spills, only the scratch registers its spill code needs
    are held back. A second scan may spill other registers, so its needs are checked and the
    reservation widened until they fit."""
    assign, spilled, free = _scan(code, abi, T, reserve={}, late=late)
    if spilled:
        need = _need(code, spilled, T)
        for _ in range(4):
            assign, spilled, free = _scan(code, abi, T, reserve=need, late=late)
            more = _need(code, spilled, T)
            if all(more[c] <= need[c] for c in need):
                break
            need = {c: max(need[c], more[c]) for c in need}
        else:
            assign, spilled, free = _scan(code, abi, T, reserve={"g": 3, "v": 3}, late=late)
    return assign, spilled, free


def _regions(code):
    """(at, end, key, loop) of the regions a split can cover, from `at` to `end`. A loop is
    entered only by falling into `at` (no jump from outside lands on a label in it), so a copy
    inserted before `at` runs before any of it; an arm may not be, and `_split` takes only a
    prefix of it that is.
      - a loop: a backward jump at `end` to the label at its head; `at` is the head, or the
        `align` before it, so the copy runs once per entry and not per iteration;
      - the arm after a forward conditional jump, up to the label it jumps to;
      - the code after a label that only jumps reach (an else arm: a `jmp` before it), up to
        the label the jump before it goes to.
    `key` names the region across rounds (a label name)."""
    at = {ins.args[0].name: i for i, ins in enumerate(code) if ins.op == "label"}
    jumps = [(i, at[ins.args[0].name]) for i, ins in enumerate(code)
             if (ins.op == "jmp" or ins.op in CONDJUMPS) and isinstance(ins.args[0], Lab)
             and ins.args[0].name in at]
    cand = []
    ends = {}
    for i, t in jumps:
        if t < i:
            ends[t] = max(ends.get(t, i), i)
    for h, e in ends.items():
        cand.append((h - 1 if h and code[h - 1].op == "align" else h, e, "loop " + code[h].args[0].name, True))
    for i, t in jumps:
        if t > i + 1 and code[i].op in CONDJUMPS:
            # inside a cold region's markers: layout_cold needs them right after the jump
            h = i + 2 if code[i + 1].op == "coldbegin" else i + 1
            cand.append((h, t - 1, "then " + code[t].args[0].name, False))
            last = code[t - 1]
            if last.op == "jmp" and isinstance(last.args[0], Lab) and at.get(last.args[0].name, -1) > t + 1:
                cand.append((t + 1, at[last.args[0].name] - 1, "else " + code[t].args[0].name, False))
    # a loop must be entered at its head as a whole; an arm only up to where `_split` cuts it
    # (it checks each prefix)
    out = []
    for h, e, key, loop in cand:
        if h <= e and (not loop or all(h <= i <= e or not h <= t <= e for i, t in jumps)):
            out.append((h, e, key, loop))
    return out


def _split(code, spilled, parent, tried, free, T):
    """Split spilled registers where a register is free: a copy of v inserted at the start of
    a single-entry stretch of code takes v's reads there (v is not written there, so its slot
    stays valid) and competes for a register over that stretch alone.

    The stretches are the `_regions` (loops, if and else arms) and the code right after v's
    definition when it has one. Of each, outermost first, the longest prefix that ends at a
    read of v, is entered only at its start, and has a register of v's class free throughout
    (`free`, from the scan that spilled v) is taken; a loop only whole. Two reads at least,
    or a loop. Returns the new code, or None when nothing splits."""
    regions = _regions(code)
    at = {ins.args[0].name: i for i, ins in enumerate(code) if ins.op == "label"}
    jumps = [(i, at[ins.args[0].name]) for i, ins in enumerate(code)
             if (ins.op == "jmp" or ins.op in CONDJUMPS) and isinstance(ins.args[0], Lab)
             and ins.args[0].name in at]
    uses, defs = {}, {}
    for i, ins in enumerate(code):
        for r in ins.uses:
            if isinstance(r, VReg) and r in spilled and r not in parent:
                uses.setdefault(r, []).append(i)
        for r in ins.defs:
            if isinstance(r, VReg):
                defs.setdefault(r, []).append(i)
    avx = T.avx(code)
    nid = max((r.id for ins in code for r in ins.uses + ins.defs if isinstance(r, VReg)), default=0)
    inserts = {}            # position -> copies
    renames = []            # (at, end, {old: new})
    claims = {}             # register -> stretches promised this round

    def entered_at_start(h, e):
        return all(h <= i <= e or not h <= t <= e for i, t in jumps)

    for v in sorted(uses, key=lambda r: r.id):
        dv = defs.get(v, ())
        cand = [(h, e, k, loop) for h, e, k, loop in regions]
        if len(dv) == 1 and all(u > dv[0] for u in uses[v]):
            cand.append((dv[0] + 1, max(uses[v]), "def", False))
        taken = []
        for h, e, key, loop in sorted(cand, key=lambda c: c[0] - c[1]):
            us = [u for u in uses[v] if h <= u <= e]
            if not us or any(h <= i <= e for i in dv):
                continue
            ends = [e] if loop else sorted(us, reverse=True)
            for u in ends:
                n = sum(x <= u for x in us)
                if (n < 2 and not loop) or (v, key, n) in tried:
                    continue
                if any(not (u < h2 or e2 < h) for h2, e2 in taken) or not entered_at_start(h, u):
                    continue
                lo, hi = 2 * h, 2 * u + 1
                regs = [r for r in free(v.cls, lo, hi)
                        if all(hi < x or y < lo for x, y in claims.get(r, ()))]
                if not regs:
                    continue
                claims.setdefault(regs[0], []).append((lo, hi))
                tried.add((v, key, n))
                taken.append((h, u))
                nid += 1
                c = VReg(v.cls, nid)
                copy = T.copy_ins(c, v, avx)
                parent[c] = (v, copy)
                inserts.setdefault(h, []).append(copy)
                renames.append((h, u, {v: c}))
                break
    if not inserts:
        return None
    out = []
    for i, ins in enumerate(code):
        out += inserts.get(i, [])
        for h, e, m in renames:
            if h <= i <= e:
                ins = _map(ins, lambda r, m=m: m.get(r, r))
        out.append(ins)
    return out


def _unsplit(code, bad, parent):
    """Undo the splits whose copy spilled too: drop the copy, rename back to the original."""
    m = {c: parent[c][0] for c in bad}
    drop = {id(parent[c][1]) for c in bad}
    for c in bad:
        del parent[c]
    return [_map(ins, lambda r: m.get(r, r)) for ins in code if id(ins) not in drop]


def allocate(code: list, abi: ABI, T) -> list:
    """Allocate; then split spilled registers around the loops that only read them, allocate
    again, and undo the splits that spilled anyway, until nothing changes."""
    parent, tried = {}, set()
    for _ in range(16):
        assign, spilled, free = _alloc(code, abi, T, set(parent))
        bad = [c for c in parent if c in spilled]
        if bad:
            code = _unsplit(code, bad, parent)
            continue
        new = _split(code, spilled, parent, tried, free, T)
        if new is None:
            break
        code = new
    else:
        assign, spilled, _ = _alloc(code, abi, T, set(parent))
    code = [ins for ins in code if ins.op not in ("rarebegin", "rareend")]
    code, nslots = T.rewrite(code, assign, spilled)
    code = T.peephole(code)
    return layout_cold(T.frame(code, abi, nslots))


