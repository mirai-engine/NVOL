//! "One memchr per line" (grep-searcher's LineStep) under hardware counters: one
//! implementation, one line length, N passes over 1 MiB.
//! Usage: linecount nvol|memchr LINE N
//! Build for Linux and run under `perf stat`, subtracting the run with N = 0; see
//! harness/rust/linecounters.sh. The haystack is linestep's: a '\n' every LINE bytes.
use nvol_harness::nvol;
use std::hint::black_box;

fn text(len: usize, line: usize) -> Vec<u8> {
    (0..len).map(|i| if i % line == line - 1 { b'\n' } else { b'a' + (i * 7 % 26) as u8 }).collect()
}

#[inline(always)]
fn step(h: &[u8], f: impl Fn(u8, &[u8]) -> Option<usize>) -> usize {
    let (mut n, mut at) = (0, 0);
    while let Some(i) = f(b'\n', &h[at..]) {
        at += i + 1;
        n += 1;
    }
    n
}

fn main() {
    let a: Vec<String> = std::env::args().collect();
    if a.len() != 4 {
        eprintln!("usage: linecount nvol|memchr LINE N");
        std::process::exit(2);
    }
    let (imp, line, n): (&str, usize, usize) = (&a[1], a[2].parse().unwrap(), a[3].parse().unwrap());
    let h = text(1 << 20, line);
    let mut sink = 0usize;
    match imp {
        "nvol" => for _ in 0..n { sink += step(black_box(&h), |c, s| nvol::memchr(c, s)) },
        "memchr" => for _ in 0..n { sink += step(black_box(&h), |c, s| memchr::memchr(c, s)) },
        _ => { eprintln!("impl: nvol or memchr"); std::process::exit(2); }
    }
    black_box(sink);
}
