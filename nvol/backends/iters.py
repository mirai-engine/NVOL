"""Iterator glue (memchr_iter & co.) for each backend.

API parity target: memchr 2.8.3 `Memchr`, `Memchr2`, `Memchr3` -- forward and
backward (DoubleEndedIterator) over match positions, `size_hint`, and a
`count()` specialisation that calls the count kernel (ripgrep's
`grep-searcher::lines::count` relies on it).

The glue is written per backend for now (not in the IR); see docs/PENDING.md.
"""
from __future__ import annotations


def pairs(module):
    """[(n, fwd_raw, rev_raw, count_raw_or_None)] from the module's API."""
    names = {name for _, name, _ in module.api}
    out = []
    for n, base in ((1, "memchr"), (2, "memchr2"), (3, "memchr3")):
        fwd, rev = f"nvol_{base}", f"nvol_{base.replace('memchr', 'memrchr')}"
        if fwd in names and rev in names:
            out.append((n, fwd, rev, "nvol_count" if n == 1 and "nvol_count" in names else None))
    return out


# ------------------------------------------------------------------ Rust
def rust(module) -> list:
    L = []
    for n, fwd, rev, cnt in pairs(module):
        sfx = "" if n == 1 else str(n)
        ty = f"Memchr{sfx}"
        fields = "".join(f"n{i + 1}: u8, " for i in range(n))
        args = "".join(f"self.n{i + 1}, " for i in range(n))
        params = "".join(f"n{i + 1}: u8, " for i in range(n))
        inits = "".join(f"n{i + 1}, " for i in range(n))
        L += [
            f"/// Iterator over match positions (memchr::{ty} equivalent). Double-ended.",
            "#[derive(Clone, Debug)]",
            f"pub struct {ty}<'h> {{ {fields}base: *const u8, lo: *const u8, hi: *const u8, _h: core::marker::PhantomData<&'h [u8]> }}",
            f"impl<'h> {ty}<'h> {{",
            "    #[inline]",
            f"    pub fn new({params}hay: &'h [u8]) -> Self {{",
            "        let p = hay.as_ptr();",
            f"        Self {{ {inits}base: p, lo: p, hi: unsafe {{ p.add(hay.len()) }}, _h: core::marker::PhantomData }}",
            "    }",
            "}",
            f"impl<'h> Iterator for {ty}<'h> {{",
            "    type Item = usize;",
            "    #[inline]",
            "    fn next(&mut self) -> Option<usize> {",
            "        unsafe {",
            f"            let at = raw::{fwd}_raw({args}self.lo, self.hi);",
            "            if at.is_null() { self.lo = self.hi; return None; }",
            "            self.lo = at.add(1);",
            "            Some(at as usize - self.base as usize)",
            "        }",
            "    }",
            "    #[inline]",
            "    fn size_hint(&self) -> (usize, Option<usize>) { (0, Some(self.hi as usize - self.lo as usize)) }",
        ]
        if cnt:
            L += [
                "    #[inline]",
                f"    fn count(self) -> usize {{ unsafe {{ raw::{cnt}_raw(self.n1, self.lo, self.hi) }} }}",
            ]
        L += [
            "}",
            f"impl<'h> DoubleEndedIterator for {ty}<'h> {{",
            "    #[inline]",
            "    fn next_back(&mut self) -> Option<usize> {",
            "        unsafe {",
            f"            let at = raw::{rev}_raw({args}self.lo, self.hi);",
            "            if at.is_null() { self.hi = self.lo; return None; }",
            "            self.hi = at;",
            "            Some(at as usize - self.base as usize)",
            "        }",
            "    }",
            "}",
            f"impl<'h> core::iter::FusedIterator for {ty}<'h> {{}}",
            f"#[inline] pub fn memchr{sfx}_iter<'h>({params}hay: &'h [u8]) -> {ty}<'h> {{ {ty}::new({inits}hay) }}",
            f"#[inline] pub fn memrchr{sfx}_iter<'h>({params}hay: &'h [u8]) -> core::iter::Rev<{ty}<'h>> {{ {ty}::new({inits}hay).rev() }}",
            "",
        ]
    return L


# ------------------------------------------------------------------ C
def c_header(module) -> list:
    if not pairs(module):
        return []
    return [
        "/* Iterator over match positions of 1..3 needles (memchr_iter & co.).",
        " *   nvol_memchr_iter it; nvol_memchr_iter_init(&it, 2, 'a', 'b', 0, hay, len);",
        " *   for (size_t i; (i = nvol_memchr_iter_next(&it)) != SIZE_MAX;) ...",
        " * next_back walks from the end; both ends meet without overlap. */",
        "typedef struct nvol_memchr_iter { const uint8_t *base, *lo, *hi; uint8_t n1, n2, n3; int k; } nvol_memchr_iter;",
        "NVOL_API void nvol_memchr_iter_init(nvol_memchr_iter *it, int k, uint8_t n1, uint8_t n2, uint8_t n3, const uint8_t *hay, size_t len);",
        "NVOL_API size_t nvol_memchr_iter_next(nvol_memchr_iter *it);",
        "NVOL_API size_t nvol_memchr_iter_next_back(nvol_memchr_iter *it);",
        "/* Number of positions left (consumes the iterator; uses nvol_count for k == 1). */",
        "NVOL_API size_t nvol_memchr_iter_count(nvol_memchr_iter *it);",
        "",
    ]


