//! C ABI over NVOL-Rust and the reference crates, so harness/odin/xbench can
//! time every implementation in one process, interleaved. Separate processes
//! differ by several percent on the same code (see docs/benchmarks), which is
//! larger than the differences being measured.
use aho_corasick::packed::{Config, MatchKind, Searcher};
use nvol_harness::nvol;
use std::sync::OnceLock;

pub const PATS: [&str; 16] = ["Moriarty", "Lestrade", "Mycroft", "Hudson", "Adler", "Baskerville", "Gregson", "Hopkins",
    "Bradstreet", "Jones", "Wiggins", "Morstan", "Stamford", "Barrymore", "Musgrave", "Milverton"];

struct State {
    fl: nvol::memmem::Finder<'static>,
    fm: memchr::memmem::Finder<'static>,
    tl: nvol::packed::Searcher,
    ta: Searcher,
    bc: [u8; nvol::raw::NVOL_BYTESET_CLASS_BYTES],   // byteset class "&<>|" (k-chain; absent from the text: one full scan)
    br: [u8; nvol::raw::NVOL_BYTESET_CLASS_BYTES],   // byteset class 0x00-0x1F, '"', '\\' (range + 2 extras, the JSON stop set)
}

fn class(set: &[u8]) -> [u8; nvol::raw::NVOL_BYTESET_CLASS_BYTES] {
    let mut c = [0u8; nvol::raw::NVOL_BYTESET_CLASS_BYTES];
    unsafe { nvol::raw::nvol_byteset_init(set.as_ptr(), set.len(), c.as_mut_ptr()) };
    c
}

/// Every position of the class in `h`, xor-folded (one kernel call per match, like memchr_iter).
fn byteset_iter(c: &[u8; nvol::raw::NVOL_BYTESET_CLASS_BYTES], h: &[u8]) -> usize {
    let (base, end) = (h.as_ptr(), unsafe { h.as_ptr().add(h.len()) });
    let (mut p, mut acc) = (base, 0usize);
    loop {
        let r = unsafe { nvol::raw::nvol_find_byteset_raw(c.as_ptr(), p, end) };
        if r.is_null() { return acc; }
        acc ^= r as usize - base as usize;
        p = unsafe { r.add(1) };
    }
}

fn st() -> &'static State {
    static S: OnceLock<State> = OnceLock::new();
    S.get_or_init(|| State {
        fl: nvol::memmem::Finder::new(b"Moriarty"),
        fm: memchr::memmem::Finder::new(b"Moriarty"),
        tl: nvol::packed::Searcher::new(PATS).unwrap(),
        ta: Config::new().match_kind(MatchKind::LeftmostFirst).builder().extend(PATS).build().unwrap(),
        bc: class(b"&<>|"),
        br: class(&(0u8..0x20).chain([b'"', b'\\']).collect::<Vec<u8>>()),
    })
}

/// Warm-up: build the finders/searchers before timing.
#[no_mangle]
pub extern "C" fn xb_init() { st(); }

/// Same cases, in the same order, as the Odin side. `imp`: 0 = NVOL-Rust, 1 = reference crate.
macro_rules! case {
    ($name:ident, $nvol:expr, $refr:expr) => {
        #[no_mangle]
        pub unsafe extern "C" fn $name(imp: u32, p: *const u8, len: usize) -> usize {
            let h = std::slice::from_raw_parts(p, len);
            let s = st();
            let _ = s;
            if imp == 0 { ($nvol)(h, s) } else { ($refr)(h, s) }
        }
    };
}
case!(xb_memchr_absent, |h: &[u8], _: &State| nvol::memchr(b'Z', h).unwrap_or(0),
      |h: &[u8], _: &State| memchr::memchr(b'Z', h).unwrap_or(0));
case!(xb_memchr3_absent, |h: &[u8], _: &State| nvol::memchr3(b'Z', b'{', b'~', h).unwrap_or(0),
      |h: &[u8], _: &State| memchr::memchr3(b'Z', b'{', b'~', h).unwrap_or(0));
case!(xb_count_nl, |h: &[u8], _: &State| nvol::count(b'\n', h),
      |h: &[u8], _: &State| memchr::memchr_iter(b'\n', h).count());
case!(xb_memchr_iter_nl, |h: &[u8], _: &State| nvol::memchr_iter(b'\n', h).fold(0, |a, i| a ^ i),
      |h: &[u8], _: &State| memchr::memchr_iter(b'\n', h).fold(0, |a, i| a ^ i));
case!(xb_memmem_absent, |h: &[u8], s: &State| s.fl.find(h).unwrap_or(0),
      |h: &[u8], s: &State| s.fm.find(h).unwrap_or(0));
case!(xb_teddy16_absent, |h: &[u8], s: &State| s.tl.find(h).map_or(0, |m| m.start),
      |h: &[u8], s: &State| s.ta.find(h).map_or(0, |m| m.start()));
// No reference crate has a byte-class search: `imp` 1 runs NVOL-Rust too.
case!(xb_byteset_absent, |h: &[u8], s: &State| byteset_iter(&s.bc, h), |h: &[u8], s: &State| byteset_iter(&s.bc, h));
case!(xb_byteset_iter_range, |h: &[u8], s: &State| byteset_iter(&s.br, h), |h: &[u8], s: &State| byteset_iter(&s.br, h));
