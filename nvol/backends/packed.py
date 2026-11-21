"""Teddy (aho_corasick::packed::Searcher) glue for each backend.

The language side owns two buffers: the patterns blob (layout in
kernels/teddy.py) and NVOL_TEDDY_STATE_BYTES of state filled by the generated
`nvol_teddy_init`. `build` returns "none" exactly when aho-corasick's packed
builder does. Searches call the generated `nvol_teddy_find_raw`, which returns
(start - hstart) << 8 | pattern id. Only LeftmostFirst is provided: it is the
match kind regex-automata (ripgrep) uses.
"""
from __future__ import annotations

from ..kernels.teddy import STATE_BYTES


def has(module) -> bool:
    return any(k == "teddy" for k, _, _ in module.api)


# ------------------------------------------------------------------ C
def c_header(module) -> list:
    if not has(module):
        return []
    return [
        f"#define NVOL_TEDDY_STATE_BYTES {STATE_BYTES}",
        "/* Packed multi-literal searcher (aho_corasick::packed::Searcher, LeftmostFirst). */",
        "typedef struct nvol_teddy { const uint8_t *blob; uint64_t state[NVOL_TEDDY_STATE_BYTES / 8]; } nvol_teddy;",
        "typedef struct nvol_teddy_match { size_t pattern, start, end; } nvol_teddy_match;",
        "/* Bytes needed for the patterns blob of n patterns. */",
        "NVOL_API size_t nvol_teddy_blob_size(const uint8_t *const *pats, const size_t *lens, size_t n);",
        "/* Serialize patterns into out[nvol_teddy_blob_size(..)] (8-byte aligned recommended). */",
        "NVOL_API void nvol_teddy_blob_write(uint8_t *out, const uint8_t *const *pats, const size_t *lens, size_t n);",
        "/* Build from a blob (borrowed; must outlive the searcher). Returns 0 when Teddy",
        " * cannot be used for these patterns on this CPU (the caller should fall back). */",
        "NVOL_API int nvol_teddy_build(nvol_teddy *t, const uint8_t *blob);",
        "/* Leftmost-first match within hay[start..end); returns 0 if none. */",
        "NVOL_API int nvol_teddy_find_in(const nvol_teddy *t, const uint8_t *hay, size_t start, size_t end, nvol_teddy_match *m);",
        "",
    ]


def c_source(module, cpp: bool = False) -> list:
    if not has(module):
        return []
    api = "inline" if cpp else "NVOL_API"
    return [
        f"{api} size_t nvol_teddy_blob_size(const uint8_t *const *pats, const size_t *lens, size_t n) {{",
        "    size_t total = 8 + 16 * n;",
        "    for (size_t i = 0; i < n; i++) total += lens[i];",
        "    (void)pats; return total;",
        "}",
        f"{api} void nvol_teddy_blob_write(uint8_t *out, const uint8_t *const *pats, const size_t *lens, size_t n) {{",
        "    uint64_t w = n; size_t pos = 8 + 16 * n;",
        "    NVOL_MEMCPY(out, &w, 8);",
        "    for (size_t i = 0; i < n; i++) {",
        "        uint64_t off = pos, len = lens[i];",
        "        NVOL_MEMCPY(out + 8 + 16 * i, &off, 8); NVOL_MEMCPY(out + 16 + 16 * i, &len, 8);",
        "        if (len) NVOL_MEMCPY(out + pos, pats[i], len);",
        "        pos += len;",
        "    }",
        "}",
        f"{api} int nvol_teddy_build(nvol_teddy *t, const uint8_t *blob) {{",
        "    t->blob = blob;",
        "    nvol_teddy_init(blob, (uint8_t *)t->state);",
        "    return t->state[0] != 0;",
        "}",
        f"{api} int nvol_teddy_find_in(const nvol_teddy *t, const uint8_t *hay, size_t start, size_t end, nvol_teddy_match *m) {{",
        "    size_t r = nvol_teddy_find_raw((const uint8_t *)t->state, t->blob, hay + start, hay + end);",
        "    if (r == SIZE_MAX) return 0;",
        "    uint64_t len; NVOL_MEMCPY(&len, t->blob + 16 + 16 * (r & 0xFF), 8);",
        "    m->pattern = r & 0xFF; m->start = start + (r >> 8); m->end = m->start + (size_t)len;",
        "    return 1;",
        "}",
        "",
    ]


