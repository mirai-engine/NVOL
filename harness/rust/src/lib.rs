//! Test + benchmark harness for the NVOL-generated Rust module.
#[path = "../../../generated/rust/nvol.rs"]
pub mod nvol;

/// Interleaved A/B timing shared by the benchmark binaries: every round times
/// both closures back to back, alternating which goes first, and keeps each
/// one's best seconds per call. Timing one side fully before the other biased
/// rows by up to ±10 % (PENDING 1.2d).
pub fn ab(fa: &mut dyn FnMut() -> usize, fb: &mut dyn FnMut() -> usize, bytes: usize, rounds: usize) -> (f64, f64) {
    use std::{hint::black_box, time::Instant};
    let iters = ((16 << 20) / bytes.max(1)).clamp(1, 1 << 19);
    let (mut a, mut b) = (f64::MAX, f64::MAX);
    let time = |f: &mut dyn FnMut() -> usize| {
        let t = Instant::now();
        for _ in 0..iters { black_box(f()); }
        t.elapsed().as_secs_f64() / iters as f64
    };
    for r in 0..rounds {
        if r % 2 == 0 {
            a = a.min(time(fa)); b = b.min(time(fb));
        } else {
            b = b.min(time(fb)); a = a.min(time(fa));
        }
    }
    (a, b)
}

#[cfg(test)]
mod tests {
    use super::nvol::{self, raw};

    struct Rng(u64);
    impl Rng {
        fn next(&mut self) -> u64 {
            self.0 ^= self.0 << 13;
            self.0 ^= self.0 >> 7;
            self.0 ^= self.0 << 17;
            self.0
        }
        fn below(&mut self, n: usize) -> usize { (self.next() % n as u64) as usize }
    }

    fn ix(p: *const u8, base: *const u8) -> usize { if p.is_null() { usize::MAX } else { p as usize - base as usize } }
    fn naive_find(ns: &[u8], h: &[u8]) -> usize { h.iter().position(|b| ns.contains(b)).unwrap_or(usize::MAX) }
    fn naive_rfind(ns: &[u8], h: &[u8]) -> usize { h.iter().rposition(|b| ns.contains(b)).unwrap_or(usize::MAX) }
    fn naive_count(n: u8, h: &[u8]) -> usize { h.iter().filter(|&&b| b == n).count() }
    fn naive_ascii(h: &[u8]) -> usize { h.iter().position(|&b| b >= 0x80).unwrap_or(usize::MAX) }

