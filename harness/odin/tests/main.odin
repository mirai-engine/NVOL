// Differential tests for the NVOL-generated Odin package against naive Odin
// references (the same cases as the Rust harness). Run:
//   odin run harness/odin/tests -collection:nvol=generated -o:speed
// x86_64 runs everything three times (test hook): AVX2, the baseline SSE2/SSSE3 paths,
// and SSE4.2 without AVX2 (which only the negated byte-set search uses).
package tests

import "core:fmt"
import "core:os"
import nvol "nvol:odin"

Rng :: struct { s: u64 }
next :: proc(r: ^Rng) -> u64 { r.s ~= r.s << 13; r.s ~= r.s >> 7; r.s ~= r.s << 17; return r.s }
below :: proc(r: ^Rng, n: int) -> int { return int(next(r) % u64(n)) }

failures := 0
check :: proc(cond: bool, what: string, args: ..any) {
	if !cond {
		failures += 1
		if failures < 20 { fmt.eprintln("FAIL", what, args) }
	}
}

has :: proc(ns: []u8, b: u8) -> bool { for n in ns { if n == b { return true } }; return false }
naive_find :: proc(ns: []u8, h: []u8) -> (int, bool) { for b, i in h { if has(ns, b) { return i, true } }; return -1, false }
naive_rfind :: proc(ns: []u8, h: []u8) -> (int, bool) {
	for i := len(h) - 1; i >= 0; i -= 1 { if has(ns, h[i]) { return i, true } }
	return -1, false
}
naive_count :: proc(n: u8, h: []u8) -> int { c := 0; for b in h { if b == n { c += 1 } }; return c }
naive_ascii :: proc(h: []u8) -> (int, bool) { for b, i in h { if b >= 0x80 { return i, true } }; return -1, false }
naive_memmem :: proc(h, n: []u8) -> (int, bool) {
	if len(n) > len(h) { return -1, false }
	outer: for i in 0..=len(h) - len(n) {
		for j in 0..<len(n) { if h[i + j] != n[j] { continue outer } }
		return i, true
	}
	return -1, false
}
naive_lf :: proc(pats: []string, h: []u8, start, end: int) -> (nvol.Teddy_Match, bool) {
	for at in start..<end {
		for p, pid in pats {
			if at + len(p) <= end && string(h[at:at + len(p)]) == p { return {pid, at, at + len(p)}, true }
		}
	}
	return {}, false
}

positional :: proc() {
	rng := Rng{0x9E3779B97F4A7C15}
	buf := make([]u8, 300 + 128)
	defer delete(buf)
	for ln in 0..<300 {
		for off in 0..<64 {
			pos := [5]int{-1, 0, ln / 2, max(ln - 1, 0), ln > 0 ? below(&rng, ln) : 0}
			for p in pos {
				if ln == 0 && p >= 0 { continue }
				for hit in ([2]u8{'z', 0x80}) {
					for &b in buf { b = 'a' }
					if off > 0 { buf[off - 1] = hit }
					buf[off + ln] = hit
					if p >= 0 { buf[off + p] = hit }
					h := buf[off:off + ln]
					n := [3]u8{'x', 'y', hit}
					a, aok := nvol.memchr(hit, h); b, bok := naive_find(n[2:], h)
					check(a == b && aok == bok, "memchr", ln, off, p)
					a, aok = nvol.memrchr(hit, h); b, bok = naive_rfind(n[2:], h)
					check(a == b && aok == bok, "memrchr", ln, off, p)
					a, aok = nvol.memchr2(n[1], hit, h); b, bok = naive_find(n[1:], h)
					check(a == b && aok == bok, "memchr2", ln, off, p)
					a, aok = nvol.memrchr2(n[1], hit, h); b, bok = naive_rfind(n[1:], h)
					check(a == b && aok == bok, "memrchr2", ln, off, p)
					a, aok = nvol.memchr3(n[0], n[1], hit, h); b, bok = naive_find(n[:], h)
					check(a == b && aok == bok, "memchr3", ln, off, p)
					a, aok = nvol.memrchr3(n[0], n[1], hit, h); b, bok = naive_rfind(n[:], h)
					check(a == b && aok == bok, "memrchr3", ln, off, p)
					check(nvol.count(hit, h) == naive_count(hit, h), "count", ln, off, p)
					check(nvol.count_parity(hit, h) == naive_count(hit, h), "count_parity", ln, off, p)
					a, aok = nvol.first_non_ascii(h); b, bok = naive_ascii(h)
					check(a == b && aok == bok, "first_non_ascii", ln, off, p)
				}
			}
		}
	}
}

