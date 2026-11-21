//! memchr on short haystacks (1-15 bytes, item 28) under hardware counters: one
//! implementation, one size, N calls with no match (every call scans to the end).
//! Usage: smallcount nvol|memchr SIZE N
//! Build for Linux and run under `perf stat`, subtracting the run with N = 0; see
//! harness/rust/pcounters.sh.
use nvol_harness::nvol;
use std::hint::black_box;

fn main() {
    let a: Vec<String> = std::env::args().collect();
    if a.len() != 4 {
        eprintln!("usage: smallcount nvol|memchr SIZE N");
        std::process::exit(2);
    }
    let (imp, size, n): (&str, usize, usize) = (&a[1], a[2].parse().unwrap(), a[3].parse().unwrap());
    let h: Vec<u8> = (0..size).map(|i| b'a' + (i * 7 % 26) as u8).collect();
    let mut sink = 0usize;
    match imp {
        "nvol" => for _ in 0..n { sink += nvol::memchr(black_box(b'Z'), black_box(&h)).unwrap_or(0) },
        "memchr" => for _ in 0..n { sink += memchr::memchr(black_box(b'Z'), black_box(&h)).unwrap_or(0) },
        _ => { eprintln!("impl: nvol or memchr"); std::process::exit(2); }
    }
    black_box(sink);
}