    /// Every (ISA-specific) raw function and every dispatcher, on every
    /// length 0..300 at every alignment 0..63, with the match at the edges,
    /// middle, nowhere, plus poison just outside the window.
    #[test]
    fn positional_exhaustive() {
        #[cfg(target_arch = "x86_64")]
        let avx2 = std::is_x86_feature_detected!("avx2");
        let mut rng = Rng(0x9E3779B97F4A7C15);
        let mut buf = vec![b'a'; 300 + 128];
        for len in 0..300usize {
            for off in 0..64usize {
                let mut pos = vec![None, Some(0), Some(len / 2), Some(len.saturating_sub(1))];
                if len > 0 { pos.push(Some(rng.below(len))); }
                for &p in &pos {
                    if len == 0 && p.is_some() { continue; }
                    for hit in [b'z', 0x80u8] {
                        buf.iter_mut().for_each(|b| *b = b'a');
                        if off > 0 { buf[off - 1] = hit; }
                        buf[off + len] = hit;
                        if let Some(p) = p { buf[off + p] = hit; }
                        let h = &buf[off..off + len];
                        let ptr = h.as_ptr();
                        unsafe {
                            let n = [b'x', b'y', hit];
                            // dispatchers
                            assert_eq!(raw::nvol_memchr(hit, ptr, len), naive_find(&[hit], h));
                            assert_eq!(raw::nvol_memrchr(hit, ptr, len), naive_rfind(&[hit], h));
                            assert_eq!(raw::nvol_memchr2(n[1], hit, ptr, len), naive_find(&n[1..], h));
                            assert_eq!(raw::nvol_memrchr2(n[1], hit, ptr, len), naive_rfind(&n[1..], h));
                            assert_eq!(raw::nvol_memchr3(n[0], n[1], hit, ptr, len), naive_find(&n, h));
                            assert_eq!(raw::nvol_memrchr3(n[0], n[1], hit, ptr, len), naive_rfind(&n, h));
                            assert_eq!(raw::nvol_count(hit, ptr, len), naive_count(hit, h));
                            assert_eq!(raw::nvol_count_parity(hit, ptr, len), naive_count(hit, h));
                            assert_eq!(raw::nvol_first_non_ascii(ptr, len), naive_ascii(h));
                            // ISA-specific paths, forced
                            #[cfg(target_arch = "aarch64")]
                            if len >= 16 {
                                assert_eq!(ix(raw::nvol_memchr_neon(hit, ptr, ptr.add(len)), ptr), naive_find(&[hit], h));
                                assert_eq!(ix(raw::nvol_memrchr_neon(hit, ptr, ptr.add(len)), ptr), naive_rfind(&[hit], h));
                                assert_eq!(ix(raw::nvol_memchr2_neon(n[1], hit, ptr, ptr.add(len)), ptr), naive_find(&n[1..], h));
                                assert_eq!(ix(raw::nvol_memrchr2_neon(n[1], hit, ptr, ptr.add(len)), ptr), naive_rfind(&n[1..], h));
                                assert_eq!(ix(raw::nvol_memchr3_neon(n[0], n[1], hit, ptr, ptr.add(len)), ptr), naive_find(&n, h));
                                assert_eq!(ix(raw::nvol_memrchr3_neon(n[0], n[1], hit, ptr, ptr.add(len)), ptr), naive_rfind(&n, h));
                                assert_eq!(raw::nvol_count_sad_neon(hit, ptr, ptr.add(len)), naive_count(hit, h));
                                assert_eq!(raw::nvol_count_parity_neon(hit, ptr, ptr.add(len)), naive_count(hit, h));
                                assert_eq!(ix(raw::nvol_first_non_ascii_neon(ptr, ptr.add(len)), ptr), naive_ascii(h));
                            }
                            #[cfg(target_arch = "x86_64")]
                            if len >= 16 {
                                assert_eq!(ix(raw::nvol_memchr_sse2(hit, ptr, ptr.add(len)), ptr), naive_find(&[hit], h));
                                assert_eq!(ix(raw::nvol_memrchr_sse2(hit, ptr, ptr.add(len)), ptr), naive_rfind(&[hit], h));
                                assert_eq!(ix(raw::nvol_memchr3_sse2(n[0], n[1], hit, ptr, ptr.add(len)), ptr), naive_find(&n, h));
                                assert_eq!(ix(raw::nvol_memrchr2_sse2(n[1], hit, ptr, ptr.add(len)), ptr), naive_rfind(&n[1..], h));
                                assert_eq!(raw::nvol_count_sad_sse2(hit, ptr, ptr.add(len)), naive_count(hit, h));
                                assert_eq!(raw::nvol_count_parity_sse2(hit, ptr, ptr.add(len)), naive_count(hit, h));
                                assert_eq!(ix(raw::nvol_first_non_ascii_sse2(ptr, ptr.add(len)), ptr), naive_ascii(h));
                            }
                            #[cfg(target_arch = "x86_64")]
                            if len >= 32 && avx2 {
                                assert_eq!(ix(raw::nvol_memchr_avx2(hit, ptr, ptr.add(len)), ptr), naive_find(&[hit], h));
                                assert_eq!(ix(raw::nvol_memrchr3_avx2(n[0], n[1], hit, ptr, ptr.add(len)), ptr), naive_rfind(&n, h));
                                assert_eq!(ix(raw::nvol_memchr2_avx2(n[1], hit, ptr, ptr.add(len)), ptr), naive_find(&n[1..], h));
                                assert_eq!(raw::nvol_count_sad_avx2(hit, ptr, ptr.add(len)), naive_count(hit, h));
                                assert_eq!(ix(raw::nvol_first_non_ascii_avx2(ptr, ptr.add(len)), ptr), naive_ascii(h));
                            }
                        }
                    }
                }
            }
        }
    }

