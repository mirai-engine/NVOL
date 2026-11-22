//! Rust side of the parity benchmark (harness/parity): one operation of the NVOL Rust module,
//! N times, with the same inputs as harness/asm/bench.c, for hardware counters.
//!
//!     parbench OP SIZE N
//!
//! OP: memchr memrchr memchr2 memchr3 count count_parity first_non_ascii memmem memmem2
//!     byteset byteset_not pair teddy
//! The Rust module has no test hook, so this is the CPU's own path (AVX2 on x86_64).
//! Every call scans the whole buffer (nothing matches), as in bench.c.
use nvol_harness::nvol;
use std::hint::black_box;

fn main() {
    let a: Vec<String> = std::env::args().collect();
    if a.len() != 4 {
        eprintln!("usage: parbench OP SIZE N");
        std::process::exit(2);
    }
    let (op, size, n): (&str, usize, usize) = (&a[1], a[2].parse().unwrap(), a[3].parse().unwrap());
    if size > 1 << 20 {
        std::process::exit(2);
    }
    let buf: Vec<u8> = (0..(1usize << 20) + 64).map(|i| b'a' + (i % 8) as u8).collect();   // a..h
    let spaces = vec![b' '; (1 << 20) + 64];
    let h = &buf[..size];
    let mut cls = [0u8; nvol::raw::NVOL_BYTESET_CLASS_BYTES];
    let mut ws = [0u8; nvol::raw::NVOL_BYTESET_CLASS_BYTES];
    let mut pair = [0u8; nvol::raw::NVOL_BYTESET_PAIR_BYTES];
    unsafe {
        nvol::raw::nvol_byteset_init(b"xyz0".as_ptr(), 4, cls.as_mut_ptr());
        nvol::raw::nvol_byteset_init(b" \t\n\r".as_ptr(), 4, ws.as_mut_ptr());
        nvol::raw::nvol_byteset_pair_init(b"xy".as_ptr(), 2, b"q".as_ptr(), 1, 0, 1, pair.as_mut_ptr());
    }
    let fin = nvol::memmem::Finder::new(b"needle12");
    let fin2 = nvol::memmem::Finder::new(b"zq");                // ugrep's -c zq
    let ted = nvol::packed::Searcher::new(["foo", "bar", "quux", "zebra", "xylo", "jump", "wizard", "yoyo"])
        .expect("teddy");
    let mut sink = 0usize;
    let (p, e) = (h.as_ptr(), unsafe { h.as_ptr().add(size) });
    let (sp, se) = (spaces.as_ptr(), unsafe { spaces.as_ptr().add(size) });
    macro_rules! times {
        ($e:expr) => {
            for _ in 0..n {
                sink = sink.wrapping_add(black_box($e));
            }
        };
    }
    match op {
        "memchr" => times!(nvol::memchr(b'z', black_box(h)).unwrap_or(size)),
        "memrchr" => times!(nvol::memrchr(b'z', black_box(h)).unwrap_or(size)),
        "memchr2" => times!(nvol::memchr2(b'y', b'z', black_box(h)).unwrap_or(size)),
        "memchr3" => times!(nvol::memchr3(b'x', b'y', b'z', black_box(h)).unwrap_or(size)),
        "count" => times!(nvol::count(b'a', black_box(h))),
        "count_parity" => times!(nvol::count_parity(b'a', black_box(h))),
        "first_non_ascii" => times!(nvol::first_non_ascii(black_box(h)).unwrap_or(size)),
        "memmem" => times!(fin.find(black_box(h)).unwrap_or(size)),
        "memmem2" => times!(fin2.find(black_box(h)).unwrap_or(size)),
        "byteset" => times!(unsafe { nvol::raw::nvol_find_byteset_raw(cls.as_ptr(), black_box(p), e) } as usize),
        "byteset_not" => times!(unsafe { nvol::raw::nvol_find_not_byteset_raw(ws.as_ptr(), black_box(sp), se) } as usize),
        "pair" => times!(unsafe { nvol::raw::nvol_find_byteset_pair_raw(pair.as_ptr(), black_box(p), e) } as usize),
        "teddy" => times!(ted.find(black_box(h)).map_or(size, |m| m.start)),
        _ => {
            eprintln!("unknown op {op}");
            std::process::exit(2);
        }
    }
    std::process::exit(if sink == 12345 { 1 } else { 0 });
}
