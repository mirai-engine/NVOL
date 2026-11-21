"""memchr-style runtime dispatch, shared by every kernel family.

Kernels use memchr's raw signature `(needles.., start, end)`, returning a
pointer (NULL = none) or a count. For a public op `P` with implementations
{scalar, sse2, avx2} this emits:

  * `P_entry_sse2` (baseline): len < 16 -> scalar, else sse2
  * `P_entry_avx2` (compiled with avx2): len < 16 -> scalar, len < 32 -> sse2,
    else avx2. The whole entry carries the avx2 target, so every callee
    inlines into it -- one call per invocation, as in memchr's `unsafe_ifunc!`.
  * `P_raw(needles.., start, end)`: an ifunc resolved once into a cached
    function pointer. Iterators call this directly (no index<->pointer churn).
  * `P(needles.., start, len) -> usize`: the index API (SIZE_MAX = none).
"""
from __future__ import annotations

from ..ir import NONE, NULLP, PTR, USIZE, Builder, Call, Function, Module, prim


def _entry(name: str, needles: list, ret, impls: dict, isa: str, small: int, mid: int) -> Function:
    b = Builder(f"{name}_entry_{isa}", ret, isa=isa, doc=f"{name}: {isa} entry point")
    ns = [b.param(n, t) for n, t in needles]
    start, end = b.param("start", PTR), b.param("end", PTR)
    args = ns + [start, end]
    length = b.let("len", end - start)
    with b.if_(length < small):
        b.ret(Call(impls["scalar"], args, ret))
    if isa == "avx2":
        with b.if_(length < mid):
            b.ret(Call(impls["sse2"], args, ret))
    b.ret(Call(impls[isa], args, ret))
    return b.build()


def add_public(mod: Module, name: str, needles: list, impls: dict, doc: str,
               kind: str = "find", small: int = 16, mid: int = 32) -> None:
    """needles: [(name, Ty)]; kind 'find' (returns position) or 'count'."""
    ret = PTR if kind == "find" else USIZE
    if "neon" in impls:
        # aarch64: NEON is baseline -- no runtime detection, no ifunc
        # (memchr arch/aarch64: len < 16 -> byte loop, else the NEON routine)
        rb = Builder(f"{name}_raw", ret, public=True,
                     doc=f"Raw form of {name} over [start, end)" +
                         (": pointer to the match, or NULL." if kind == "find" else "."))
        ns = [rb.param(n, t) for n, t in needles]
        start, end = rb.param("start", PTR), rb.param("end", PTR)
        args = ns + [start, end]
        with rb.if_((end - start) < small):
            rb.ret(Call(impls["scalar"], args, ret))
        rb.ret(Call(impls["neon"], args, ret))
        raw = rb.build()
        mod.functions.append(raw)
        _index_wrapper(mod, name, needles, raw, ret, kind, doc)
        return
    targets = []
    if "avx2" in impls:
        f = _entry(name, needles, ret, impls, "avx2", small, mid)
        mod.functions.append(f)
        targets.append((prim("cpu_avx2"), f.name))
    f = _entry(name, needles, ret, impls, "sse2", small, mid)
    mod.functions.append(f)
    targets.append((None, f.name))

    rb = Builder(f"{name}_raw", ret, public=True,
                 doc=f"Raw form of {name} over [start, end)" +
                     (": pointer to the match, or NULL." if kind == "find" else "."))
    for n, t in needles:
        rb.param(n, t)
    rb.param("start", PTR)
    rb.param("end", PTR)
    raw = rb.build()
    raw.ifunc = targets
    mod.functions.append(raw)

    _index_wrapper(mod, name, needles, raw, ret, kind, doc)


def _index_wrapper(mod, name, needles, raw, ret, kind, doc):
    b = Builder(name, USIZE, public=True, doc=doc)
    ns = [b.param(n, t) for n, t in needles]
    start, length = b.param("start", PTR), b.param("len", USIZE)
    call = Call(raw.name, ns + [start, start + length], ret)
    if kind == "find":
        p = b.let("p", call)
        with b.if_(p.eq(NULLP)):
            b.ret(NONE)
        b.ret(p - start)
    else:
        b.ret(call)
    mod.functions.append(b.build())