def c_source(module, cpp: bool = False) -> list:
    ps = {n: (f, r, c) for n, f, r, c in pairs(module)}
    if not ps:
        return []
    api = "inline" if cpp else "NVOL_API"
    def call(n, which, p, ln):
        f, r, _ = ps[n]
        fn = f if which == "f" else r
        ns = ", ".join(["it->n1", "it->n2", "it->n3"][:n])
        return f"{fn}_raw({ns}, {p}, {ln})"
    def switch(which):
        out = ["    switch (it->k) {"]
        for n in sorted(ps):
            out.append(f"    case {n}: r = {call(n, which, 'it->lo', 'it->hi')}; break;")
        out.append("    default: r = NULL;")
        out.append("    }")
        return out
    L = [f"{api} void nvol_memchr_iter_init(nvol_memchr_iter *it, int k, uint8_t n1, uint8_t n2, uint8_t n3, const uint8_t *hay, size_t len) {{",
         "    it->base = it->lo = hay; it->hi = hay + len; it->n1 = n1; it->n2 = n2; it->n3 = n3; it->k = k;",
         "}",
         f"{api} size_t nvol_memchr_iter_next(nvol_memchr_iter *it) {{",
         "    const uint8_t *r;"]
    L += switch("f")
    L += ["    if (r == NULL) { it->lo = it->hi; return SIZE_MAX; }",
          "    it->lo = r + 1;",
          "    return (size_t)(r - it->base);",
          "}",
          f"{api} size_t nvol_memchr_iter_next_back(nvol_memchr_iter *it) {{",
          "    const uint8_t *r;"]
    L += switch("r")
    L += ["    if (r == NULL) { it->hi = it->lo; return SIZE_MAX; }",
          "    it->hi = r;",
          "    return (size_t)(r - it->base);",
          "}",
          f"{api} size_t nvol_memchr_iter_count(nvol_memchr_iter *it) {{",
          "    size_t n = 0;"]
    if 1 in ps and ps[1][2]:
        L += [f"    if (it->k == 1) {{ n = {ps[1][2]}_raw(it->n1, it->lo, it->hi); it->lo = it->hi; return n; }}"]
    L += ["    while (nvol_memchr_iter_next(it) != SIZE_MAX) n++;",
          "    return n;",
          "}",
          ""]
    return L


# ------------------------------------------------------------------ C++
def cpp_wrappers(module) -> list:
    if not pairs(module):
        return []
    return [
        "/* Range-for friendly view over match positions: for (size_t i : nvol::memchr_iter('\\n', hay)) */",
        "class memchr_iter {",
        "public:",
        "    struct sentinel {};",
        "    class iterator {",
        "    public:",
        "        explicit iterator(raw::nvol_memchr_iter *it) : it_(it), cur_(raw::nvol_memchr_iter_next(it)) {}",
        "        std::size_t operator*() const { return cur_; }",
        "        iterator &operator++() { cur_ = raw::nvol_memchr_iter_next(it_); return *this; }",
        "        bool operator!=(sentinel) const { return cur_ != SIZE_MAX; }",
        "    private:",
        "        raw::nvol_memchr_iter *it_; std::size_t cur_;",
        "    };",
        "    memchr_iter(std::uint8_t n1, std::span<const std::uint8_t> hay) { raw::nvol_memchr_iter_init(&it_, 1, n1, 0, 0, hay.data(), hay.size()); }",
        "    memchr_iter(std::uint8_t n1, std::uint8_t n2, std::span<const std::uint8_t> hay) { raw::nvol_memchr_iter_init(&it_, 2, n1, n2, 0, hay.data(), hay.size()); }",
        "    memchr_iter(std::uint8_t n1, std::uint8_t n2, std::uint8_t n3, std::span<const std::uint8_t> hay) { raw::nvol_memchr_iter_init(&it_, 3, n1, n2, n3, hay.data(), hay.size()); }",
        "    iterator begin() { return iterator(&it_); }",
        "    sentinel end() const { return {}; }",
        "    std::optional<std::size_t> next() { std::size_t r = raw::nvol_memchr_iter_next(&it_); return r == SIZE_MAX ? std::nullopt : std::optional<std::size_t>(r); }",
        "    std::optional<std::size_t> next_back() { std::size_t r = raw::nvol_memchr_iter_next_back(&it_); return r == SIZE_MAX ? std::nullopt : std::optional<std::size_t>(r); }",
        "    std::size_t count() { return raw::nvol_memchr_iter_count(&it_); }",
        "private:",
        "    raw::nvol_memchr_iter it_;",
        "};",
        "",
    ]


# ------------------------------------------------------------------ Python
def python(module) -> list:
    ps = pairs(module)
    if not ps:
        return []
    L = []
    for n, fwd, rev, _ in ps:
        sfx = "" if n == 1 else str(n)
        params = "".join(f"n{i + 1}, " for i in range(n))
        L += [
            "",
            f"def memchr{sfx}_iter({params}hay):",
            f'    """Yield every match position, front to back (memchr::memchr{sfx}_iter)."""',
            "    p, ln, _keep = _buf(hay)",
            "    at = 0",
            "    while at < ln:",
            f"        r = _lib.{fwd}({params}p + at, ln - at)",
            "        if r == _NONE:",
            "            return",
            "        yield at + r",
            "        at += r + 1",
            "",
            "",
            f"def memrchr{sfx}_iter({params}hay):",
            f'    """Yield every match position, back to front (memchr::memrchr{sfx}_iter)."""',
            "    p, ln, _keep = _buf(hay)",
            "    while ln > 0:",
            f"        r = _lib.{rev}({params}p, ln)",
            "        if r == _NONE:",
            "            return",
            "        yield r",
            "        ln = r",
            "",
        ]
    return L