def cpp_wrappers(module) -> list:
    if not has(module):
        return []
    return [
        "namespace packed {",
        "struct Match { std::size_t pattern, start, end; };",
        "/* aho_corasick::packed::Searcher (LeftmostFirst). Owns copies of the patterns. */",
        "class Searcher {",
        "public:",
        "    template <class Range> static std::optional<Searcher> build(const Range &patterns) {",
        "        std::vector<const std::uint8_t *> ptrs; std::vector<std::size_t> lens;",
        "        for (const auto &p : patterns) {",
        "            ptrs.push_back(reinterpret_cast<const std::uint8_t *>(std::data(p)));",
        "            lens.push_back(std::size(p) * sizeof(*std::data(p)));",
        "        }",
        "        Searcher s;",
        "        s.blob_.resize((raw::nvol_teddy_blob_size(ptrs.data(), lens.data(), ptrs.size()) + 7) / 8);",
        "        auto *b = reinterpret_cast<std::uint8_t *>(s.blob_.data());",
        "        raw::nvol_teddy_blob_write(b, ptrs.data(), lens.data(), ptrs.size());",
        "        if (!raw::nvol_teddy_build(&s.t_, b)) return std::nullopt;",
        "        return s;",
        "    }",
        "    Searcher(const Searcher &o) : blob_(o.blob_), t_(o.t_) { t_.blob = reinterpret_cast<const std::uint8_t *>(blob_.data()); }",
        "    Searcher(Searcher &&o) noexcept : blob_(std::move(o.blob_)), t_(o.t_) { t_.blob = reinterpret_cast<const std::uint8_t *>(blob_.data()); }",
        "    std::optional<Match> find_in(std::span<const std::uint8_t> hay, std::size_t start, std::size_t end) const {",
        "        raw::nvol_teddy_match m;",
        "        if (!raw::nvol_teddy_find_in(&t_, hay.data(), start, end, &m)) return std::nullopt;",
        "        return Match{m.pattern, m.start, m.end};",
        "    }",
        "    std::optional<Match> find(std::span<const std::uint8_t> hay) const { return find_in(hay, 0, hay.size()); }",
        "    /* Calls f(Match) for every non-overlapping leftmost-first match. */",
        "    template <class F> void for_each(std::span<const std::uint8_t> hay, F &&f) const {",
        "        std::size_t pos = 0;",
        "        while (auto m = find_in(hay, pos, hay.size())) { f(*m); pos = m->end; }",
        "    }",
        "private:",
        "    Searcher() = default;",
        "    std::vector<std::uint64_t> blob_;",
        "    raw::nvol_teddy t_{};",
        "};",
        "}  // namespace packed",
        "",
    ]


# ------------------------------------------------------------------ Rust
def rust(module) -> list:
    if not has(module):
        return []
    words = STATE_BYTES // 8
    return [
        "/// Packed multi-literal search (aho_corasick::packed equivalent, LeftmostFirst).",
        "pub mod packed {",
        "    use super::raw;",
        "",
        "    #[derive(Clone, Copy, Debug, PartialEq, Eq)]",
        "    pub struct Match { pub pattern: usize, pub start: usize, pub end: usize }",
        "",
        "    #[derive(Clone, Debug)]",
        f"    pub struct Searcher {{ blob: std::vec::Vec<u64>, state: std::boxed::Box<[u64; {words}]> }}",
        "",
        "    impl Searcher {",
        "        /// None when Teddy can't be used (same cases as aho-corasick's packed builder).",
        "        pub fn new<I, P>(patterns: I) -> Option<Searcher> where I: IntoIterator<Item = P>, P: AsRef<[u8]> {",
        "            let pats: std::vec::Vec<P> = patterns.into_iter().collect();",
        "            let n = pats.len();",
        "            let total = 8 + 16 * n + pats.iter().map(|p| p.as_ref().len()).sum::<usize>();",
        "            let mut blob = std::vec![0u64; (total + 7) / 8];",
        "            {",
        "                let b = unsafe { core::slice::from_raw_parts_mut(blob.as_mut_ptr() as *mut u8, blob.len() * 8) };",
        "                b[..8].copy_from_slice(&(n as u64).to_le_bytes());",
        "                let mut pos = 8 + 16 * n;",
        "                for (i, p) in pats.iter().enumerate() {",
        "                    let p = p.as_ref();",
        "                    b[8 + 16 * i..16 + 16 * i].copy_from_slice(&(pos as u64).to_le_bytes());",
        "                    b[16 + 16 * i..24 + 16 * i].copy_from_slice(&(p.len() as u64).to_le_bytes());",
        "                    b[pos..pos + p.len()].copy_from_slice(p);",
        "                    pos += p.len();",
        "                }",
        "            }",
        f"            let mut state = std::boxed::Box::new([0u64; {words}]);",
        "            unsafe { raw::nvol_teddy_init(blob.as_ptr() as *const u8, state.as_mut_ptr() as *mut u8) };",
        "            if state[0] == 0 { None } else { Some(Searcher { blob, state }) }",
        "        }",
        "        #[inline]",
        "        pub fn find(&self, haystack: &[u8]) -> Option<Match> { self.find_in(haystack, 0..haystack.len()) }",
        "        /// Like aho_corasick::packed::Searcher::find_in (the regex-automata prefilter entry point).",
        "        #[inline]",
        "        pub fn find_in(&self, haystack: &[u8], span: core::ops::Range<usize>) -> Option<Match> {",
        "            assert!(span.start <= span.end && span.end <= haystack.len());",
        "            unsafe {",
        "                let h = haystack.as_ptr();",
        "                let b = self.blob.as_ptr() as *const u8;",
        "                let r = raw::nvol_teddy_find_raw(self.state.as_ptr() as *const u8, b, h.add(span.start), h.add(span.end));",
        "                if r == usize::MAX { return None; }",
        "                let pattern = r & 0xFF;",
        "                let len = (b.add(16 + 16 * pattern) as *const u64).read_unaligned() as usize;",
        "                let start = span.start + (r >> 8);",
        "                Some(Match { pattern, start, end: start + len })",
        "            }",
        "        }",
        "        /// Non-overlapping leftmost-first matches.",
        "        pub fn find_iter<'s, 'h>(&'s self, haystack: &'h [u8]) -> FindIter<'s, 'h> { FindIter { s: self, haystack, pos: 0 } }",
        "    }",
        "",
        "    pub struct FindIter<'s, 'h> { s: &'s Searcher, haystack: &'h [u8], pos: usize }",
        "    impl<'s, 'h> Iterator for FindIter<'s, 'h> {",
        "        type Item = Match;",
        "        #[inline]",
        "        fn next(&mut self) -> Option<Match> {",
        "            if self.pos > self.haystack.len() { return None; }",
        "            let m = self.s.find_in(self.haystack, self.pos..self.haystack.len())?;",
        "            self.pos = m.end;",
        "            Some(m)",
        "        }",
        "    }",
        "}",
        "",
    ]


