"""Instruction lengths and branch sizes: NVOL decides the encoding, not each assembler.

`length(ins)` is the size in bytes of the shortest encoding of a machine-IR instruction, for
every form the backend emits (an unknown form raises, so a new one is noticed). `relax(code)`
gives every jump to a label its size and every loop head its alignment padding (`align`, NOP
bytes NVOL writes itself). The printers spell every size (`short`/`near`, `SHORT`/`NEAR PTR`,
`{disp8}`/`{disp32}`) instead of leaving it to the assembler: NASM 2.16.01 keeps some near
jumps whose short form fits (two jumps that depend on each other, as in a rotated loop), and
MASM and GNU as, which see the alignment padding as fixed bytes, would shrink a jump NVOL keeps
near so that a loop head stays aligned. An assembler that disagrees fails (a short jump out of
reach) or gives other bytes (tests/test_asm_syntaxes.py).

`vex_store_form(ins)` covers the one place where the shortest encoding is not the obvious one:
a VEX register-to-register move from ymm8-15 to ymm0-7 fits in a 2-byte VEX prefix only in the
store form (`7f /r`, the source in ModRM.reg). GNU as picks it; NASM and ml64 do not, so their
printers write its bytes.

The lengths are checked against GNU as, instruction by instruction, over the whole library
(`tests/test_asm_syntaxes.py`).
"""
from __future__ import annotations

from .mir import Imm, Lab, Mem, Reg

_NUM = {"rax": 0, "rcx": 1, "rdx": 2, "rbx": 3, "rsp": 4, "rbp": 5, "rsi": 6, "rdi": 7}
_NUM.update({f"r{i}": i for i in range(8, 16)})
_NUM.update({f"xmm{i}": i for i in range(16)})

JCC = {"ja", "jae", "jb", "jbe", "je", "jne", "jz", "jnz", "js", "jns"}

# legacy SSE: (mandatory prefix 66/F3, opcode bytes after it, immediate bytes)
_SSE = {"pcmpeqb": 2, "por": 2, "pand": 2, "psubb": 2, "paddb": 2, "pminub": 2, "paddq": 2, "punpckhqdq": 2,
        "punpcklbw": 2, "punpckhbw": 2, "psadbw": 2, "pxor": 2, "pmovmskb": 2, "movd": 2,
        "pshufb": 3}
_SSE_IMM = {"pshufd": 2, "psrlw": 2, "palignr": 3, "pcmpistri": 3}
# VEX: map (1 = 0F, 2 = 0F38, 3 = 0F3A), W1, immediate
_VEX = {"vpcmpeqb": (1, 0, 0), "vpor": (1, 0, 0), "vpand": (1, 0, 0), "vpsubb": (1, 0, 0), "vpaddb": (1, 0, 0),
        "vpminub": (1, 0, 0), "vpaddq": (1, 0, 0), "vpsadbw": (1, 0, 0), "vpxor": (1, 0, 0),
        "vpunpcklbw": (1, 0, 0), "vpunpckhbw": (1, 0, 0), "vpunpckhqdq": (1, 0, 0),
        "vpmovmskb": (1, 0, 0), "vmovd": (1, 0, 0), "vmovq": (1, 1, 0), "vpsrlw": (1, 0, 1),
        "vpshufb": (2, 0, 0), "vptest": (2, 0, 0), "vpbroadcastb": (2, 0, 0),
        "vbroadcasti128": (2, 0, 0), "vextracti128": (3, 0, 1), "vperm2i128": (3, 0, 1),
        "vpermq": (3, 1, 1), "vpalignr": (3, 0, 1), "vpextrq": (3, 1, 1),
        "vpcmpistri": (3, 0, 1), "vpshufd": (1, 0, 1)}
_ALU = {"add", "sub", "and", "or", "xor", "cmp"}


def _n(r) -> int:
    return _NUM[r.name]


def _ext(a) -> bool:
    """Does this operand need a REX/VEX extension bit (a register 8-15, or one in a Mem)?"""
    if isinstance(a, Reg):
        return _n(a.r) >= 8
    if isinstance(a, Mem):
        return any(r is not None and r.name != "rip" and _n(r) >= 8 for r in (a.base, a.index))
    return False


