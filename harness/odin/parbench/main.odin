// Odin side of the parity benchmark (harness/parity): one operation of the NVOL Odin module,
// N times, with the same inputs as harness/asm/bench.c, for hardware counters.
//
//     parbench OP SIZE N PATH
//
// OP: memchr memrchr memchr2 memchr3 count count_parity first_non_ascii memmem memmem2
//     byteset byteset_not pair teddy, json utf8 digits copy_and_find (as in bench.c)
// PATH: -1 autodetect, 0 SSE2/SSSE3, 1 AVX2 (the module's test hook, as in bench.c)
// Every call scans the whole buffer (nothing matches).
package parbench

import "base:intrinsics"
import "core:os"
import "core:strconv"
import nvol "nvol:odin"

buf: [(1 << 20) + 64]u8
spaces: [(1 << 20) + 64]u8
cls, ws: [nvol.BYTESET_CLASS_BYTES]u8
pair: [nvol.BYTESET_PAIR_BYTES]u8
// simdjson's operations (bench.c's REC, the same digits)
REC :: "{\"id\":12345678,\"name\":\"caf\xc3\xa9 \\\"q\\\"\",\"tags\":[\"a\",\"b\"],\"v\":[1.5,2.25,-3e8]}\n"
jbuf, dbuf: [(1 << 20) + 64]u8
jout: [4 * ((1 << 20) + 64)]u8
jstate: [nvol.JSON_STATE_BYTES]u8

main :: proc() {
	if len(os.args) != 5 { os.exit(2) }
	op := os.args[1]
	size, ok1 := strconv.parse_int(os.args[2])
	n, ok2 := strconv.parse_int(os.args[3])
	path, ok3 := strconv.parse_int(os.args[4])
	if !(ok1 && ok2 && ok3) || size > 1 << 20 { os.exit(2) }
	nvol.nvol_test_set_avx2(path)
	for i in 0..<len(buf) { buf[i] = u8('a' + i % 8) }    // a..h
	for i in 0..<len(spaces) { spaces[i] = ' ' }
	rec := REC
	for i in 0..<len(jbuf) { jbuf[i] = rec[i % len(rec)] }
	for i in 0..<len(dbuf) { dbuf[i] = u8('0' + (i * 7 + i / 8) % 10) }
	nvol.nvol_byteset_init(raw_data(string("xyz0")), 4, &cls[0])
	nvol.nvol_byteset_init(raw_data(string(" \t\n\r")), 4, &ws[0])
	nvol.nvol_byteset_pair_init(raw_data(string("xy")), 2, raw_data(string("q")), 1, 0, 1, &pair[0])
	fin := nvol.memmem_finder(transmute([]u8)string("needle12"))
	fin2 := nvol.memmem_finder(transmute([]u8)string("zq"))           // ugrep's -c zq
	ted, ok := nvol.teddy_build({"foo", "bar", "quux", "zebra", "xylo", "jump", "wizard", "yoyo"})
	if !ok { os.exit(3) }
	h := buf[:size]
	p, e := raw_data(h), ([^]u8)(&buf[size])
	sp, se := ([^]u8)(&spaces[0]), ([^]u8)(&spaces[size])
	sink := 0
	switch op {
	case "memchr":          for _ in 0..<n { i, _ := nvol.memchr('z', h); sink += i }
	case "memrchr":         for _ in 0..<n { i, _ := nvol.memrchr('z', h); sink += i }
	case "memchr2":         for _ in 0..<n { i, _ := nvol.memchr2('y', 'z', h); sink += i }
	case "memchr3":         for _ in 0..<n { i, _ := nvol.memchr3('x', 'y', 'z', h); sink += i }
	case "count":           for _ in 0..<n { sink += nvol.count('a', h) }
	case "count_parity":    for _ in 0..<n { sink += nvol.count_parity('a', h) }
	case "first_non_ascii": for _ in 0..<n { i, _ := nvol.first_non_ascii(h); sink += i }
	case "memmem":          for _ in 0..<n { i, _ := nvol.memmem_find(&fin, h); sink += i }
	case "memmem2":         for _ in 0..<n { i, _ := nvol.memmem_find(&fin2, h); sink += i }
	case "byteset":         for _ in 0..<n { sink += int(uintptr(nvol.nvol_find_byteset_raw(&cls[0], p, e))) }
	case "byteset_not":     for _ in 0..<n { sink += int(uintptr(nvol.nvol_find_not_byteset_raw(&ws[0], sp, se))) }
	case "pair":            for _ in 0..<n { sink += int(uintptr(nvol.nvol_find_byteset_pair_raw(&pair[0], p, e))) }
	case "teddy":           for _ in 0..<n { m, _ := nvol.teddy_find(&ted, h); sink += m.start }
	case "json":
		for _ in 0..<n {
			jstate = {}
			sink += int(nvol.nvol_json_index_raw(&jstate[0], &jbuf[0], uint(size / 64), 0, &jout[0]))
		}
	case "utf8":            for _ in 0..<n { sink += int(nvol.nvol_utf8_valid(&jbuf[0], uint(size))) }
	case "digits":
		for _ in 0..<n { for j in 0..<size / 8 { sink += int(nvol.nvol_parse_eight_digits(&dbuf[8 * j])) } }
	case "copy_and_find":
		for _ in 0..<n { for j in 0..<size / 32 { sink += int(nvol.nvol_json_copy_and_find(&jbuf[32 * j], &jout[32 * j])) } }
	case:                   os.exit(2)
	}
	// the result is used, so no call is dropped
	intrinsics.volatile_store(&sink, sink)
	os.exit(sink == 12345 ? 1 : 0)
}
