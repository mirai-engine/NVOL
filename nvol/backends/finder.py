"""memmem Finder glue for each backend (memchr::memmem API parity).

The Finder is: the needle + NVOL_MEMMEM_STATE_WORDS u64 words, each produced
written in one pass by the generated `nvol_memmem_init(needle, nend, out)`. Searching calls
the generated ifunc `nvol_memmem_find_raw(state, needle, nend, hay, hend)`.
`find_iter` reports non-overlapping matches like memchr's `FindIter`.
"""
from __future__ import annotations

from ..kernels.memmem import FIELDS, STATE_WORDS


def has(module) -> bool:
    return any(k == "memmem" for k, _, _ in module.api)


# ------------------------------------------------------------------ C
def c_header(module) -> list:
    if not has(module):
        return []
    fields = ", ".join(f"{i}={n}" for i, n in enumerate(FIELDS))
    return [
        f"#define NVOL_MEMMEM_STATE_WORDS {STATE_WORDS}",
        f"/* Substring finder (memchr::memmem::Finder). state words: {fields} */",
        "typedef struct nvol_memmem_finder { const uint8_t *needle; size_t nlen; uint64_t state[NVOL_MEMMEM_STATE_WORDS]; } nvol_memmem_finder;",
        "/* The needle is borrowed: it must outlive the finder. */",
        "NVOL_API void nvol_memmem_finder_init(nvol_memmem_finder *f, const uint8_t *needle, size_t nlen);",
        "/* Index of the first occurrence in hay[0..len), or SIZE_MAX. */",
        "NVOL_API size_t nvol_memmem_finder_find(const nvol_memmem_finder *f, const uint8_t *hay, size_t len);",
        "/* One-shot: index of needle in hay, or SIZE_MAX (memchr::memmem::find; RK below 64 bytes). */",
        "NVOL_API size_t nvol_memmem(const uint8_t *hay, size_t hlen, const uint8_t *needle, size_t nlen);",
        "",
    ]


def c_source(module, cpp: bool = False) -> list:
    if not has(module):
        return []
    api = "inline" if cpp else "NVOL_API"
    return [
        f"{api} void nvol_memmem_finder_init(nvol_memmem_finder *f, const uint8_t *needle, size_t nlen) {{",
        "    f->needle = needle; f->nlen = nlen;",
        "    nvol_memmem_init(needle, needle + nlen, (uint8_t *)f->state);",
        "}",
        f"{api} size_t nvol_memmem_finder_find(const nvol_memmem_finder *f, const uint8_t *hay, size_t len) {{",
        "    if (f->nlen == 0) return 0;",
        "    const uint8_t *p = nvol_memmem_find_raw((const uint8_t *)f->state, f->needle, f->needle + f->nlen, hay, hay + len);",
        "    return p ? (size_t)(p - hay) : SIZE_MAX;",
        "}",
        f"{api} size_t nvol_memmem(const uint8_t *hay, size_t hlen, const uint8_t *needle, size_t nlen) {{",
        "    if (nlen == 0) return 0;",
        "    if (hlen < 64) { const uint8_t *p = nvol_memmem_rk_oneshot(needle, needle + nlen, hay, hay + hlen); return p ? (size_t)(p - hay) : SIZE_MAX; }",
        "    nvol_memmem_finder f; nvol_memmem_finder_init(&f, needle, nlen);",
        "    return nvol_memmem_finder_find(&f, hay, hlen);",
        "}",
        "",
    ]


def cpp_wrappers(module) -> list:
    if not has(module):
        return []
    return [
        "namespace memmem {",
        "/* memchr::memmem::Finder: build once per needle, search many haystacks. Borrows the needle. */",
        "class Finder {",
        "public:",
        "    explicit Finder(std::span<const std::uint8_t> needle) { raw::nvol_memmem_finder_init(&f_, needle.data(), needle.size()); }",
        "    std::optional<std::size_t> find(std::span<const std::uint8_t> hay) const {",
        "        std::size_t r = raw::nvol_memmem_finder_find(&f_, hay.data(), hay.size());",
        "        return r == SIZE_MAX ? std::nullopt : std::optional<std::size_t>(r);",
        "    }",
        "    /* Calls f(index) for every non-overlapping match. */",
        "    template <class F> void for_each(std::span<const std::uint8_t> hay, F &&f) const {",
        "        std::size_t pos = 0, step = f_.nlen ? f_.nlen : 1;",
        "        while (pos <= hay.size()) {",
        "            std::size_t r = raw::nvol_memmem_finder_find(&f_, hay.data() + pos, hay.size() - pos);",
        "            if (r == SIZE_MAX) break;",
        "            f(pos + r); pos += r + step;",
        "        }",
        "    }",
        "private:",
        "    raw::nvol_memmem_finder f_;",
        "};",
        "inline std::optional<std::size_t> find(std::span<const std::uint8_t> hay, std::span<const std::uint8_t> needle) {",
        "    std::size_t r = raw::nvol_memmem(hay.data(), hay.size(), needle.data(), needle.size());",
        "    return r == SIZE_MAX ? std::nullopt : std::optional<std::size_t>(r);",
        "}",
        "}  // namespace memmem",
        "",
    ]


