//! Rust side of the parity benchmark (harness/parity): one operation of the NVOL Rust module,
//! N times, with the same inputs as harness/asm/bench.c, for hardware counters.
//!
//!     parbench OP SIZE N
//!
//! OP: memchr memrchr memchr2 memchr3 count count_parity first_non_ascii memmem memmem2
//!     byteset byteset_not pair teddy, json utf8 digits copy_and_find (as in bench.c)
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
    // simdjson's operations (bench.c's REC, the same digits)
    let rec: &[u8] = b"{\"id\":12345678,\"name\":\"caf\xc3\xa9 \\\"q\\\"\",\"tags\":[\"a\",\"b\"],\"v\":[1.5,2.25,-3e8]}\n";
    let jbuf: Vec<u8> = (0..(1usize << 20) + 64).map(|i| rec[i % rec.len()]).collect();
    let dbuf: Vec<u8> = (0..(1usize << 20) + 64).map(|i| b'0' + ((i * 7 + i / 8) % 10) as u8).collect();
    let mut jout = vec![0u8; 4 * ((1 << 20) + 64)];
    let mut jstate = [0u8; nvol::raw::NVOL_JSON_STATE_BYTES];
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
        "json" => times!({
            jstate.fill(0);
            unsafe { nvol::raw::nvol_json_index_raw(jstate.as_mut_ptr(), black_box(jbuf.as_ptr()), size / 64, 0, jout.as_mut_ptr()) }
        }),
        "utf8" => times!(unsafe { nvol::raw::nvol_utf8_valid(black_box(jbuf.as_ptr()), size) }),
        "digits" => times!((0..size / 8).fold(0usize, |s, j| s.wrapping_add(
            unsafe { nvol::raw::nvol_parse_eight_digits(black_box(dbuf.as_ptr()).add(8 * j)) } as usize))),
        "copy_and_find" => times!((0..size / 32).fold(0usize, |s, j| s.wrapping_add(unsafe {
            nvol::raw::nvol_json_copy_and_find(black_box(jbuf.as_ptr()).add(32 * j), jout.as_mut_ptr().add(32 * j))
        } as usize))),
        _ => {
            eprintln!("unknown op {op}");
            std::process::exit(2);
        }
    }
    std::process::exit(if sink == 12345 { 1 } else { 0 });
}
