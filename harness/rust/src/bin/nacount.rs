//! `first_non_ascii` against bstr under hardware counters: one implementation, one size,
//! N calls over an all-ASCII haystack (every call scans to the end).
//! Usage: nacount nvol|bstr SIZE N
//! Build for Linux and run under `perf stat`, subtracting the run with N = 0; see
//! harness/rust/nacounters.sh.
//!
//! bstr's public `find_non_ascii_byte` returns `None` when every byte is ASCII, as NVOL's
//! `first_non_ascii` does. Only its internal `ascii::first_non_ascii_byte` returns `len`.
use bstr::ByteSlice;
use nvol_harness::nvol;
use std::hint::black_box;

fn main() {
    let a: Vec<String> = std::env::args().collect();
    if a.len() != 4 {
        eprintln!("usage: nacount nvol|bstr SIZE N");
        std::process::exit(2);
    }
    let (imp, size, n): (&str, usize, usize) = (&a[1], a[2].parse().unwrap(), a[3].parse().unwrap());
    let h: Vec<u8> = (0..size).map(|i| b'a' + (i * 7 % 26) as u8).collect();
    let mut sink = 0usize;
    match imp {
        "nvol" => for _ in 0..n { sink += nvol::first_non_ascii(black_box(&h)).unwrap_or(0) },
        "bstr" => for _ in 0..n { sink += black_box(&h[..]).find_non_ascii_byte().unwrap_or(0) },
        _ => { eprintln!("impl: nvol or bstr"); std::process::exit(2); }
    }
    black_box(sink);
}