    #[test]
    fn count_saturation_boundaries() {
        // every byte matches: stresses the u8 lane accumulators (63-iteration fold)
        for len in [0usize, 1, 127, 128, 129, 8063, 8064, 8065, 16128, 16129, 100_000, 1 << 20] {
            let h = vec![b'\n'; len];
            assert_eq!(nvol::count(b'\n', &h), len, "len={len}");
            assert_eq!(nvol::count_parity(b'\n', &h), len);
            #[cfg(target_arch = "x86_64")]
            if len >= 16 { unsafe { assert_eq!(raw::nvol_count_sad_sse2(b'\n', h.as_ptr(), h.as_ptr().add(len)), len); } }
            #[cfg(target_arch = "aarch64")]
            if len >= 16 { unsafe { assert_eq!(raw::nvol_count_sad_neon(b'\n', h.as_ptr(), h.as_ptr().add(len)), len); } }
        }
    }

    #[test]
    fn random_vs_memchr_crate() {
        let mut rng = Rng(42);
        for _ in 0..3000 {
            let len = rng.below(5000);
            let alpha: &[u8] = [&b"ab\n"[..], &b"abcdefgh"[..], &[0u8, 0xff][..]][rng.below(3)];
            let h: Vec<u8> = (0..len).map(|_| alpha[rng.below(alpha.len())]).collect();
            let (a, b, c) = (alpha[rng.below(alpha.len())], b'q', alpha[rng.below(alpha.len())]);
            assert_eq!(nvol::memchr(a, &h), memchr::memchr(a, &h));
            assert_eq!(nvol::memrchr(a, &h), memchr::memrchr(a, &h));
            assert_eq!(nvol::memchr2(a, b, &h), memchr::memchr2(a, b, &h));
            assert_eq!(nvol::memrchr3(a, b, c, &h), memchr::memrchr3(a, b, c, &h));
            assert_eq!(nvol::count(a, &h), memchr::memchr_iter(a, &h).count());
        }
    }

    #[test]
    fn iterators_vs_memchr_crate() {
        let mut rng = Rng(77);
        for _ in 0..2000 {
            let len = rng.below(3000);
            let h: Vec<u8> = (0..len).map(|_| b"ab\nc"[rng.below(4)]).collect();
            assert!(nvol::memchr_iter(b'\n', &h).eq(memchr::memchr_iter(b'\n', &h)));
            assert!(nvol::memrchr_iter(b'\n', &h).eq(memchr::memrchr_iter(b'\n', &h)));
            assert!(nvol::memchr2_iter(b'\n', b'c', &h).eq(memchr::memchr2_iter(b'\n', b'c', &h)));
            assert!(nvol::memchr3_iter(b'\n', b'c', b'b', &h).rev().eq(memchr::memchr3_iter(b'\n', b'c', b'b', &h).rev()));
            assert_eq!(nvol::memchr_iter(b'\n', &h).count(), memchr::memchr_iter(b'\n', &h).count());
            // interleaved front/back
            let (mut a, mut b) = (nvol::memchr_iter(b'\n', &h), memchr::memchr_iter(b'\n', &h));
            for i in 0.. { let (x, y) = if i % 3 == 0 { (a.next_back(), b.next_back()) } else { (a.next(), b.next()) }; assert_eq!(x, y); if x.is_none() { break; } }
            // count after partial consumption
            let mut it = nvol::memchr_iter(b'\n', &h); it.next();
            let mut it2 = memchr::memchr_iter(b'\n', &h); it2.next();
            assert_eq!(it.count(), it2.count());
        }
    }

    #[test]
    fn memmem_vs_memchr_crate() {
        let mut rng = Rng(4242);
        let alphas: [&[u8]; 4] = [b"ab", b"abc", b"a", b"the quick brown fox"];
        for _ in 0..40000 {
            let alpha = alphas[rng.below(alphas.len())];
            let hl = [0usize, 1, 5, 15, 16, 17, 31, 33, 48, 64, 100, 200, 400, 3000][rng.below(14)];
            let h: Vec<u8> = (0..hl).map(|_| alpha[rng.below(alpha.len())]).collect();
            let nl = [0usize, 1, 2, 3, 4, 7, 8, 16, 31, 32, 33, 40, 64, 80][rng.below(14)];
            let n: Vec<u8> = match rng.below(3) {
                0 if hl >= nl => { let o = rng.below(hl - nl + 1); h[o..o + nl].to_vec() }
                1 => { let u: Vec<u8> = (0..1 + rng.below(3)).map(|_| alpha[rng.below(alpha.len())]).collect(); u.iter().cycle().take(nl).copied().collect() }
                _ => (0..nl).map(|_| alpha[rng.below(alpha.len())]).collect(),
            };
            let f = nvol::memmem::Finder::new(&n);
            assert_eq!(f.find(&h), memchr::memmem::find(&h, &n), "hay={:?} needle={:?}", std::str::from_utf8(&h), std::str::from_utf8(&n));
            let a: Vec<usize> = f.find_iter(&h).collect();
            let b: Vec<usize> = memchr::memmem::find_iter(&h, &n).collect();
            assert_eq!(a, b);
        }
    }