def _byte_rex(a) -> bool:
    """spl, bpl, sil, dil exist only with a REX prefix."""
    return isinstance(a, Reg) and a.w == 8 and a.r.name in ("rsp", "rbp", "rsi", "rdi")


def mem_len(m: Mem) -> int:
    """ModRM, SIB and displacement of a memory operand."""
    if m.sym is not None:
        return 1 + 4                                   # RIP-relative disp32
    base, index, disp = m.base, m.index, m.disp
    sib = index is not None or (base is not None and base.name in ("rsp", "r12"))
    if base is None:
        return 1 + 1 + 4
    if disp == 0 and base.name not in ("rbp", "r13"):
        d = 0
    elif -128 <= disp <= 127:
        d = 1
    else:
        d = 4
    return 1 + sib + d


def _rm(a) -> int:
    return mem_len(a) if isinstance(a, Mem) else 1


def _legacy(op_bytes: int, args, w64=False, prefix=0, imm=0, modrm=True) -> int:
    rex = w64 or any(_ext(a) or _byte_rex(a) for a in args)
    rm = next((a for a in args if isinstance(a, Mem)), None)
    body = (mem_len(rm) if rm is not None else 1) if modrm else 0
    return prefix + rex + op_bytes + body + imm


def _vex(mp: int, w1: int, rm, imm: int) -> int:
    """VEX prefix (2 bytes only for map 0F, W0 and no X/B), opcode, ModRM part, immediate."""
    three = mp != 1 or w1 or _ext(rm)
    return (3 if three else 2) + 1 + _rm(rm) + imm


def _imm8(v: int, w: int) -> bool:
    if w == 32:
        v &= 0xFFFFFFFF
        v = v - (1 << 32) if v >= (1 << 31) else v
    return -128 <= v <= 127