# ------------------------------------------------------------------ Rust
def rust(module) -> list:
    if not has(module):
        return []
    return [
        "/// Substring search (memchr::memmem equivalent).",
        "pub mod memmem {",
        "    use super::raw;",
        f"    pub const STATE_WORDS: usize = {STATE_WORDS};",
        "",
        "    /// Build once per needle, search many haystacks (memchr::memmem::Finder).",
        "    #[derive(Clone, Debug)]",
        "    pub struct Finder<'n> { needle: &'n [u8], state: [u64; STATE_WORDS] }",
        "",
        "    impl<'n> Finder<'n> {",
        "        pub fn new<B: ?Sized + AsRef<[u8]>>(needle: &'n B) -> Finder<'n> {",
        "            let needle = needle.as_ref();",
        "            let (p, e) = (needle.as_ptr(), unsafe { needle.as_ptr().add(needle.len()) });",
        "            let mut state = [0u64; STATE_WORDS];",
        "            unsafe { raw::nvol_memmem_init(p, e, state.as_mut_ptr() as *mut u8) };",
        "            Finder { needle, state }",
        "        }",
        "        #[inline]",
        "        pub fn needle(&self) -> &[u8] { self.needle }",
        "        #[inline]",
        "        pub fn find(&self, haystack: &[u8]) -> Option<usize> {",
        "            unsafe {",
        "                let (h, n) = (haystack.as_ptr(), self.needle.as_ptr());",
        "                let p = raw::nvol_memmem_find_raw(self.state.as_ptr() as *const u8, n, n.add(self.needle.len()), h, h.add(haystack.len()));",
        "                if p.is_null() { None } else { Some(p as usize - h as usize) }",
        "            }",
        "        }",
        "        /// Non-overlapping matches, like memchr::memmem::FindIter.",
        "        #[inline]",
        "        pub fn find_iter<'a, 'h>(&'a self, haystack: &'h [u8]) -> FindIter<'a, 'h, 'n> { FindIter { finder: self, haystack, pos: 0 } }",
        "    }",
        "",
        "    #[derive(Clone, Debug)]",
        "    pub struct FindIter<'a, 'h, 'n> { finder: &'a Finder<'n>, haystack: &'h [u8], pos: usize }",
        "    impl<'a, 'h, 'n> Iterator for FindIter<'a, 'h, 'n> {",
        "        type Item = usize;",
        "        #[inline]",
        "        fn next(&mut self) -> Option<usize> {",
        "            let hay = self.haystack.get(self.pos..)?;",
        "            let i = self.finder.find(hay)?;",
        "            let at = self.pos + i;",
        "            self.pos = at + core::cmp::max(1, self.finder.needle.len());",
        "            Some(at)",
        "        }",
        "    }",
        "",
        "    /// One-shot search (memchr::memmem::find): Rabin-Karp below 64 bytes, else a Finder.",
        "    #[inline]",
        "    pub fn find(haystack: &[u8], needle: &[u8]) -> Option<usize> {",
        "        if haystack.len() < 64 {",
        "            unsafe {",
        "                let (h, n) = (haystack.as_ptr(), needle.as_ptr());",
        "                let p = raw::nvol_memmem_rk_oneshot(n, n.add(needle.len()), h, h.add(haystack.len()));",
        "                return if p.is_null() { None } else { Some(p as usize - h as usize) };",
        "            }",
        "        }",
        "        Finder::new(needle).find(haystack)",
        "    }",
        "    /// One-shot non-overlapping matches (collects; use Finder::find_iter to stream).",
        "    pub fn find_iter(haystack: &[u8], needle: &[u8]) -> std::vec::Vec<usize> { Finder::new(needle).find_iter(haystack).collect() }",
        "}",
        "",
    ]


# ------------------------------------------------------------------ Python
def python(module) -> list:
    if not has(module):
        return []
    return [
        "",
        "_lib.nvol_memmem_init.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]",
        "_lib.nvol_memmem_init.restype = None",
        "_lib.nvol_memmem_find_raw.argtypes = [ctypes.c_void_p] * 5",
        "_lib.nvol_memmem_find_raw.restype = ctypes.c_void_p",
        f"STATE_WORDS = {STATE_WORDS}",
        "",
        "",
        "class Finder:",
        '    """Build once per needle, search many haystacks (memchr::memmem::Finder)."""',
        "",
        "    def __init__(self, needle):",
        "        self.needle = bytes(needle)",
        "        self._np, self._nl, self._keep = _buf(self.needle)",
        "        st = (ctypes.c_uint64 * STATE_WORDS)()",
        "        _lib.nvol_memmem_init(self._np, self._np + self._nl, ctypes.addressof(st))",
        "        self._state = st",
        "",
        "    def find(self, hay):",
        "        if not self._nl:",
        "            return 0",
        "        p, ln, _keep = _buf(hay)",
        "        r = _lib.nvol_memmem_find_raw(ctypes.addressof(self._state), self._np, self._np + self._nl, p, p + ln)",
        "        return None if not r else r - p",
        "",
        "    def find_iter(self, hay):",
        '        """Yield non-overlapping match positions."""',
        "        p, ln, _keep = _buf(hay)",
        "        if not self._nl:",
        "            yield from range(ln + 1)",
        "            return",
        "        step = self._nl",
        "        pos = 0",
        "        while pos <= ln:",
        "            r = _lib.nvol_memmem_find_raw(ctypes.addressof(self._state), self._np, self._np + self._nl, p + pos, p + ln)",
        "            if not r:",
        "                return",
        "            at = r - p",
        "            yield at",
        "            pos = at + step",
        "",
        "",
        "_lib.nvol_memmem.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]",
        "_lib.nvol_memmem.restype = ctypes.c_size_t",
        "",
        "",
        "def memmem(hay, needle):",
        '    """One-shot: index of needle in hay, or None (RK below 64 bytes, like memchr)."""',
        "    if not len(needle):",
        "        return 0",
        "    p, ln, _k1 = _buf(hay)",
        "    q, nl, _k2 = _buf(needle)",
        "    r = _lib.nvol_memmem(p, ln, q, nl)",
        "    return None if r == _NONE else r",
        "",
    ]