# ------------------------------------------------------------------ Python
def python(module) -> list:
    if not has(module):
        return []
    return [
        "",
        "_lib.nvol_teddy_init.argtypes = [ctypes.c_void_p, ctypes.c_void_p]",
        "_lib.nvol_teddy_init.restype = None",
        "_lib.nvol_teddy_find_raw.argtypes = [ctypes.c_void_p] * 4",
        "_lib.nvol_teddy_find_raw.restype = ctypes.c_size_t",
        f"TEDDY_STATE_BYTES = {STATE_BYTES}",
        "",
        "",
        "class PackedSearcher:",
        '    """aho_corasick.packed.Searcher (LeftmostFirst). Use PackedSearcher.build(patterns)."""',
        "",
        "    @classmethod",
        "    def build(cls, patterns):",
        '        """Return a searcher, or None when Teddy cannot be used (like aho-corasick)."""',
        "        pats = [bytes(p) for p in patterns]",
        "        n = len(pats)",
        "        head = 8 + 16 * n",
        "        table, pos = [], head",
        "        for p in pats:",
        "            table.append(pos.to_bytes(8, 'little') + len(p).to_bytes(8, 'little'))",
        "            pos += len(p)",
        "        blob = n.to_bytes(8, 'little') + b''.join(table) + b''.join(pats)",
        "        self = cls.__new__(cls)",
        "        self._blob = ctypes.create_string_buffer(blob, len(blob) + 8)",
        "        self._state = (ctypes.c_uint8 * TEDDY_STATE_BYTES)()",
        "        _lib.nvol_teddy_init(ctypes.addressof(self._blob), ctypes.addressof(self._state))",
        "        if not int.from_bytes(bytes(self._state[:8]), 'little'):",
        "            return None",
        "        self.patterns = pats",
        "        return self",
        "",
        "    def find(self, hay, start=0, end=None):",
        '        """(pattern id, start, end) of the leftmost-first match in hay[start:end], or None."""',
        "        p, ln, _keep = _buf(hay)",
        "        end = ln if end is None else end",
        "        r = _lib.nvol_teddy_find_raw(ctypes.addressof(self._state), ctypes.addressof(self._blob), p + start, p + end)",
        "        if r == _NONE:",
        "            return None",
        "        pid = r & 0xFF",
        "        s = start + (r >> 8)",
        "        return pid, s, s + len(self.patterns[pid])",
        "",
        "    def find_iter(self, hay):",
        "        pos = 0",
        "        while True:",
        "            m = self.find(hay, pos)",
        "            if m is None:",
        "                return",
        "            yield m",
        "            pos = m[2]",
        "",
    ]
