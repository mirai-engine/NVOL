//! Rust side of the cross-language benchmark (pair: harness/odin/bench/main.odin):
//! same text, same cases, same measurement. Prints NVOL-Rust and the reference crates.
use aho_corasick::packed::{Config, MatchKind};
use nvol_harness::nvol;
use std::hint::black_box;
use std::time::Instant;

fn text(n: usize) -> Vec<u8> {
    let words: [&[u8]; 16] = [b"the ", b"quick ", b"brown ", b"fox ", b"jumps ", b"over ", b"lazy ", b"dog ",
        b"Sherlock ", b"Holmes ", b"said ", b"to ", b"Watson ", b"and ", b"then\n", b"of "];
    let (mut v, mut x) = (Vec::with_capacity(n + 16), 0x2545F4914F6CDD1Du64);
    while v.len() < n { x ^= x << 13; x ^= x >> 7; x ^= x << 17; v.extend_from_slice(words[(x % 16) as usize]); }
    v.truncate(n);
    v
}

fn best(f: &mut dyn FnMut(&[u8]) -> usize, h: &[u8]) -> f64 {
    let iters = ((64 << 20) / h.len()).max(1);
    let mut b = f64::MAX;
    for _ in 0..9 {
        let t = Instant::now();
        for _ in 0..iters { black_box(f(black_box(h))); }
        b = b.min(t.elapsed().as_secs_f64() / iters as f64);
    }
    h.len() as f64 / b / 1e9
}

fn main() {
    let pats = ["Moriarty", "Lestrade", "Mycroft", "Hudson", "Adler", "Baskerville", "Gregson", "Hopkins",
        "Bradstreet", "Jones", "Wiggins", "Morstan", "Stamford", "Barrymore", "Musgrave", "Milverton"];
    let fl = nvol::memmem::Finder::new(b"Moriarty");
    let fm = memchr::memmem::Finder::new(b"Moriarty");
    let tl = nvol::packed::Searcher::new(pats).unwrap();
    let ta = Config::new().match_kind(MatchKind::LeftmostFirst).builder().extend(pats).build().unwrap();
    for size in [64usize << 10, 1 << 20] {
        let h = text(size);
        let r = |name: &str, a: f64, b: f64| println!("size={size} {name}  nvol-rust={a:.2}  reference={b:.2}");
        r("memchr_absent", best(&mut |h| nvol::memchr(b'Z', h).unwrap_or(0), &h), best(&mut |h| memchr::memchr(b'Z', h).unwrap_or(0), &h));
        r("memchr3_absent", best(&mut |h| nvol::memchr3(b'Z', b'{', b'~', h).unwrap_or(0), &h), best(&mut |h| memchr::memchr3(b'Z', b'{', b'~', h).unwrap_or(0), &h));
        r("count_nl", best(&mut |h| nvol::count(b'\n', h), &h), best(&mut |h| memchr::memchr_iter(b'\n', h).count(), &h));
        r("memchr_iter_nl", best(&mut |h| nvol::memchr_iter(b'\n', h).fold(0, |a, i| a ^ i), &h), best(&mut |h| memchr::memchr_iter(b'\n', h).fold(0, |a, i| a ^ i), &h));
        r("memmem_absent", best(&mut |h| fl.find(h).unwrap_or(0), &h), best(&mut |h| fm.find(h).unwrap_or(0), &h));
        r("teddy16_absent", best(&mut |h| tl.find(h).map_or(0, |m| m.start), &h), best(&mut |h| ta.find(h).map_or(0, |m| m.start()), &h));
    }
}