def length(ins) -> int:
    op, a = ins.op, ins.args
    if op in ("label", "endprolog"):
        return 0
    if op == "align":
        return a[2].v                                  # the padding encode.relax decided
    if op in ("ret",):
        return 1
    if op == "vzeroupper":
        return 3
    if op in ("call", "tailcall") and isinstance(a[0], Lab):
        return 5
    if op in JCC:
        return 2 if getattr(ins, "hint", None) == "short" else 6
    if op == "jmp" or (op == "tailjmp" and isinstance(a[0], Lab)):
        return 2 if getattr(ins, "hint", None) == "short" else 5
    if op in ("callind", "tailjmp", "tailcall"):       # FF /2, FF /4
        return _legacy(1, a)
    if op in ("push", "pop"):
        return _legacy(1, a, modrm=False) + 0
    w = a[0].w if isinstance(a[0], (Reg, Mem)) else 64
    if op in _ALU:
        dst, src = a
        w64 = dst.w == 64 if isinstance(dst, Reg) else dst.w == 64
        if isinstance(src, Imm):
            if _imm8(src.v, w):
                return _legacy(1, [dst], w64, imm=1)   # 83 /n ib
            if isinstance(dst, Reg) and _n(dst.r) == 0:
                return w64 + 1 + 4                     # 05/25/2d/3d... id (accumulator)
            return _legacy(1, [dst], w64, imm=4)       # 81 /n id
        return _legacy(1, a, w64)
    if op == "test":
        return _legacy(1, a, a[0].w == 64)
    if op in ("mov", "trunc32"):
        dst, src = a
        if isinstance(src, Imm):
            if isinstance(dst, Mem):
                return _legacy(1, [dst], dst.w == 64, imm=1 if dst.w == 8 else 4)
            v = src.v
            if dst.w == 32:
                return _legacy(1, [dst], modrm=False) + 4          # B8+r id
            s = v - (1 << 64) if v >= (1 << 63) else v
            if -(1 << 31) <= s < (1 << 31):
                return _legacy(1, [dst], True, imm=4)              # REX.W C7 /0 id
            return _legacy(1, [dst], True, modrm=False) + 8        # movabs
        wd = dst.w if isinstance(dst, Reg) else (src.w if isinstance(src, Reg) else dst.w)
        return _legacy(1, a, wd == 64)
    if op == "lea":
        return _legacy(1, a, a[0].w == 64)          # a 32-bit lea has no REX.W
    if op == "movzx":
        return _legacy(2, a)
    if op == "movsxd":
        return _legacy(1, a, True)
    if op in ("shl", "shr"):
        dst, n = a
        if isinstance(n, Imm):
            return _legacy(1, [dst], dst.w == 64, imm=0 if n.v == 1 else 1)
        return _legacy(1, [dst], dst.w == 64)                      # D3 /n (cl)
    if op == "imul":
        if len(a) == 3:
            return _legacy(1, a[:2], a[0].w == 64, imm=1 if _imm8(a[2].v, a[0].w) else 4)
        return _legacy(2, a, a[0].w == 64)
    if op in ("bsf", "bsr", "bt") or op.startswith("cmov"):
        return _legacy(2, a, a[0].w == 64)
    if op == "popcnt":
        return _legacy(2, a, a[0].w == 64, prefix=1)
    if op.startswith("set"):
        return _legacy(2, a)
    if op in ("movdqa", "movdqu"):
        return _legacy(2, a, prefix=1)
    if op == "movq":                                   # 66 REX.W 0F 7E
        return _legacy(2, a, True, prefix=1)
    if op in _SSE:
        return _legacy(_SSE[op], a, prefix=1)
    if op in _SSE_IMM:
        return _legacy(_SSE_IMM[op], [x for x in a if not isinstance(x, Imm)], prefix=1, imm=1)
    if op in ("vmovdqa", "vmovdqu"):
        dst, src = a
        if isinstance(src, Mem):
            return _vex(1, 0, src, 0)                  # 6f: load
        if isinstance(dst, Mem):
            return _vex(1, 0, dst, 0)                  # 7f: store
        return min(_vex(1, 0, src, 0), _vex(1, 0, dst, 0))
    if op in _VEX:
        mp, w1, imm = _VEX[op]
        regs = [x for x in a if not isinstance(x, Imm)]
        if op in ("vextracti128", "vpextrq", "vmovq"):
            rm = regs[0]           # ModRM.rm is the destination (xmm/m128, r64)
        elif op in ("vpsrlw", "vmovd"):
            rm = regs[1]           # the source (VEX.vvvv is the destination of vpsrlw)
        else:
            rm = regs[-1]          # the last source
        return _vex(mp, w1, rm, imm)
    raise NotImplementedError(f"no length for {op} {a}")


def vex_store_form(ins):
    """The bytes of a VEX register-to-register move written in the store form, when that is the
    only 2-byte-VEX encoding (source ymm8-15/xmm8-15, destination 0-7), else None."""
    if ins.op not in ("vmovdqa", "vmovdqu") or not all(isinstance(x, Reg) for x in ins.args):
        return None
    dst, src = ins.args
    d, s = _n(dst.r), _n(src.r)
    if not (s >= 8 and d < 8):
        return None
    pp = 1 if ins.op == "vmovdqa" else 2               # 66 / F3
    lbit = 1 if dst.w == 256 else 0
    byte2 = (0 << 7) | (0b1111 << 3) | (lbit << 2) | pp   # R̄ = 0: ModRM.reg is register 8-15
    return [0xC5, byte2, 0x7F, 0xC0 | ((s & 7) << 3) | d]


UWOP = {"push": 0, "alloc_large": 1, "alloc_small": 2, "savexmm": 8}


