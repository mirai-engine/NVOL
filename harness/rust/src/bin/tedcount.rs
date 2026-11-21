//! Teddy under hardware counters: one implementation, one tedbench case, one size, N calls.
//! Usage: tedcount nvol|aho CASE SIZE N   (CASE = row index in tedbench, 0..7)
//! Build for Linux and run under `perf stat` (subtract the run with N = 0); see
//! harness/rust/tedcounters.sh. The haystack and patterns are exactly tedbench's.
use aho_corasick::packed::{Config, MatchKind};
use nvol_harness::nvol;
use std::hint::black_box;

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

fn absent(n: usize, len: usize) -> Vec<Vec<u8>> {
    let mut x = 0x2545F4914F6CDD1Du64 ^ (n as u64 * 31 + len as u64);
    (0..n).map(|_| (0..len).map(|i| { x ^= x << 13; x ^= x >> 7; x ^= x << 17;
        if i == 0 { b'A' + (x % 26) as u8 } else { b'a' + (x % 26) as u8 } }).chain(std::iter::once(b'Q')).take(len).collect()).collect()
}

fn main() {
    let a: Vec<String> = std::env::args().collect();
    if a.len() != 5 {
        eprintln!("usage: tedcount nvol|aho CASE SIZE N");
        std::process::exit(2);
    }
    let (imp, case, size, n): (&str, usize, usize, usize) = (&a[1], a[2].parse().unwrap(), a[3].parse().unwrap(), a[4].parse().unwrap());
    let sets: Vec<Vec<Vec<u8>>> = vec![
        absent(2, 6), absent(8, 3), (0..16).map(|i| vec![b'0' + i as u8]).collect(), absent(16, 2),
        absent(32, 5), absent(40, 3), absent(64, 6),
        vec![b"Sherlock".to_vec(), b"Holmes".to_vec(), b"Watson".to_vec()],
    ];
    let pats = &sets[case];
    let hay = text(size);
    let iter = case == 7;
    let mut sink = 0usize;
    match imp {
        "nvol" => {
            let s = nvol::packed::Searcher::new(pats).expect("nvol teddy");
            for _ in 0..n {
                sink += if iter { s.find_iter(black_box(&hay)).count() } else { s.find(black_box(&hay)).map_or(0, |m| m.start) };
            }
        }
        "aho" => {
            let s = Config::new().match_kind(MatchKind::LeftmostFirst).builder().extend(pats).build().expect("aho teddy");
            for _ in 0..n {
                sink += if iter { s.find_iter(black_box(&hay)).count() } else { s.find(black_box(&hay)).map_or(0, |m| m.start()) };
            }
        }
        _ => { eprintln!("impl: nvol or aho"); std::process::exit(2); }
    }
    black_box(sink);
}