iterators :: proc() {
	rng := Rng{77}
	for _ in 0..<1500 {
		ln := below(&rng, 3000)
		h := make([]u8, ln); defer delete(h)
		alph := "ab\nc"
		for &b in h { b = alph[below(&rng, 4)] }
		want := 0
		for b in h { if b == '\n' || b == 'c' { want += 1 } }
		it := nvol.memchr_iter(h, '\n', 'c')
		got, last := 0, -1
		for {
			i, ok := nvol.memchr_iter_next(&it)
			if !ok { break }
			check(i > last && (h[i] == '\n' || h[i] == 'c'), "iter order")
			last = i; got += 1
		}
		check(got == want, "iter count", got, want)
		// interleaved front/back meets in the middle without overlap
		it2 := nvol.memchr_iter(h, '\n')
		seen := 0
		for k := 0; ; k += 1 {
			ok: bool
			if k % 3 == 0 { _, ok = nvol.memchr_iter_next_back(&it2) } else { _, ok = nvol.memchr_iter_next(&it2) }
			if !ok { break }
			seen += 1
		}
		check(seen == naive_count('\n', h), "iter both ends")
		it3 := nvol.memchr_iter(h, '\n')
		check(nvol.memchr_iter_count(&it3) == naive_count('\n', h), "iter count kernel")
	}
}

memmem_tests :: proc() {
	rng := Rng{4242}
	alphas := [4]string{"ab", "abc", "a", "the quick brown fox"}
	lens := [14]int{0, 1, 5, 15, 16, 17, 31, 33, 48, 64, 100, 200, 400, 3000}
	nls := [14]int{0, 1, 2, 3, 4, 7, 8, 16, 31, 32, 33, 40, 64, 80}
	for _ in 0..<20000 {
		alpha := alphas[below(&rng, 4)]
		hl := lens[below(&rng, 14)]
		h := make([]u8, hl); defer delete(h)
		for &b in h { b = alpha[below(&rng, len(alpha))] }
		nl := nls[below(&rng, 14)]
		n := make([]u8, nl); defer delete(n)
		switch below(&rng, 3) {
		case 0:
			if hl >= nl { o := below(&rng, hl - nl + 1); copy(n, h[o:o + nl]) } else { for &b in n { b = alpha[below(&rng, len(alpha))] } }
		case 1:
			u := 1 + below(&rng, 3)
			unit: [3]u8
			for i in 0..<u { unit[i] = alpha[below(&rng, len(alpha))] }
			for i in 0..<nl { n[i] = unit[i % u] }
		case:
			for &b in n { b = alpha[below(&rng, len(alpha))] }
		}
		f := nvol.memmem_finder(n)
		a, aok := nvol.memmem_find(&f, h)
		b, bok := naive_memmem(h, n)
		check(a == b && aok == bok, "memmem finder", hl, nl)
		a, aok = nvol.memmem(h, n)
		check(a == b && aok == bok, "memmem one-shot", hl, nl)
	}
}

