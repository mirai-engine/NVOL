//! Interleaved A/B for Teddy hot paths (per-call cost vs verification cost).
use aho_corasick::packed::{Config, MatchKind};
use nvol_harness::nvol;
use std::hint::black_box;
use std::time::Instant;

fn t(f: &mut dyn FnMut() -> usize) -> f64 {
    let s = Instant::now();
    for _ in 0..10 { black_box(f()); }
    s.elapsed().as_secs_f64() / 10.0
}

fn main() {
    let words: [&[u8]; 8] = [b"alpha ", b"bravo ", b"charlie ", b"delta ", b"echo ", b"foxtrot ", b"golf ", b"hotel "];
    let mut x = 88172645463325252u64;
    let mut gen = |len: usize, every: usize, needle: &[u8]| {
        let mut v = Vec::with_capacity(len + 16);
        let mut k = 0;
        while v.len() < len {
            x ^= x << 13; x ^= x >> 7; x ^= x << 17;
            k += 1;
            if every > 0 && k % every == 0 { v.extend_from_slice(needle); v.push(b' '); }
            else { v.extend_from_slice(words[(x % 8) as usize]); }
        }
        v.truncate(len);
        v
    };
    let pats: Vec<&[u8]> = vec![b"Sherlock", b"Holmes", b"Watson"];
    // exactly tedbench's text generator
    let tb_text = |len: usize| {
        let words: [&[u8]; 16] = [b"the ", b"quick ", b"brown ", b"fox ", b"jumps ", b"over ", b"lazy ", b"dog ",
            b"Sherlock ", b"Holmes ", b"said ", b"to ", b"Watson ", b"and ", b"then\n", b"of "];
        let (mut v, mut x) = (Vec::with_capacity(len + 16), 0x9E3779B97F4A7C15u64);
        while v.len() < len { x ^= x << 13; x ^= x >> 7; x ^= x << 17; v.extend_from_slice(words[(x % 16) as usize]); }
        v.truncate(len);
        v
    };
    // (name, haystack): dense matches (per-call cost), sparse matches, no match but
    // candidate-heavy (verification cost: "Sherlack" shares Teddy nybbles with "Sherlock")
    let cases = vec![
        ("dense: match every 2 words", gen(1 << 20, 2, b"Holmes")),
        ("medium: match every 16 words", gen(1 << 20, 16, b"Holmes")),
        ("sparse: match every 512 words", gen(1 << 20, 512, b"Watson")),
        ("false candidates only (Sherlack)", gen(1 << 20, 3, b"Sherlack")),
        ("tedbench text 64 KiB", tb_text(64 << 10)),
        ("tedbench text 1 MiB", tb_text(1 << 20)),
    ];
    let a = nvol::packed::Searcher::new(&pats).unwrap();
    let b = Config::new().match_kind(MatchKind::LeftmostFirst).builder().extend(&pats).build().unwrap();
    for (name, h) in &cases {
        assert_eq!(a.find_iter(h).count(), b.find_iter(h).count());
        let (mut ta, mut tb) = (f64::MAX, f64::MAX);
        for _ in 0..15 {
            if std::env::args().nth(1).as_deref() == Some("ahofirst") {
                tb = tb.min(t(&mut || b.find_iter(black_box(h)).count()));
                ta = ta.min(t(&mut || a.find_iter(black_box(h)).count()));
            } else {
                ta = ta.min(t(&mut || a.find_iter(black_box(h)).count()));
                tb = tb.min(t(&mut || b.find_iter(black_box(h)).count()));
            }
        }
        println!("{name:36} matches={:7}  nvol {:.3} ms  aho {:.3} ms  delta {:+.1}%",
                 a.find_iter(h).count(), ta * 1e3, tb * 1e3, (tb / ta - 1.0) * 100.0);
    }
}
