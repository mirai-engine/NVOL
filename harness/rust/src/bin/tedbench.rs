//! Teddy: NVOL-generated Rust vs aho-corasick 1.1.5 packed::Searcher (LeftmostFirst).
//! Usage: cargo run --release --bin tedbench
use aho_corasick::packed::{Config, MatchKind};
use nvol_harness::nvol;
use std::hint::black_box;
use std::time::Instant;

fn text(len: usize) -> Vec<u8> {
    let words: [&[u8]; 16] = [b"the ", b"quick ", b"brown ", b"fox ", b"jumps ", b"over ", b"lazy ", b"dog ",
        b"Sherlock ", b"Holmes ", b"said ", b"to ", b"Watson ", b"and ", b"then\n", b"of "];
    let (mut v, mut x) = (Vec::with_capacity(len + 16), 0x9E3779B97F4A7C15u64);
    while v.len() < len {
        x ^= x << 13; x ^= x >> 7; x ^= x << 17;
        v.extend_from_slice(words[(x % 16) as usize]);
    }
    v.truncate(len);
    v
}

/// Interleaved A/B: alternate the two closures every round, keep each one's best.
fn ab(fa: &mut dyn FnMut() -> usize, fb: &mut dyn FnMut() -> usize, bytes: usize) -> (f64, f64) {
    let iters = ((16 << 20) / bytes.max(1)).clamp(1, 1 << 19);
    let (mut a, mut b) = (f64::MAX, f64::MAX);
    for _ in 0..15 {
        let t = Instant::now();
        for _ in 0..iters { black_box(fa()); }
        a = a.min(t.elapsed().as_secs_f64() / iters as f64);
        let t = Instant::now();
        for _ in 0..iters { black_box(fb()); }
        b = b.min(t.elapsed().as_secs_f64() / iters as f64);
    }
    (a, b)
}

fn row(name: &str, size: usize, a: f64, b: f64) {
    let unit = |s: f64| if size >= 4096 { format!("{:8.2} GB/s", size as f64 / s / 1e9) } else { format!("{:8.1} ns  ", s * 1e9) };
    println!("| {:<40} | {:>7} | {} | {} | {:>+6.1}% |", name, size, unit(a), unit(b), (b / a - 1.0) * 100.0);
}

fn absent(n: usize, len: usize) -> Vec<Vec<u8>> {
    // pseudo-random lowercase/uppercase strings that do not occur in the text
    let mut x = 0x2545F4914F6CDD1Du64 ^ (n as u64 * 31 + len as u64);
    (0..n).map(|_| (0..len).map(|i| { x ^= x << 13; x ^= x >> 7; x ^= x << 17;
        if i == 0 { b'A' + (x % 26) as u8 } else { b'a' + (x % 26) as u8 } }).chain(std::iter::once(b'Q')).take(len).collect()).collect()
}

fn main() {
    println!("| {:<40} | {:>7} | {:>13} | {:>13} | {:>7} |", "case", "hay B", "nvol", "aho-corasick", "delta");
    println!("|{:-<42}|{:-<9}|{:-<15}|{:-<15}|{:-<9}|", "", "", "", "", "");
    let sets: Vec<(String, Vec<Vec<u8>>)> = vec![
        ("2 pats, m=4 (Slim256) absent".into(), absent(2, 6)),
        ("8 pats, m=3 (Slim256) absent".into(), absent(8, 3)),
        ("16 pats, m=1 (Slim256) absent".into(), (0..16).map(|i| vec![b'0' + i as u8]).collect()),
        ("16 pats, m=2 (Slim256) absent".into(), absent(16, 2)),
        ("32 pats, m=4 (Slim256) absent".into(), absent(32, 5)),
        ("40 pats, m=3 (Fat256) absent".into(), absent(40, 3)),
        ("64 pats, m=4 (Fat256) absent".into(), absent(64, 6)),
        ("3 words present (Sherlock,Holmes,Watson)".into(), vec![b"Sherlock".to_vec(), b"Holmes".to_vec(), b"Watson".to_vec()]),
    ];
    for &size in &[64usize, 4 << 10, 64 << 10, 1 << 20] {
        let hay = text(size);
        for (name, pats) in &sets {
            let a = nvol::packed::Searcher::new(pats).expect("nvol teddy");
            let b = Config::new().match_kind(MatchKind::LeftmostFirst).builder().extend(pats).build().expect("aho teddy");
            assert_eq!(a.find(&hay).map(|m| (m.pattern, m.start)), b.find(&hay).map(|m| (m.pattern().as_usize(), m.start())), "{name}");
            if name.contains("present") {
                let (x, y) = ab(&mut || a.find_iter(black_box(&hay)).count(), &mut || b.find_iter(black_box(&hay)).count(), size);
                row(&format!("find_iter count {name}"), size, x, y);
            } else {
                let (x, y) = ab(&mut || a.find(black_box(&hay)).map_or(0, |m| m.start), &mut || b.find(black_box(&hay)).map_or(0, |m| m.start()), size);
                row(&format!("find {name}"), size, x, y);
            }
        }
        println!("|{:-<42}|{:-<9}|{:-<15}|{:-<15}|{:-<9}|", "", "", "", "", "");
    }
    for (name, pats) in [("build 8 pats", absent(8, 4)), ("build 64 pats", absent(64, 6))] {
        let (x, y) = ab(&mut || nvol::packed::Searcher::new(black_box(&pats)).map_or(0, |_| 1), &mut || Config::new().builder().extend(black_box(&pats)).build().map_or(0, |_| 1), 64);
        row(&format!("{name} (construction only)"), 0, x, y);
    }
}
