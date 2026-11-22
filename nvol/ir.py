"""NVOL kernel IR.

A deliberately small, typed, structured IR. Kernels are *written in Python*
with the `Builder` below; the result is a tree of `Stmt`/`Expr` nodes that:

  * the reference interpreter (`nvol.interp`) executes with the primitive
    semantics from `nvol.primitives` (this is the semantic oracle), and
  * every backend (`nvol.backends.*`) prints as source code.

There is no control flow other than `While`/`If`/`Return`, no aliasing other
than raw byte pointers, and every primitive is total over its declared domain.
That keeps the interpreter and all backends trivially in agreement about what a
program means.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional, Union


# --------------------------------------------------------------------------
# Types
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Ty:
    name: str

    def __repr__(self) -> str:
        return self.name


U8 = Ty("u8")
U32 = Ty("u32")
U64 = Ty("u64")
USIZE = Ty("usize")
BOOL = Ty("bool")
PTR = Ty("ptr")    # pointer to const u8
MPTR = Ty("mptr")  # pointer to mutable u8 (only target of Store64)
VOID = Ty("void")  # return type of functions that only store
VEC = Ty("vec")    # the ISA's native byte vector (u8 x isa.bytes)
VISIT = Ty("visit")  # a caller-supplied visitor (see Visit)

INT_TYPES = (U8, U32, U64, USIZE)


# --------------------------------------------------------------------------
# Expressions
# --------------------------------------------------------------------------
class Expr:
    ty: Ty

    # ---- arithmetic / pointer sugar ---------------------------------------
    def __add__(self, o):
        isptr = self.ty in (PTR, MPTR)
        o = lift(o, USIZE if isptr else self.ty)
        if isptr:
            return PtrAdd(self, o, self.ty)
        return Bin("add", self, o, self.ty)

    def __sub__(self, o):
        if self.ty is PTR:
            if isinstance(o, Expr) and o.ty is PTR:
                return PtrDiff(self, o)
            return PtrSub(self, lift(o, USIZE))
        return Bin("sub", self, lift(o, self.ty), self.ty)

    def __radd__(self, o):
        if self.ty in (PTR, MPTR):
            return PtrAdd(self, lift(o, USIZE), self.ty)
        return lift(o, self.ty) + self

    def __rsub__(self, o):
        return lift(o, self.ty) - self

    def __and__(self, o):
        return Bin("and", self, lift(o, self.ty), self.ty)

    def __or__(self, o):
        return Bin("or", self, lift(o, self.ty), self.ty)

    def __xor__(self, o):
        return Bin("xor", self, lift(o, self.ty), self.ty)

    def __rshift__(self, o):
        return Bin("shr", self, lift(o, self.ty), self.ty)

    def __lshift__(self, o):
        """Left shift; bits shifted out are discarded (shift < width required)."""
        return Bin("shl", self, lift(o, self.ty), self.ty)

    # wrapping (modular) arithmetic -- plain add/sub must never wrap
    def wadd(self, o):
        return Bin("wadd", self, lift(o, self.ty), self.ty)

    def wsub(self, o):
        return Bin("wsub", self, lift(o, self.ty), self.ty)

    def wmul(self, o):
        return Bin("wmul", self, lift(o, self.ty), self.ty)

    def __lt__(self, o):
        return Bin("lt", self, lift(o, self.ty), BOOL)

    def __le__(self, o):
        return Bin("le", self, lift(o, self.ty), BOOL)

    def __gt__(self, o):
        return Bin("gt", self, lift(o, self.ty), BOOL)

    def __ge__(self, o):
        return Bin("ge", self, lift(o, self.ty), BOOL)

    def eq(self, o):
        return Bin("eq", self, lift(o, self.ty), BOOL)

    def ne(self, o):
        return Bin("ne", self, lift(o, self.ty), BOOL)

    def addr(self) -> "Expr":
        assert self.ty in (PTR, MPTR)
        return PtrAddr(self)

    def load8(self) -> "Expr":
        assert self.ty in (PTR, MPTR)
        return Load8(self)

    def cast(self, ty: Ty) -> "Expr":
        return self if ty is self.ty else Cast(self, ty)

    __hash__ = object.__hash__


@dataclass(eq=False)
class Var(Expr):
    name: str
    ty: Ty


@dataclass(eq=False)
class Const(Expr):
    value: Optional[int]      # None == "not found" sentinel (max of ty)
    ty: Ty


@dataclass(eq=False)
class Bin(Expr):
    op: str                   # add sub and or xor shr shl wadd wsub wmul max min lt le gt ge eq ne land lor
    a: Expr
    b: Expr
    ty: Ty


@dataclass(eq=False)
class Not(Expr):
    a: Expr
    ty: Ty = BOOL


@dataclass(eq=False)
class Cast(Expr):
    a: Expr
    ty: Ty


@dataclass(eq=False)
class PtrAdd(Expr):
    p: Expr
    n: Expr
    ty: Ty = PTR


@dataclass(eq=False)
class PtrSub(Expr):
    p: Expr
    n: Expr
    ty: Ty = PTR


@dataclass(eq=False)
class PtrDiff(Expr):          # a - b, in bytes, requires a >= b
    a: Expr
    b: Expr
    ty: Ty = USIZE


@dataclass(eq=False)
class PtrAddr(Expr):          # integer address of p
    p: Expr
    ty: Ty = USIZE


@dataclass(eq=False)
class Load8(Expr):
    p: Expr
    ty: Ty = U8


@dataclass(eq=False)
class Prim(Expr):
    """Call to a primitive from `nvol.primitives` (lowered per ISA/language)."""
    name: str
    args: list
    ty: Ty


@dataclass(eq=False)
class Call(Expr):
    """Call to another generated function of the same module."""
    fn: str
    args: list
    ty: Ty


@dataclass(eq=False)
class CallTable(Expr):
    """Indirect call: module.fn_tables[table][idx](args). Every function in a
    table has the same signature. Lets a precomputed selector (e.g. the Teddy
    variant chosen at build time) cost one indirect call, like a vtable."""
    table: str
    idx: Expr
    args: list
    ty: Ty


@dataclass(eq=False)
class Visit(Expr):
    """Call the caller's visitor `cb` (a VISIT parameter) with a chunk base and its candidate
    mask; true = stop. A kernel that hands every chunk with candidates to a visitor lets an
    inlining compiler fuse the caller's per-candidate work into the kernel's loop, instead of
    returning and re-entering the kernel (with its setup) for every such chunk.
    C/C++: `const nvol_visitor *` ({fn, ctx}); Rust: `&mut impl FnMut(*const u8, u64) -> bool`;
    Odin: `^Visitor`; the interpreter: a Python callable."""
    cb: Expr
    at: Expr
    mask: Expr
    ty: Ty = BOOL


def lift(x, ty: Ty) -> Expr:
    if isinstance(x, Expr):
        return x
    if isinstance(x, bool):
        return Const(int(x), BOOL)
    if isinstance(x, int):
        return Const(x, ty)
    raise TypeError(f"cannot lift {x!r}")


NONE = Const(None, USIZE)     # "not found" index (SIZE_MAX)
NULLP = Const(None, PTR)      # "not found" pointer (NULL)


def max_(a: Expr, b) -> Expr:
    return Bin("max", a, lift(b, a.ty), a.ty)


def min_(a: Expr, b) -> Expr:
    return Bin("min", a, lift(b, a.ty), a.ty)


@dataclass(eq=False)
class TableLoad(Expr):
    """byte = module.tables[table][idx] (a constant 256-entry table)."""
    table: str
    idx: Expr
    ty: Ty = U8


def land(a: Expr, b: Expr) -> Expr:
    return Bin("land", a, b, BOOL)


def lor(a: Expr, b: Expr) -> Expr:
    return Bin("lor", a, b, BOOL)


def prim(name: str, *args) -> Prim:
    from . import primitives
    p = primitives.PRIMS[name]
    return Prim(name, [lift(a, t) for a, t in zip(args, p.params)], p.ret)


# --------------------------------------------------------------------------
# Statements
# --------------------------------------------------------------------------
class Stmt:
    pass


@dataclass(eq=False)
class Let(Stmt):
    var: Var
    value: Expr
    mut: bool = False


@dataclass(eq=False)
class Assign(Stmt):
    var: Var
    value: Expr


@dataclass(eq=False)
class While(Stmt):
    cond: Expr
    body: list
    # NVOL's own assembly only (the language backends and the interpreter ignore it): when this
    # holds on entry, the loop first runs in the exit-first shape (test at the top, the body's
    # final `if c: ... return` as the fallthrough, the back edge on not c), whose exit the
    # branch predictor gets right on long trips, then falls into the usual rotated loop
    exit_first: Optional[Expr] = None


@dataclass(eq=False)
class If(Stmt):
    cond: Expr
    then: list
    els: Optional[list] = None
    # NVOL's own assembly only: the language backends drop this `if` (see for_languages),
    # so it must not change the function's result, only how the asm gets it
    asm_only: bool = False


@dataclass(eq=False)
class Return(Stmt):
    value: Optional[Expr]     # None for VOID functions


@dataclass(eq=False)
class Store8(Stmt):
    """Byte store to a mutable pointer."""
    ptr: Expr
    value: Expr


@dataclass(eq=False)
class StoreVec(Stmt):
    """Unaligned store of a whole ISA vector to a mutable pointer."""
    ptr: Expr
    value: Expr


@dataclass(eq=False)
class Store64(Stmt):
    """Unaligned little-endian u64 store to a mutable pointer."""
    ptr: Expr
    value: Expr


@dataclass(eq=False)
class Function:
    name: str
    params: list              # list[Var]
    ret: Ty
    body: list
    isa: Optional[str] = None  # None => portable scalar code
    public: bool = False
    doc: str = ""
    # ifunc-style dispatch: [(condition Prim or None, target fn name), ...].
    # The first entry whose condition holds is resolved ONCE and cached in a
    # function pointer; `body` is then empty. Backends print the cache.
    ifunc: Optional[list] = None
    # keep out of line: big kernels selected by a dispatcher get their own
    # prologue instead of bloating the caller's (Win64 saves xmm6-15)
    noinline: bool = False
    # a function only NVOL's own assembly emits (reached from asm-only ifs): the language
    # backends drop it (see for_languages)
    asm_only: bool = False
# Builder
# --------------------------------------------------------------------------
# Keywords of every target language: a variable never gets one of these names.
RESERVED = frozenset("""
auto break case char const continue default do double else enum extern float for goto if
inline int long register restrict return short signed sizeof static struct switch typedef
union unsigned void volatile while bool true false
alignas alignof and asm catch class constexpr decltype delete explicit export friend mutable
namespace new noexcept not nullptr operator or private protected public template this throw
try typeid typename using virtual xor
as async await crate dyn fn impl in let loop match mod move mut pub ref self super trait
type unsafe use where yield box macro
def del elif except finally from global import is lambda nonlocal pass raise with None True False
bit_set cast context defer distinct dynamic fallthrough foreign map matrix no_inline not_in
or_else or_return package proc transmute when where size_of align_of offset_of type_of
max min len cap abs clamp raw_data make delete new free append swizzle context typeid_of
""".split())


class Builder:
    def __init__(self, name: str, ret: Ty, isa: Optional[str] = None,
                 public: bool = False, doc: str = "", noinline: bool = False,
                 asm_only: bool = False):
        self.name, self.ret_ty, self.isa, self.public, self.doc = name, ret, isa, public, doc
        self.noinline = noinline
        self.asm_only = asm_only
        self.params: list = []
        self._stack: list = [[]]
        self._names: dict = {}
        self._used: set = set()

    # names are made unique per function so that every backend (including
    # languages without shadowing) can declare them at any nesting depth.
    # A generated name never equals one already taken, even a user name that
    # happens to look generated (e.g. "r" -> "r1" while "r1" exists).
    def _fresh(self, base: str) -> str:
        if base in RESERVED:
            base = base + "_"
        n = self._names.get(base, 0)
        name = base if n == 0 else f"{base}{n}"
        while name in self._used:
            n += 1
            name = f"{base}{n}"
        self._names[base] = n + 1
        self._used.add(name)
        return name

    def param(self, name: str, ty: Ty) -> Var:
        v = Var(self._fresh(name), ty)
        self.params.append(v)
        return v

    def emit(self, s: Stmt) -> None:
        self._stack[-1].append(s)

    def let(self, name: str, value, ty: Optional[Ty] = None, mut: bool = False) -> Var:
        value = lift(value, ty or USIZE)
        v = Var(self._fresh(name), value.ty)
        self.emit(Let(v, value, mut))
        return v

    def var(self, name: str, value, ty: Optional[Ty] = None) -> Var:
        return self.let(name, value, ty, mut=True)

    def set(self, v: Var, value) -> None:
        self.emit(Assign(v, lift(value, v.ty)))

    def ret(self, value=None) -> None:
        if self.ret_ty is VOID:
            assert value is None
            self.emit(Return(None))
        else:
            self.emit(Return(lift(value, self.ret_ty)))

    def store8(self, p: Expr, value) -> None:
        assert p.ty is MPTR, "Store8 needs a mutable pointer"
        self.emit(Store8(p, lift(value, U8)))

    def store_vec(self, p: Expr, value: Expr) -> None:
        assert p.ty is MPTR and value.ty is VEC, "StoreVec needs a mutable pointer and a vector"
        self.emit(StoreVec(p, value))

    def store64(self, p: Expr, value) -> None:
        assert p.ty is MPTR, "Store64 needs a mutable pointer"
        self.emit(Store64(p, lift(value, U64)))

    @contextmanager
    def while_(self, cond: Expr, exit_first: Optional[Expr] = None):
        body: list = []
        self._stack.append(body)
        yield
        self._stack.pop()
        self.emit(While(cond, body, exit_first))

    @contextmanager
    def if_(self, cond: Expr, asm_only: bool = False):
        body: list = []
        self._stack.append(body)
        yield
        self._stack.pop()
        self.emit(If(cond, body, None, asm_only))

    @contextmanager
    def else_(self):
        last = self._stack[-1][-1]
        assert isinstance(last, If) and last.els is None
        body: list = []
        self._stack.append(body)
        yield
        self._stack.pop()
        last.els = body

    def build(self) -> Function:
        assert len(self._stack) == 1
        body = prune_unused_lets(self._stack[0])
        return Function(self.name, self.params, self.ret_ty, body,
                        self.isa, self.public, self.doc, noinline=self.noinline,
                        asm_only=self.asm_only)


# --------------------------------------------------------------------------
# Passes
# --------------------------------------------------------------------------
def _expr_children(e):
    for v in vars(e).values():
        if isinstance(v, Expr):
            yield v
        elif isinstance(v, list):
            yield from (x for x in v if isinstance(x, Expr))


def _uses(stmts, acc: dict) -> dict:
    def ex(e):
        if isinstance(e, Var):
            acc[e.name] = acc.get(e.name, 0) + 1
        for c in _expr_children(e):
            ex(c)
    for s in stmts:
        if isinstance(s, (Let, Assign)):
            ex(s.value)
        elif isinstance(s, While):
            ex(s.cond); _uses(s.body, acc)
            if s.exit_first is not None:
                ex(s.exit_first)
        elif isinstance(s, If):
            ex(s.cond); _uses(s.then, acc)
            if s.els:
                _uses(s.els, acc)
        elif isinstance(s, Return):
            if s.value is not None:
                ex(s.value)
        elif isinstance(s, (Store8, Store64, StoreVec)):
            ex(s.ptr); ex(s.value)
    return acc


def prune_unused_lets(stmts: list) -> list:
    """Drop `let`s whose variable is never read (every IR expression is pure).
    A mutable variable that is only assigned, never read, goes too."""
    while True:
        used = _uses(stmts, {})
        dead = set()

        def walk(ss):
            out = []
            for s in ss:
                if isinstance(s, Let) and used.get(s.var.name, 0) == 0:
                    dead.add(s.var.name)
                    continue
                if isinstance(s, Assign) and used.get(s.var.name, 0) == 0:
                    continue
                if isinstance(s, While):
                    s = While(s.cond, walk(s.body), s.exit_first)
                elif isinstance(s, If):
                    s = If(s.cond, walk(s.then), walk(s.els) if s.els else s.els, s.asm_only)
                out.append(s)
            return out
        stmts = walk(stmts)
        if not dead:
            return stmts


def for_languages(module):
    """The module the language backends (C, C++, Rust, Odin, Python) print: without the
    asm-only functions and ifs. module: an ir.Module or {arch: ir.Module}."""
    import copy
    import dataclasses
    if isinstance(module, dict):
        return {k: for_languages(m) for k, m in module.items()}

    def strip(stmts):
        out = []
        for s in stmts:
            if isinstance(s, If):
                if s.asm_only:
                    continue
                s = If(s.cond, strip(s.then), strip(s.els) if s.els else s.els)
            elif isinstance(s, While):
                s = While(s.cond, strip(s.body), s.exit_first)
            out.append(s)
        return out
    m = copy.copy(module)
    m.functions = [dataclasses.replace(f, body=strip(f.body)) for f in module.functions
                   if not f.asm_only]
    return m


@dataclass
class Module:
    """A set of functions emitted together as one library."""
    name: str
    functions: list = field(default_factory=list)
    tables: dict = field(default_factory=dict)   # name -> bytes
    fn_tables: dict = field(default_factory=dict)  # name -> [function names]
    # public API: name -> (doc, params, kind) where kind drives wrappers
    api: list = field(default_factory=list)

    def fn(self, name: str) -> Function:
        for f in self.functions:
            if f.name == name:
                return f
        raise KeyError(name)