    #[test]
    fn teddy_vs_aho_corasick() {
        use aho_corasick::packed::{Config, MatchKind};
        let mut rng = Rng(9001);
        let alphas: [&[u8]; 4] = [b"ab", b"abcd", b"the quick brown fox", b"aA01"];
        let mut built = 0;
        for _ in 0..20000 {
            let alpha = alphas[rng.below(alphas.len())];
            let npat = [1usize, 2, 3, 5, 8, 9, 16, 17, 32, 33, 40, 64, 65][rng.below(13)];
            let (lo, hi) = [(1usize, 1usize), (1, 3), (2, 2), (2, 5), (3, 3), (3, 8), (4, 4), (4, 9)][rng.below(8)];
            let hl = [0usize, 5, 16, 17, 18, 19, 31, 32, 33, 34, 35, 48, 64, 100, 300, 2000][rng.below(16)];
            let h: Vec<u8> = (0..hl).map(|_| alpha[rng.below(alpha.len())]).collect();
            let pats: Vec<Vec<u8>> = (0..npat).map(|_| {
                let ln = lo + rng.below(hi - lo + 1);
                if hl >= ln && rng.below(3) == 0 { let o = rng.below(hl - ln + 1); h[o..o + ln].to_vec() }
                else { (0..ln).map(|_| alpha[rng.below(alpha.len())]).collect() }
            }).collect();
            let theirs = Config::new().match_kind(MatchKind::LeftmostFirst).builder().extend(&pats).build();
            let ours = nvol::packed::Searcher::new(&pats);
            assert_eq!(ours.is_some(), theirs.is_some(), "buildable npat={npat} lens={lo}..{hi}");
            let (Some(a), Some(b)) = (ours, theirs) else { continue };
            built += 1;
            let start = if rng.below(4) == 0 { rng.below(hl + 1) } else { 0 };
            let end = if rng.below(4) == 0 { start + rng.below(hl - start + 1) } else { hl };
            let x = a.find_in(&h, start..end).map(|m| (m.pattern, m.start, m.end));
            let y = b.find_in(&h, aho_corasick::Span { start, end }).map(|m| (m.pattern().as_usize(), m.start(), m.end()));
            assert_eq!(x, y, "npat={npat} hay={:?} span={start}..{end}", String::from_utf8_lossy(&h));
            let xs: Vec<_> = a.find_iter(&h).map(|m| (m.pattern, m.start, m.end)).collect();
            let ys: Vec<_> = b.find_iter(&h).map(|m| (m.pattern().as_usize(), m.start(), m.end())).collect();
            assert_eq!(xs, ys);
        }
        assert!(built > 10000, "only {built} searchers built");
    }

