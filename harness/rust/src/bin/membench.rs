//! memmem: NVOL-generated Rust vs memchr 2.8.3 (memchr::memmem), interleaved A/B.
//! Usage: cargo run --release --bin membench
use nvol_harness::nvol;
use std::hint::black_box;

fn text(len: usize) -> Vec<u8> {
    let words: [&[u8]; 16] = [b"the ", b"quick ", b"brown ", b"fox ", b"jumps ", b"over ", b"lazy ", b"dog ",
        b"Sherlock ", b"Holmes ", b"said ", b"to ", b"Watson ", b"and ", b"then\n", b"of "];
    let (mut v, mut x) = (Vec::with_capacity(len + 16), 0x2545F4914F6CDD1Du64);
    while v.len() < len {
        x ^= x << 13; x ^= x >> 7; x ^= x << 17;
        v.extend_from_slice(words[(x % 16) as usize]);
    }
    v.truncate(len);
    v
}

fn row(name: &str, size: usize, a: f64, b: f64) {
    let unit = |s: f64| if size >= 4096 { format!("{:8.2} GB/s", size as f64 / s / 1e9) } else { format!("{:8.1} ns  ", s * 1e9) };
    println!("| {:<36} | {:>7} | {} | {} | {:>+6.1}% |", name, size, unit(a), unit(b), (b / a - 1.0) * 100.0);
}

fn main() {
    println!("| {:<36} | {:>7} | {:>13} | {:>13} | {:>7} |", "case", "hay B", "nvol", "memchr", "delta");
    println!("|{:-<38}|{:-<9}|{:-<15}|{:-<15}|{:-<9}|", "", "", "", "", "");
    let needles: [(&str, &[u8]); 8] = [
        ("2B absent \"zq\"", b"zq"),
        ("8B present \"Sherlock\"", b"Sherlock"),
        ("8B absent \"Moriarty\"", b"Moriarty"),
        ("16B absent", b"Professor Moriar"),
        ("32B absent", b"Professor Moriarty of the gang!!"),
        ("40B absent (two-way)", b"Professor James Moriarty, the Napoleon!!"),
        ("64B present (two-way)", b"the quick brown fox jumps over lazy dog Sherlock Holmes said to "),
        ("3B frequent \"the\"", b"the"),
    ];
    for &size in &[64usize, 4 << 10, 64 << 10, 1 << 20] {
        let hay = text(size);
        // make the "present" needles appear only near the end so the scan is long
        let mut hay2 = hay.clone();
        for (_, n) in &needles {
            let ns = std::str::from_utf8(n).unwrap();
            if ns.contains("Sherlock") && !ns.contains("quick") {
                hay2 = hay2.split(|_| false).next().unwrap().to_vec();
                let s = String::from_utf8_lossy(&hay2).replace("Sherlock", "Sherlack");
                hay2 = s.into_bytes();
                let at = hay2.len().saturating_sub(n.len() + 3);
                if hay2.len() >= n.len() + 3 { hay2[at..at + n.len()].copy_from_slice(n); }
            }
        }
        for (name, n) in &needles {
            let h: &[u8] = if name.contains("Sherlock") { &hay2 } else { &hay };
            let (fa, fb) = (nvol::memmem::Finder::new(n), memchr::memmem::Finder::new(n));
            assert_eq!(fa.find(h), fb.find(h), "{name} {size}");
            let (a, b) = nvol_harness::ab(&mut || fa.find(black_box(h)).unwrap_or(0), &mut || fb.find(black_box(h)).unwrap_or(0), size, 15);
            row(&format!("Finder::find {name}"), size, a, b);
        }
        let (fa, fb) = (nvol::memmem::Finder::new(b"the "), memchr::memmem::Finder::new(b"the "));
        let (a, b) = nvol_harness::ab(&mut || fa.find_iter(black_box(&hay)).count(), &mut || fb.find_iter(black_box(&hay)).count(), size, 15);
        row("find_iter count \"the \"", size, a, b);
        let (a, b) = nvol_harness::ab(&mut || nvol::memmem::find(black_box(&hay), black_box(b"Moriarty".as_slice())).unwrap_or(0), &mut || memchr::memmem::find(black_box(&hay), black_box(b"Moriarty".as_slice())).unwrap_or(0), size, 15);
        row("one-shot find (build+search) 8B", size, a, b);
        println!("|{:-<38}|{:-<9}|{:-<15}|{:-<15}|{:-<9}|", "", "", "", "", "");
    }
    // construction cost alone (Finder::new), short (packed pair) and long (Two-Way) needles
    for (name, n) in [("8B", b"Moriarty".as_slice()), ("40B", b"Professor James Moriarty, the Napoleon!!".as_slice())] {
        let (a, b) = nvol_harness::ab(&mut || nvol::memmem::Finder::new(black_box(n)).needle().len(), &mut || memchr::memmem::Finder::new(black_box(n)).needle().len(), 64, 15);
        row(&format!("Finder::new {name} (construction only)"), 0, a, b);
    }
    // pathological: Two-Way must stay linear
    let hay = [vec![b'a'; 1 << 20], b"b".to_vec()].concat();
    for n in [[b"a".repeat(40), b"b".to_vec()].concat(), b"ab".repeat(30)] {
        let (fa, fb) = (nvol::memmem::Finder::new(&n), memchr::memmem::Finder::new(&n));
        let (a, b) = nvol_harness::ab(&mut || fa.find(black_box(&hay)).unwrap_or(0), &mut || fb.find(black_box(&hay)).unwrap_or(0), hay.len(), 15);
        row(&format!("pathological a^1M b, needle {}B", n.len()), hay.len(), a, b);
    }
}
