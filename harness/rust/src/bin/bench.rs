//! NVOL-generated Rust vs memchr 2.8.3 (the crate ripgrep uses).
//! Reports throughput in GB/s, best of 21 interleaved A/B rounds. Usage: cargo run --release --bin bench
use nvol_harness::nvol;
use std::hint::black_box;

fn text(len: usize) -> Vec<u8> {
    // English-ish text, ~1 newline / 64 bytes, no 'Z', '{', '~' or bytes >= 0x80
    let words = b"the quick brown fox jumps over lazy dog search line match grep ripgrep ";
    let mut v = Vec::with_capacity(len);
    let mut i = 0usize;
    while v.len() < len {
        v.push(if v.len() % 64 == 63 { b'\n' } else { words[i % words.len()] });
        i = i.wrapping_mul(1103515245).wrapping_add(12345) >> 3;
    }
    v
}

/// (nvol GB/s, memchr GB/s), interleaved.
fn pair(bytes: usize, mut fa: impl FnMut() -> usize, mut fb: impl FnMut() -> usize) -> (f64, f64) {
    let (a, b) = nvol_harness::ab(&mut fa, &mut fb, bytes, 21);
    (bytes as f64 / a / 1e9, bytes as f64 / b / 1e9)
}

fn row(name: &str, size: usize, (ours, theirs): (f64, f64)) {
    println!("| {:<28} | {:>8} | {:>8.2} | {:>8.2} | {:>+6.1}% |", name, human(size), ours, theirs, (ours / theirs - 1.0) * 100.0);
}

fn human(n: usize) -> String {
    if n >= 1 << 20 { format!("{}MiB", n >> 20) } else if n >= 1 << 10 { format!("{}KiB", n >> 10) } else { format!("{n}B") }
}

fn main() {
    #[cfg(target_arch = "x86_64")]
    println!("avx2 = {}", std::is_x86_feature_detected!("avx2"));
    println!("| {:<28} | {:>8} | {:>8} | {:>8} | {:>7} |", "op", "size", "nvol GB/s", "memchr", "delta");
    println!("|{:-<30}|{:-<10}|{:-<10}|{:-<10}|{:-<9}|", "", "", "", "", "");
    for &size in &[16usize, 32, 64, 256, 4 << 10, 64 << 10, 1 << 20, 16 << 20] {
        let h = text(size);
        let h = &h[..];
        row("memchr (absent)", size, pair(size, || nvol::memchr(b'Z', black_box(h)).unwrap_or(0),
            || memchr::memchr(b'Z', black_box(h)).unwrap_or(0)));
        row("memrchr (absent)", size, pair(size, || nvol::memrchr(b'Z', black_box(h)).unwrap_or(0),
            || memchr::memrchr(b'Z', black_box(h)).unwrap_or(0)));
        row("memchr2 (absent)", size, pair(size, || nvol::memchr2(b'Z', b'{', black_box(h)).unwrap_or(0),
            || memchr::memchr2(b'Z', b'{', black_box(h)).unwrap_or(0)));
        row("memchr3 (absent)", size, pair(size, || nvol::memchr3(b'Z', b'{', b'~', black_box(h)).unwrap_or(0),
            || memchr::memchr3(b'Z', b'{', b'~', black_box(h)).unwrap_or(0)));
        row("count NL (sad)", size, pair(size, || nvol::count(b'\n', black_box(h)),
            || memchr::memchr_iter(b'\n', black_box(h)).count()));
        row("count NL (parity)", size, pair(size, || nvol::count_parity(b'\n', black_box(h)),
            || memchr::memchr_iter(b'\n', black_box(h)).count()));
        if size >= 256 {
            // grep-searcher LineStep pattern: one memchr call per line
            row("line step (memchr per line)", size,
                pair(size, || line_step(black_box(h), |n, s| nvol::memchr(n, s)),
                || line_step(black_box(h), |n, s| memchr::memchr(n, s))));
            row("memchr_iter (sum positions)", size,
                pair(size, || nvol::memchr_iter(b'\n', black_box(h)).fold(0, |a, p| a ^ p),
                || memchr::memchr_iter(b'\n', black_box(h)).fold(0, |a, p| a ^ p)));
            row("memrchr_iter (sum positions)", size,
                pair(size, || nvol::memrchr_iter(b'\n', black_box(h)).fold(0, |a, p| a ^ p),
                || memchr::memrchr_iter(b'\n', black_box(h)).fold(0, |a, p| a ^ p)));
            row("memchr_iter().count()", size,
                pair(size, || nvol::memchr_iter(b'\n', black_box(h)).count(),
                || memchr::memchr_iter(b'\n', black_box(h)).count()));
        }
        println!("|{:-<30}|{:-<10}|{:-<10}|{:-<10}|{:-<9}|", "", "", "", "", "");
    }
}

#[inline(always)]
fn line_step(h: &[u8], f: impl Fn(u8, &[u8]) -> Option<usize>) -> usize {
    let (mut n, mut at) = (0usize, 0usize);
    while let Some(i) = f(b'\n', &h[at..]) { at += i + 1; n += 1; }
    n
}
