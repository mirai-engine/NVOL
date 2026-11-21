//! Focused, interleaved A/B for the grep-searcher LineStep pattern.
use nvol_harness::nvol;
use std::hint::black_box;
use std::time::Instant;

fn text(len: usize, line: usize) -> Vec<u8> {
    (0..len).map(|i| if i % line == line - 1 { b'\n' } else { b'a' + (i * 7 % 26) as u8 }).collect()
}
#[inline(always)]
fn step(h: &[u8], f: impl Fn(u8, &[u8]) -> Option<usize>) -> usize {
    let (mut n, mut at) = (0, 0);
    while let Some(i) = f(b'\n', &h[at..]) { at += i + 1; n += 1; }
    n
}
fn t(f: &mut dyn FnMut() -> usize) -> f64 {
    let s = Instant::now(); for _ in 0..20 { black_box(f()); } s.elapsed().as_secs_f64()
}
fn main() {
    for &line in &[8usize, 20, 40, 64, 100, 200, 1000] {
        let h = text(1 << 20, line);
        let (mut a, mut b) = (f64::MAX, f64::MAX);
        for _ in 0..15 {
            a = a.min(t(&mut || step(black_box(&h), |n, s| nvol::memchr(n, s))));
            b = b.min(t(&mut || step(black_box(&h), |n, s| memchr::memchr(n, s))));
        }
        println!("line={line:5}  nvol {:.3} ms  memchr {:.3} ms  delta {:+.1}%", a * 50.0, b * 50.0, (b / a - 1.0) * 100.0);
    }
}