teddy_tests :: proc() {
	rng := Rng{9001}
	alphas := [4]string{"ab", "abcd", "the quick brown fox", "aA01"}
	npats := [13]int{1, 2, 3, 5, 8, 9, 16, 17, 32, 33, 40, 64, 65}
	lrange := [8][2]int{{1, 1}, {1, 3}, {2, 2}, {2, 5}, {3, 3}, {3, 8}, {4, 4}, {4, 9}}
	hls := [16]int{0, 5, 16, 17, 18, 19, 31, 32, 33, 34, 35, 48, 64, 100, 300, 2000}
	built := 0
	for _ in 0..<12000 {
		alpha := alphas[below(&rng, 4)]
		np := npats[below(&rng, 13)]
		lr := lrange[below(&rng, 8)]
		hl := hls[below(&rng, 16)]
		h := make([]u8, hl); defer delete(h)
		for &b in h { b = alpha[below(&rng, len(alpha))] }
		pats := make([]string, np); defer delete(pats)
		storage := make([dynamic]u8, 0, np * 9); defer delete(storage)
		for i in 0..<np {
			ln := lr[0] + below(&rng, lr[1] - lr[0] + 1)
			st := len(storage)
			if hl >= ln && below(&rng, 3) == 0 {
				o := below(&rng, hl - ln + 1)
				append(&storage, ..h[o:o + ln])
			} else {
				for _ in 0..<ln { append(&storage, alpha[below(&rng, len(alpha))]) }
			}
			pats[i] = string(storage[st:st + ln])
		}
		minlen := 1 << 30
		for p in pats { minlen = min(minlen, len(p)) }
		m := min(4, minlen)
		when ODIN_ARCH == .amd64 {
			want_build := np <= 64 && !(m == 1 && np > 16)
		} else {
			want_build := np <= 64          // aho-corasick's NEON branch never rejects
		}
		t, ok := nvol.teddy_build(pats)
		check(ok == want_build, "teddy buildable", np, m)
		if !ok { continue }
		defer nvol.teddy_destroy(&t)
		built += 1
		start := below(&rng, 4) == 0 ? below(&rng, hl + 1) : 0
		end := below(&rng, 4) == 0 ? start + below(&rng, hl - start + 1) : hl
		x, xok := nvol.teddy_find_in(&t, h, start, end)
		y, yok := naive_lf(pats, h, start, end)
		check(xok == yok && x == y, "teddy find_in", np, hl, start, end, x, y)
	}
	check(built > 6000, "teddy built enough", built)
}

// Random byte set: range sets, unique-nibble sets, 7-16 distinct bytes without 0 (the
// pcmpistri member list of the SSE4.2 entry) and arbitrary small/large sets.
rand_set :: proc(rng: ^Rng, out: ^[dynamic]u8) {
	clear(out)
	pool := [13]u8{0x00, 0x0A, 0x41, 0x5A, 0x61, 0x7A, 0x7F, 0x80, 0x8F, 0x9A, 0xC3, 0xF0, 0xFF}
	switch below(rng, 5) {
	case 0:      // low range [0, t] plus 0-4 bytes above it
		T := [8]int{1, 5, 8, 31, 31, 64, 200, 255}
		t := T[below(rng, 8)]
		for b in 0..=t { append(out, u8(b)) }
		for _ in 0..<below(rng, 5) { if t < 255 { append(out, u8(t + 1 + below(rng, 255 - t))) } }
	case 1:      // 7-16 distinct bytes, no 0
		n := 7 + below(rng, 10)
		for len(out) < n { b := u8(1 + below(rng, 255)); if !has(out[:], b) { append(out, b) } }
	case 2:      // unique-nibble: at most one member per low nibble
		N := [5]int{1, 2, 4, 7, 16}
		n := N[below(rng, 5)]
		start := below(rng, 16)
		for i in 0..<n { append(out, u8(below(rng, 16) << 4 | (start + i) % 16)) }
	case:
		K := [8]int{1, 2, 3, 4, 5, 8, 16, 20}
		for _ in 0..<K[below(rng, 8)] { append(out, below(rng, 3) == 0 ? u8(below(rng, 256)) : pool[below(rng, 13)]) }
	}
}