def unwind(code: list):
    """Win64 unwind info of a function's prologue (the instructions `regalloc._frame` marks), or
    None for a leaf without one: (prologue size, [16-bit slots as (byte offset, byte op|info)
    or a raw 16-bit value]), slots in the order UNWIND_INFO stores them (last step first)."""
    steps, off = [], 0
    for ins in code:
        if ins.op == "endprolog":
            break
        off += length(ins)
        if ins.hint == "push":
            steps.append([(off, UWOP["push"] | (_n(ins.args[0].r) << 4))])
        elif ins.hint == "alloc":
            n = ins.args[1].v
            if n <= 128:
                steps.append([(off, UWOP["alloc_small"] | (((n - 8) // 8) << 4))])
            else:
                steps.append([(off, UWOP["alloc_large"]), n // 8])
        elif ins.hint == "savexmm":
            mem, r = ins.args
            steps.append([(off, UWOP["savexmm"] | (_n(r.r) << 4)), mem.disp // 16])
    else:
        return None
    slots = [s for step in reversed(steps) for s in step]
    return off, slots


def xdata(info) -> bytes:
    """UNWIND_INFO: version 1, no flags, no frame register, the slots padded to an even count."""
    size, slots = info
    out = bytearray([1, size, len(slots), 0])
    for s in slots:
        out += bytes(s) if isinstance(s, tuple) else s.to_bytes(2, "little")
    if len(slots) % 2:
        out += bytes(2)
    return bytes(out)


# DWARF register numbers (System V psABI)
_DWARF = {"rax": 0, "rdx": 1, "rcx": 2, "rbx": 3, "rsi": 4, "rdi": 5, "rbp": 6, "rsp": 7}
_DWARF.update({f"r{i}": i for i in range(8, 16)})

# the CIE every FDE points to: version 1, "zR", code align 1, data align -8, return address
# in column 16, FDE addresses pc-relative sdata4; CFA = rsp + 8, return address at CFA - 8
CIE = bytes([0x14, 0, 0, 0, 0, 0, 0, 0, 1, ord("z"), ord("R"), 0, 1, 0x78, 16, 1, 0x1B,
             0x0C, 7, 8, 0x90, 1, 0, 0])


def _uleb(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)


def cfi(code: list) -> list:
    """System V call frame information of a function: [(offset, step)] where a step is
    ("def_cfa_offset", n), ("offset", reg, n) (the register saved at CFA - n),
    ("remember",) or ("restore",), at the byte offset after the instruction that makes it
    true. The same list drives GNU as's `.cfi_*` and the `.eh_frame` NASM gets as data."""
    out, off, cfa = [], 0, 8
    saved, remember = None, False
    for k, ins in enumerate(code):
        if ins.hint in ("unalloc", "pop") and saved is None:
            # an epilogue: its steps hold until its ret/jmp; when code follows that, the rule
            # set before the epilogue is saved here and restored after it
            t = next(j for j in range(k, len(code)) if code[j].op in ("ret", "tailjmp", "tailcall"))
            remember = any(c.op not in ("label", "endprolog") for c in code[t + 1:])
            if remember:
                out.append((off, ("remember",)))
            saved = cfa
        off += length(ins)
        if ins.hint == "push":
            cfa += 8
            out += [(off, ("def_cfa_offset", cfa)), (off, ("offset", ins.args[0].r.name, cfa))]
        elif ins.hint == "alloc":
            cfa += ins.args[1].v
            out.append((off, ("def_cfa_offset", cfa)))
        elif ins.hint == "unalloc":
            cfa -= ins.args[1].v
            out.append((off, ("def_cfa_offset", cfa)))
        elif ins.hint == "pop":
            cfa -= 8
            out.append((off, ("def_cfa_offset", cfa)))
        elif ins.op in ("ret", "tailjmp", "tailcall") and saved is not None:
            if remember:
                out.append((off, ("restore",)))
                cfa = saved
            saved, remember = None, False
    return out


def fde_program(steps: list) -> bytes:
    """The DWARF call frame instructions for cfi(): advance_loc, def_cfa_offset, offset."""
    out, loc = bytearray(), 0
    for off, step in steps:
        d = off - loc
        if d:
            if d < 64:
                out.append(0x40 | d)
            elif d < 256:
                out += bytes([0x02, d])
            elif d < 65536:
                out += bytes([0x03]) + d.to_bytes(2, "little")
            else:
                out += bytes([0x04]) + d.to_bytes(4, "little")
            loc = off
        if step[0] == "def_cfa_offset":
            out += bytes([0x0E]) + _uleb(step[1])
        elif step[0] == "offset":
            out += bytes([0x80 | _DWARF[step[1]]]) + _uleb(step[2] // 8)
        elif step[0] == "remember":
            out.append(0x0A)
        elif step[0] == "restore":
            out.append(0x0B)
    return bytes(out)


def fde_length(program: bytes) -> int:
    """The FDE length field: CIE pointer, pc begin, pc range, augmentation length and the
    instructions, padded with DW_CFA_nop to a multiple of 4 (what GNU as writes for x86-64)."""
    n = 4 + 4 + 4 + 1 + len(program)
    return n + (-n) % 4


# the recommended multi-byte NOPs (Intel SDM, "NOP"), 1 to 9 bytes
NOPS = {1: [0x90], 2: [0x66, 0x90], 3: [0x0F, 0x1F, 0x00], 4: [0x0F, 0x1F, 0x40, 0x00],
        5: [0x0F, 0x1F, 0x44, 0x00, 0x00], 6: [0x66, 0x0F, 0x1F, 0x44, 0x00, 0x00],
        7: [0x0F, 0x1F, 0x80, 0x00, 0x00, 0x00, 0x00],
        8: [0x0F, 0x1F, 0x84, 0x00, 0x00, 0x00, 0x00, 0x00],
        9: [0x66, 0x0F, 0x1F, 0x84, 0x00, 0x00, 0x00, 0x00, 0x00]}


def nops(n: int) -> list:
    """n bytes of padding as the fewest NOP instructions. Every printer writes these bytes
    itself (each assembler pads with its own NOPs, so the syntaxes would differ)."""
    out = []
    while n > 0:
        k = min(n, 9)
        out.append(NOPS[k])
        n -= k
    return out


def code_size(code: list) -> int:
    return sum(length(ins) for ins in code)


def relax(code: list) -> list:
    """Size every jump to a label inside `code`, as `Ins.hint`, and every `align` (its padding,
    from the offset: a function starts 16-aligned). All jumps start short and grow while their
    target is out of reach. Alignment padding can then shrink and bring a grown jump back in
    reach; such a jump is made short again and the layout redone, so that the result is a
    fixed point in which no near jump could be short."""
    branches = [i for i, ins in enumerate(code)
                if (ins.op == "jmp" or ins.op in JCC) and isinstance(ins.args[0], Lab)
                and ins.args[0].name.startswith(".")]
    for i in branches:
        code[i].hint = "short"
    pinned = set()                     # jumps that oscillate: kept near
    for _ in range(64):
        _grow(code, branches)
        labels, pos = _layout(code)
        shrink = [i for i in branches if code[i].hint == "near" and i not in pinned
                  and -128 <= labels[code[i].args[0].name] - (pos[i] + 2) <= 127]
        if not shrink:
            return code
        for i in shrink:
            code[i].hint = "short"
        pinned |= set(shrink) if _ > 32 else set()
    return code


FUNC_ALIGN = 64     # every function starts on a cache line (the printers pad to it)


def _layout(code: list):
    """Label offsets and instruction offsets, the alignment padding recomputed.

    Offsets count from the function's start, which the printers put on a FUNC_ALIGN boundary,
    so a function's place in the uop cache's 32-byte windows depends only on its own code: an
    edit to another function no longer moves it. A loop's place in a window can change its
    cycles by 30 % (memmem 4 KiB, PENDING item 25), and with 16-byte function alignment every
    edit redrew that for every function after it. Loop heads keep the cheap rule: 16 when the
    pad costs at most the align's skip, else 8 (aligning every head to 32 ran up to 31 NOP
    bytes on each loop entry and grew .text 8 %, for no gain over this one)."""
    labels, pos, off = {}, [], 0
    for ins in code:
        pos.append(off)
        if ins.op == "label":
            labels[ins.args[0].name] = off
        elif ins.op == "align":
            a, skip = ins.args[0].v, ins.args[1].v
            pad = (-off) % a
            if pad > skip:
                pad = (-off) % 8
            ins.args[2] = Imm(pad)
        off += length(ins)
    return labels, pos


def _grow(code: list, branches: list) -> None:
    while True:
        labels, pos = _layout(code)
        grew = False
        for i in branches:
            ins = code[i]
            if ins.hint == "short" and not -128 <= labels[ins.args[0].name] - (pos[i] + 2) <= 127:
                ins.hint = "near"
                grew = True
        if not grew:
            return
