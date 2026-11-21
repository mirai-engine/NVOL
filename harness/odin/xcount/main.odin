// Counter-friendly twin of xbench: runs ONE case of ONE implementation N times, so
// `perf stat` can count instructions and cycles per call (see xcounters.sh).
//
//   xcount odin|nvol|ref CASE SIZE N
//
// CASE is one of xbench's names (memchr_absent, memchr3_absent, count_nl, memchr_iter_nl,
// memmem_absent, teddy16_absent; plus byteset_absent, byteset_iter_range); nvol = NVOL-Rust,
// ref = memchr / aho-corasick crates (byteset has no reference: ref runs NVOL-Rust again).
// Built for linux_amd64 and linked against the Rust staticlib for x86_64-unknown-linux-musl
// (xcounters.sh does both).
package xcount

import "core:fmt"
import "core:os"
import "core:strconv"
import nvol "nvol:odin"

foreign import rs "../../rust-ffi/target/x86_64-unknown-linux-musl/release/libnvol_ffi_bench.a"

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
	xb_byteset_absent :: proc(imp: u32, p: [^]u8, len: uint) -> uint ---
	xb_byteset_iter_range :: proc(imp: u32, p: [^]u8, len: uint) -> uint ---
}

// Same haystack as xbench (and harness/rust-ffi's callers).
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
class_chain, class_range: [nvol.BYTESET_CLASS_BYTES]u8

// Every position of the class in `h`, xor-folded (one kernel call per match, like memchr_iter).
byteset_iter :: proc(c: ^[nvol.BYTESET_CLASS_BYTES]u8, h: []u8) -> int {
	base := raw_data(h); end := base[len(h):]; p := base; acc := 0
	for {
		r := nvol.nvol_find_byteset_raw(raw_data(c[:]), p, end)
		if r == nil { return acc }
		acc ~= int(uintptr(r) - uintptr(base))
		p = r[1:]
	}
}

Odin_Case :: proc(h: []u8) -> int

Case :: struct {
	name: string,
	odin: Odin_Case,
	rust: Rust_Case,
}

main :: proc() {
	if len(os.args) != 5 {
		fmt.eprintln("usage: xcount odin|nvol|ref CASE SIZE N")
		os.exit(2)
	}
	pats := []string{"Moriarty", "Lestrade", "Mycroft", "Hudson", "Adler", "Baskerville", "Gregson", "Hopkins",
		"Bradstreet", "Jones", "Wiggins", "Morstan", "Stamford", "Barrymore", "Musgrave", "Milverton"}
	finder = nvol.memmem_finder(transmute([]u8)string("Moriarty"))
	ok: bool
	teddy, ok = nvol.teddy_build(pats)
	assert(ok)
	xb_init()
	chain := "&<>|"
	nvol.nvol_byteset_init(raw_data(chain), uint(len(chain)), raw_data(class_chain[:]))
	rng: [34]u8
	for i in 0..<32 { rng[i] = u8(i) }
	rng[32], rng[33] = '"', '\\'
	nvol.nvol_byteset_init(raw_data(rng[:]), uint(len(rng)), raw_data(class_range[:]))

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
		{"byteset_absent", proc(h: []u8) -> int { return byteset_iter(&class_chain, h) }, xb_byteset_absent},
		{"byteset_iter_range", proc(h: []u8) -> int { return byteset_iter(&class_range, h) }, xb_byteset_iter_range},
	}
	imp := os.args[1]
	size, _ := strconv.parse_int(os.args[3])
	n, _ := strconv.parse_int(os.args[4])
	h := text(size)
	sink: uint
	for c in cases {
		if c.name != os.args[2] { continue }
		switch imp {
		case "odin": for _ in 0..<n { sink += uint(c.odin(h)) }
		case "nvol": for _ in 0..<n { sink += c.rust(0, raw_data(h), uint(len(h))) }
		case "ref":  for _ in 0..<n { sink += c.rust(1, raw_data(h), uint(len(h))) }
		case: fmt.eprintln("unknown implementation:", imp); os.exit(2)
		}
		fmt.println(sink)
		return
	}
	fmt.eprintln("unknown case:", os.args[2])
	os.exit(2)
}