byteset_tests :: proc() {
	rng := Rng{0x5DEECE66D}
	s1, s2: [dynamic]u8
	defer { delete(s1); delete(s2) }
	cls: [nvol.BYTESET_CLASS_BYTES]u8
	pst: [nvol.BYTESET_PAIR_BYTES]u8
	buf := make([]u8, 1100 + 128)
	nbuf := make([]u8, 1100 + 128)
	dst := make([]u8, 1100 + 128 + 64)
	defer { delete(buf); delete(nbuf); delete(dst) }
	for round in 0..<4000 {
		rand_set(&rng, &s1)
		rand_set(&rng, &s2)
		I1 := [6]int{0, 1, 2, 5, 17, 33}
		I2 := [6]int{0, 1, 3, 7, 31, 40}
		i1, i2 := I1[below(&rng, 6)], I2[below(&rng, 6)]
		LN := [4]int{below(&rng, 70), below(&rng, 201), 16 + below(&rng, 48), below(&rng, 1100)}
		ln := LN[round % 4]
		off := below(&rng, 64)
		// haystack: bytes of neither set, plus a few members
		filler: [dynamic]u8
		defer delete(filler)
		for b in 0..=255 { if !has(s1[:], u8(b)) && !has(s2[:], u8(b)) { append(&filler, u8(b)) } }
		for &b in buf { b = len(filler) > 0 ? filler[below(&rng, len(filler))] : 'x' }
		for _ in 0..<round % 4 {
			if ln > 0 { buf[off + below(&rng, ln)] = below(&rng, 2) == 0 ? s1[below(&rng, len(s1))] : s2[below(&rng, len(s2))] }
		}
		hay := buf[off:off + ln]
		// negated: mostly members of set 1
		for &b in nbuf { b = s1[below(&rng, len(s1))] }
		// (a quarter of the intruders are 0: pcmpistri ends its comparison at a 0 byte)
		for _ in 0..<round % 3 { if ln > 0 { nbuf[off + below(&rng, ln)] = below(&rng, 4) == 0 ? 0 : u8(below(&rng, 256)) } }
		nhay := nbuf[off:off + ln]

		nvol.nvol_byteset_init(raw_data(s1[:]), uint(len(s1)), &cls[0])
		nvol.nvol_byteset_pair_init(raw_data(s1[:]), uint(len(s1)), raw_data(s2[:]), uint(len(s2)), uint(i1), uint(i2), &pst[0])
		a, e := raw_data(hay), raw_data(buf[off + ln:])
		na, ne := raw_data(nhay), raw_data(nbuf[off + ln:])
		pos :: proc(p, base: [^]u8) -> int { return p == nil ? -1 : int(uintptr(p) - uintptr(base)) }

		w1, w2, wn := -1, -1, -1
		for b, i in hay { if has(s1[:], b) { w1 = i; break } }
		for p in 0..<max(ln - max(i1, i2), 0) { if has(s1[:], hay[p + i1]) && has(s2[:], hay[p + i2]) { w2 = p; break } }
		for b, i in nhay { if !has(s1[:], b) { wn = i; break } }
		check(pos(nvol.nvol_find_byteset_raw(&cls[0], a, e), a) == w1, "byteset find", s1[:], ln, off)
		check(pos(nvol.nvol_find_byteset_pair_raw(&pst[0], a, e), a) == w2, "byteset pair", s1[:], s2[:], i1, i2, ln)
		check(pos(nvol.nvol_find_not_byteset_raw(&cls[0], na, ne), na) == wn, "byteset not", s1[:], ln, off)

		// copy-until: exact prefix, return at the first member (or end), guard bytes untouched
		doff := below(&rng, 32)
		for &b in dst { b = 0xAB }
		r := nvol.nvol_copy_until_byteset_raw(&cls[0], a, e, raw_data(dst[doff:]))
		stop := w1 < 0 ? ln : w1
		ok := pos(r, a) == stop
		for i in 0..<stop { if dst[doff + i] != hay[i] { ok = false } }
		for i in 0..<doff { if dst[i] != 0xAB { ok = false } }
		for i in doff + ln..<len(dst) { if dst[i] != 0xAB { ok = false } }
		check(ok, "byteset copy_until", s1[:], ln, off, doff)
	}
}

main :: proc() {
	rounds := 3 when ODIN_ARCH == .amd64 else 1
	HOOK := [3]int{1, 0, 2}                     // AVX2, baseline (SSE2/SSSE3), SSE4.2 without AVX2
	NAME := [3]string{"avx2", "sse2/ssse3", "sse4.2"}
	for r in 0..<rounds {
		if ODIN_ARCH == .amd64 { nvol.nvol_test_set_avx2(HOOK[r]) }
		path := NAME[r] when ODIN_ARCH == .amd64 else "neon"
		positional(); fmt.printfln("[%v] positional ok=%v", path, failures == 0)
		iterators(); fmt.printfln("[%v] iterators ok=%v", path, failures == 0)
		memmem_tests(); fmt.printfln("[%v] memmem ok=%v", path, failures == 0)
		teddy_tests(); fmt.printfln("[%v] teddy ok=%v", path, failures == 0)
		byteset_tests(); fmt.printfln("[%v] byteset ok=%v", path, failures == 0)
	}
	nvol.nvol_test_set_avx2(-1)
	if failures > 0 { fmt.eprintln(failures, "failures"); os.exit(1) }
	fmt.println("all Odin tests passed")
}
