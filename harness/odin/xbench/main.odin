// Same-process cross-language benchmark: Odin (generated/odin), NVOL-Rust and the
// reference crates (memchr 2.8.3, aho-corasick 1.1.5), timed interleaved, round by
// round, in one process. Separate processes differ by several percent on identical
// code, more than the differences being measured, so this is the comparison to trust.
//
//   cargo build --release --manifest-path harness/rust-ffi/Cargo.toml
//   odin run harness/odin/xbench -collection:nvol=generated -o:speed
package xbench

import "core:fmt"
import "core:time"
import nvol "nvol:odin"

when ODIN_OS == .Windows {
	foreign import rs {
		"../../rust-ffi/target/release/nvol_ffi_bench.lib",
		"system:kernel32.lib", "system:ntdll.lib", "system:userenv.lib", "system:ws2_32.lib", "system:dbghelp.lib",
	}
} else {
	foreign import rs "../../rust-ffi/target/release/libnvol_ffi_bench.a"
}

Rust_Case :: proc "c" (imp: u32, p: [^]u8, len: uint) -> uint

@(default_calling_convention = "c")
foreign rs {
	xb_init :: proc() ---
	xb_memchr_absent :: proc(imp: u32, p: [^]u8, len: uint) -> uint ---
	xb_memchr3_absent :: proc(imp: u32, p: [^]u8, len: uint) -> uint ---
	xb_count_nl :: proc(imp: u32, p: [^]u8, len: uint) -> uint ---
	xb_memchr_iter_nl :: proc(imp: u32, p: [^]u8, len: uint) -> uint ---
	xb_memmem_absent :: proc(imp: u32, p: [^]u8, len: uint) -> uint ---
	xb_teddy16_absent :: proc(imp: u32, p: [^]u8, len: uint) -> uint ---
}

text :: proc(n: int) -> []u8 {
	words := [16]string{"the ", "quick ", "brown ", "fox ", "jumps ", "over ", "lazy ", "dog ",
		"Sherlock ", "Holmes ", "said ", "to ", "Watson ", "and ", "then\n", "of "}
	v := make([dynamic]u8, 0, n + 16)
	x: u64 = 0x2545F4914F6CDD1D
	for len(v) < n {
		x ~= x << 13; x ~= x >> 7; x ~= x << 17
		append(&v, words[x % 16])
	}
	resize(&v, n)
	return v[:]
}

finder: nvol.Memmem_Finder
teddy: nvol.Teddy
sink: uint

Odin_Case :: proc(h: []u8) -> int

Case :: struct {
	name: string,
	odin: Odin_Case,
	rust: Rust_Case,
}

ROUNDS :: 15

// Seconds per call of one round: `iters` calls back to back.
round_odin :: proc(f: Odin_Case, h: []u8, iters: int) -> f64 {
	t := time.tick_now()
	for _ in 0..<iters { sink += uint(f(h)) }
	return time.duration_seconds(time.tick_since(t)) / f64(iters)
}

round_rust :: proc(f: Rust_Case, imp: u32, h: []u8, iters: int) -> f64 {
	t := time.tick_now()
	for _ in 0..<iters { sink += f(imp, raw_data(h), uint(len(h))) }
	return time.duration_seconds(time.tick_since(t)) / f64(iters)
}

main :: proc() {
	pats := []string{"Moriarty", "Lestrade", "Mycroft", "Hudson", "Adler", "Baskerville", "Gregson", "Hopkins",
		"Bradstreet", "Jones", "Wiggins", "Morstan", "Stamford", "Barrymore", "Musgrave", "Milverton"}
	finder = nvol.memmem_finder(transmute([]u8)string("Moriarty"))
	ok: bool
	teddy, ok = nvol.teddy_build(pats)
	assert(ok)
	xb_init()

	cases := []Case{
		{"memchr_absent", proc(h: []u8) -> int { i, _ := nvol.memchr('Z', h); return i }, xb_memchr_absent},
		{"memchr3_absent", proc(h: []u8) -> int { i, _ := nvol.memchr3('Z', '{', '~', h); return i }, xb_memchr3_absent},
		{"count_nl", proc(h: []u8) -> int { return nvol.count('\n', h) }, xb_count_nl},
		{"memchr_iter_nl", proc(h: []u8) -> int {
			it := nvol.memchr_iter(h, '\n'); n := 0
			for { i, ok := nvol.memchr_iter_next(&it); if !ok { break }; n ~= i }
			return n
		}, xb_memchr_iter_nl},
		{"memmem_absent", proc(h: []u8) -> int { i, _ := nvol.memmem_find(&finder, h); return i }, xb_memmem_absent},
		{"teddy16_absent", proc(h: []u8) -> int { m, _ := nvol.teddy_find(&teddy, h); return m.start }, xb_teddy16_absent},
	}
	fmt.println("GB/s, best of", ROUNDS, "interleaved rounds in one process")
	for size in ([2]int{64 << 10, 1 << 20}) {
		h := text(size)
		defer delete(h)
		iters := max(1, (32 << 20) / size)
		for c in cases {
			b := [3]f64{max(f64), max(f64), max(f64)}
			for _ in 0..<ROUNDS {
				b[0] = min(b[0], round_odin(c.odin, h, iters))
				b[1] = min(b[1], round_rust(c.rust, 0, h, iters))
				b[2] = min(b[2], round_rust(c.rust, 1, h, iters))
			}
			gb := [3]f64{f64(size) / b[0] / 1e9, f64(size) / b[1] / 1e9, f64(size) / b[2] / 1e9}
			fmt.printfln("size=%-8v %-15s odin=%.2f  nvol-rust=%.2f  reference=%.2f  odin/nvol-rust=%+.1f%%",
				size, c.name, gb[0], gb[1], gb[2], (gb[0] / gb[1] - 1) * 100)
		}
	}
	fmt.println("sink", sink % 7)
}
