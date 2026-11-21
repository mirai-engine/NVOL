// Odin side of the cross-language benchmark (pair: harness/rust/src/bin/xbench.rs,
// identical text and cases). Run: odin run harness/odin/bench -collection:nvol=generated -o:speed
package bench

import "core:fmt"
import "core:time"
import nvol "nvol:odin"

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

sink: int

best :: proc(f: proc(h: []u8) -> int, h: []u8) -> f64 {
	iters := max(1, (64 << 20) / len(h))
	b := max(f64)
	for _ in 0..<9 {
		t := time.tick_now()
		for _ in 0..<iters { sink += f(h) }
		b = min(b, time.duration_seconds(time.tick_since(t)) / f64(iters))
	}
	return f64(len(h)) / b / 1e9
}

finder: nvol.Memmem_Finder
teddy: nvol.Teddy

main :: proc() {
	pats := []string{"Moriarty", "Lestrade", "Mycroft", "Hudson", "Adler", "Baskerville", "Gregson", "Hopkins",
		"Bradstreet", "Jones", "Wiggins", "Morstan", "Stamford", "Barrymore", "Musgrave", "Milverton"}
	finder = nvol.memmem_finder(transmute([]u8)string("Moriarty"))
	ok: bool
	teddy, ok = nvol.teddy_build(pats)
	assert(ok)
	for size in ([2]int{64 << 10, 1 << 20}) {
		h := text(size)
		defer delete(h)
		fmt.printfln("size=%v memchr_absent=%.2f", size, best(proc(h: []u8) -> int { i, _ := nvol.memchr('Z', h); return i }, h))
		fmt.printfln("size=%v memchr3_absent=%.2f", size, best(proc(h: []u8) -> int { i, _ := nvol.memchr3('Z', '{', '~', h); return i }, h))
		fmt.printfln("size=%v count_nl=%.2f", size, best(proc(h: []u8) -> int { return nvol.count('\n', h) }, h))
		fmt.printfln("size=%v memchr_iter_nl=%.2f", size, best(proc(h: []u8) -> int {
			it := nvol.memchr_iter(h, '\n'); n := 0
			for { i, ok := nvol.memchr_iter_next(&it); if !ok { break }; n ~= i }
			return n
		}, h))
		fmt.printfln("size=%v memmem_absent=%.2f", size, best(proc(h: []u8) -> int { i, _ := nvol.memmem_find(&finder, h); return i }, h))
		fmt.printfln("size=%v teddy16_absent=%.2f", size, best(proc(h: []u8) -> int { m, _ := nvol.teddy_find(&teddy, h); return m.start }, h))
	}
	fmt.println("sink", sink % 7)
}
