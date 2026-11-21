//! Focused, interleaved A/B: iterate every match position (memchr_iter vs nvol).
//! Usage: cargo run --release --bin itbench [fwd|rev]
use nvol_harness::nvol;
use std::hint::black_box;
use std::time::Instant;

fn text(len: usize, line: usize) -> Vec<u8> {
    (0..len).map(|i| if i % line == line - 1 { b'\n' } else { b'a' + (i * 7 % 26) as u8 }).collect()
}

fn t(f: &mut dyn FnMut() -> usize) -> f64 {
    let s = Instant::now();
    for _ in 0..20 { black_box(f()); }
    s.elapsed().as_secs_f64() / 20.0
}

fn main() {
    let rev = std::env::args().nth(1).as_deref() == Some("rev");
    let mut total = 0.0;
    for &line in &[4usize, 8, 20, 40, 64, 100, 200, 1000] {
        let h = text(1 << 20, line);
        let (mut a, mut b) = (f64::MAX, f64::MAX);
        for _ in 0..25 {
            if rev {
                a = a.min(t(&mut || nvol::memrchr_iter(b'\n', black_box(&h)).fold(0, |x, p| x ^ p)));
                b = b.min(t(&mut || memchr::memrchr_iter(b'\n', black_box(&h)).fold(0, |x, p| x ^ p)));
            } else {
                a = a.min(t(&mut || nvol::memchr_iter(b'\n', black_box(&h)).fold(0, |x, p| x ^ p)));
                b = b.min(t(&mut || memchr::memchr_iter(b'\n', black_box(&h)).fold(0, |x, p| x ^ p)));
            }
        }
        let d = (b / a - 1.0) * 100.0;
        total += d;
        println!("line={line:5}  nvol {:.3} ms  memchr {:.3} ms  delta {:+.1}%", a * 1e3, b * 1e3, d);
    }
    println!("mean delta {:+.1}%", total / 8.0);
}