    /// Byte-set search (one set, and a pair of sets at two offsets) against a
    /// naive scan: random sets with high bytes and duplicates, every length up
    /// to 200 at several alignments. Runs natively and under QEMU (NEON).
    #[test]
    fn byteset_vs_naive() {
        let mut rng = Rng(0x5DEECE66D);
        let pool = [0x00u8, 0x0A, 0x41, 0x5A, 0x61, 0x7A, 0x7F, 0x80, 0x8F, 0x9A, 0xC3, 0xF0, 0xFF];
        let mut checked = 0usize;
        for round in 0..3000usize {
            let set = |rng: &mut Rng| -> Vec<u8> {
                if rng.below(5) == 0 {
                    // range set: [0, t] plus 0..4 bytes above it (4: too many, nibble lookup)
                    let t = [1usize, 5, 8, 31, 31, 64, 200, 255][rng.below(8)];
                    let mut v: Vec<u8> = (0..=t as u8).collect();
                    for _ in 0..rng.below(5) { if t < 255 { v.push((t + 1 + rng.below(255 - t)) as u8); } }
                    return v;
                }
                if rng.below(5) == 0 {
                    // 7-16 distinct bytes without 0: the pcmpistri member list (SSE4.2 entry)
                    let mut v: Vec<u8> = Vec::new();
                    let n = 7 + rng.below(10);
                    while v.len() < n { let b = 1 + rng.below(255) as u8; if !v.contains(&b) { v.push(b); } }
                    return v;
                }
                if rng.below(4) == 0 {
                    // unique-nibble set: at most one member per low nibble
                    let mut los: Vec<u8> = (0..16).collect();
                    for i in (1..16).rev() { let j = rng.below(i + 1); los.swap(i, j); }
                    let n = [1usize, 2, 4, 7, 16][rng.below(5)];
                    return los[..n].iter().map(|&lo| ((rng.below(16) as u8) << 4) | lo).collect();
                }
                let k = [1usize, 2, 3, 4, 5, 8, 16, 20][rng.below(8)];
                (0..k).map(|_| if rng.below(3) == 0 { rng.below(256) as u8 } else { pool[rng.below(pool.len())] }).collect()
            };
            let (s1, s2) = (set(&mut rng), set(&mut rng));
            let (i1, i2) = ([0usize, 1, 2, 5, 17, 33][rng.below(6)], [0usize, 1, 3, 7, 31, 40][rng.below(6)]);
            let len = rng.below(201);
            let off = rng.below(64);
            let filler: Vec<u8> = (0..=255u8).filter(|b| !s1.contains(b) && !s2.contains(b)).collect();
            let mut buf = vec![0u8; off + len + 64];
            for b in buf.iter_mut() { *b = if filler.is_empty() { b'x' } else { filler[rng.below(filler.len())] }; }
            for _ in 0..(round % 4) {
                if len > 0 { let at = off + rng.below(len); buf[at] = if rng.below(2) == 0 { s1[rng.below(s1.len())] } else { s2[rng.below(s2.len())] }; }
            }
            let hay = &buf[off..off + len];
            let w1 = hay.iter().position(|b| s1.contains(b));
            let w2 = (0..len.saturating_sub(i1.max(i2))).find(|&p| s1.contains(&hay[p + i1]) && s2.contains(&hay[p + i2]));
            // negated search over a haystack made mostly of members of set 1
            let mut nbuf: Vec<u8> = (0..off + len + 64).map(|_| s1[rng.below(s1.len())]).collect();
            for _ in 0..(round % 3) { if len > 0 { let at = off + rng.below(len); nbuf[at] = rng.below(256) as u8; } }
            let nhay = &nbuf[off..off + len];
            let wn = nhay.iter().position(|b| !s1.contains(b));
            let (mut cls, mut pst) = ([0u8; raw::NVOL_BYTESET_CLASS_BYTES], [0u8; raw::NVOL_BYTESET_PAIR_BYTES]);
            let (g1, g2, gn) = unsafe {
                raw::nvol_byteset_init(s1.as_ptr(), s1.len(), cls.as_mut_ptr());
                raw::nvol_byteset_pair_init(s1.as_ptr(), s1.len(), s2.as_ptr(), s2.len(), i1, i2, pst.as_mut_ptr());
                let (a, e) = (hay.as_ptr(), hay.as_ptr().add(len));
                let (na, ne) = (nhay.as_ptr(), nhay.as_ptr().add(len));
                (raw::nvol_find_byteset_raw(cls.as_ptr(), a, e), raw::nvol_find_byteset_pair_raw(pst.as_ptr(), a, e),
                 raw::nvol_find_not_byteset_raw(cls.as_ptr(), na, ne))
            };
            let ix = |p: *const u8| if p.is_null() { None } else { Some(p as usize - hay.as_ptr() as usize) };
            let nx = |p: *const u8| if p.is_null() { None } else { Some(p as usize - nhay.as_ptr() as usize) };
            assert_eq!((ix(g1), ix(g2), nx(gn)), (w1, w2, wn), "s1={s1:?} s2={s2:?} i=({i1},{i2}) len={len} off={off}");
            // every x86 entry this CPU can run, called directly: the dispatcher above only
            // reaches the best one (Rust has no test hook to force the others)
            #[cfg(target_arch = "x86_64")]
            unsafe {
                let (a, e) = (hay.as_ptr(), hay.as_ptr().add(len));
                let (na, ne) = (nhay.as_ptr(), nhay.as_ptr().add(len));
                let c = cls.as_ptr();
                let ctx = format!("s1={s1:?} len={len} off={off}");
                assert_eq!(ix(raw::nvol_find_byteset_entry_base(c, a, e)), w1, "find base {ctx}");
                assert_eq!(nx(raw::nvol_find_not_byteset_entry_base(c, na, ne)), wn, "not base {ctx}");
                if std::is_x86_feature_detected!("sse4.2") {
                    assert_eq!(nx(raw::nvol_find_not_byteset_entry_sse42(c, na, ne)), wn, "not sse4.2 {ctx}");
                }
                if std::is_x86_feature_detected!("avx2") {
                    assert_eq!(ix(raw::nvol_find_byteset_entry_avx2(c, a, e)), w1, "find avx2 {ctx}");
                    assert_eq!(nx(raw::nvol_find_not_byteset_entry_avx2(c, na, ne)), wn, "not avx2 {ctx}");
                }
            }
            // visitor: every chunk with candidates, in order, on the haystack and on a dense one
            // (bytes of both sets); once to the end, once stopping at a chunk
            let dlen = [40usize, 300, 1100, 5000][rng.below(4)];
            let both: Vec<u8> = s1.iter().chain(s2.iter()).copied().collect();
            let dense: Vec<u8> = (0..dlen).map(|_| if rng.below(5) == 0 { rng.below(256) as u8 } else { both[rng.below(both.len())] }).collect();
            let step = if cfg!(target_arch = "aarch64") { 4 } else { 1 };
            for h in [hay, &dense[..]] {
                let want: Vec<usize> = (0..h.len().saturating_sub(i1.max(i2)))
                    .filter(|&p| s1.contains(&h[p + i1]) && s2.contains(&h[p + i2])).collect();
                let (base, end) = (h.as_ptr(), unsafe { h.as_ptr().add(h.len()) });
                let mut chunks: Vec<(usize, u64)> = Vec::new();
                let r = unsafe {
                    raw::nvol_find_byteset_pair_visit_raw(pst.as_ptr(), base, end, &mut |at: *const u8, m: u64| {
                        chunks.push((at as usize - base as usize, m));
                        false
                    })
                };
                assert!(r.is_null() && chunks.iter().all(|c| c.1 != 0));
                let got: Vec<usize> = chunks.iter()
                    .flat_map(|&(o, m)| (0..64).filter(move |b| m >> b & 1 == 1).map(move |b| o + b / step)).collect();
                assert_eq!(got, want, "visit s1={s1:?} s2={s2:?} i=({i1},{i2}) len={}", h.len());
                if !chunks.is_empty() {
                    let stop = rng.below(chunks.len());
                    let mut seen = Vec::new();
                    let r = unsafe {
                        raw::nvol_find_byteset_pair_visit_raw(pst.as_ptr(), base, end, &mut |at: *const u8, m: u64| {
                            seen.push((at as usize - base as usize, m));
                            seen.len() == stop + 1
                        })
                    };
                    assert_eq!(seen[..], chunks[..stop + 1]);
                    assert_eq!(r as usize - base as usize, chunks[stop].0);
                }
            }
            // copy-until over the haystack: exact prefix, return at the first member (or end),
            // nothing written outside dst[0, len) (guard bytes on both sides)
            {
                let doff = rng.below(32);
                let mut dst = vec![0xABu8; doff + len + 64];
                let stop = hay.iter().position(|b| s1.contains(b)).unwrap_or(len);
                let r = unsafe {
                    raw::nvol_copy_until_byteset_raw(cls.as_ptr(), hay.as_ptr(), hay.as_ptr().add(len), dst.as_mut_ptr().add(doff))
                };
                assert_eq!(r as usize - hay.as_ptr() as usize, stop, "copy s1={s1:?} len={len}");
                assert_eq!(&dst[doff..doff + stop], &hay[..stop]);
                assert!(dst[..doff].iter().all(|&b| b == 0xAB) && dst[doff + len..].iter().all(|&b| b == 0xAB));
            }
            checked += 1;
        }
        assert_eq!(checked, 3000);
    }
}
