// simdjson's stage 1 against nvol_json_index_raw, on the same document, for hardware counters.
//
//   stage1bench count IMPL N FILE    IMPL: nvol | simdjson; runs stage 1 over FILE N times
//   stage1bench same FILE            both give the same structural positions (exit 1 if not)
//
// The document is padded with spaces to a whole number of 64-byte blocks; simdjson gets the
// unpadded length and its own padding. Both validate UTF-8 in the same pass.
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <vector>

#include "simdjson.h"
extern "C" {
#include "nvol.h"
}

static std::vector<uint8_t> load(const char *path, size_t &len) {
    FILE *f = std::fopen(path, "rb");
    if (!f) { std::perror(path); std::exit(2); }
    std::fseek(f, 0, SEEK_END);
    len = (size_t)std::ftell(f);
    std::fseek(f, 0, SEEK_SET);
    std::vector<uint8_t> buf((len + 63) / 64 * 64 + simdjson::SIMDJSON_PADDING, ' ');
    if (std::fread(buf.data(), 1, len, f) != len) { std::perror(path); std::exit(2); }
    std::fclose(f);
    return buf;
}

struct Simdjson {
    std::unique_ptr<simdjson::internal::dom_parser_implementation> p;
    explicit Simdjson(size_t len) {
        const simdjson::implementation *impl = simdjson::get_active_implementation();
        if (impl->create_dom_parser_implementation(len, 1024, p)) { std::fputs("simdjson: alloc\n", stderr); std::exit(2); }
    }
    simdjson::error_code err = simdjson::SUCCESS;
    size_t run(const uint8_t *buf, size_t len) {
        err = p->stage1(buf, len, simdjson::stage1_mode::regular);
        if (err) return 0;
        return p->n_structural_indexes;
    }
};

// state words 4 and 5: UTF-8 error, ends inside a code point (an error at the end too)
static size_t nvol(const uint8_t *buf, size_t len, std::vector<uint32_t> &out, bool *utf8_ok = nullptr) {
    uint64_t st[(NVOL_JSON_STATE_BYTES + 7) / 8] = {0};
    size_t n = nvol_json_index_raw((uint8_t *)st, buf, (len + 63) / 64, 0, (uint8_t *)out.data());
    if (utf8_ok) *utf8_ok = st[4] == 0 && st[5] == 0;
    return n;
}

int main(int argc, char **argv) {
    if (argc == 3 && !std::strcmp(argv[1], "same")) {
        size_t len;
        auto buf = load(argv[2], len);
        std::vector<uint32_t> out(len + 64);
        bool utf8_ok;
        size_t n = nvol(buf.data(), len, out, &utf8_ok);
        Simdjson s(len);
        size_t m = s.run(buf.data(), len);
        if (s.err == simdjson::UTF8_ERROR || !utf8_ok) {
            bool same = (s.err == simdjson::UTF8_ERROR) == !utf8_ok;
            std::printf("%s UTF-8 error: nvol %d simdjson %d: %s\n", argv[2], !utf8_ok,
                        s.err == simdjson::UTF8_ERROR, same ? "same" : "DIFFERENT");
            return same ? 0 : 1;
        }
        // simdjson writes len, len, 0 past its last structural; n_structural_indexes leaves them out
        bool ok = m == n && std::memcmp(out.data(), s.p->structural_indexes.get(), n * 4) == 0;
        std::printf("%s nvol %zu simdjson %zu (%s): %s\n", argv[2], n, m,
                    simdjson::get_active_implementation()->name().c_str(), ok ? "same" : "DIFFERENT");
        return ok ? 0 : 1;
    }
    if (argc != 5 || std::strcmp(argv[1], "count")) {
        std::fputs("usage: stage1bench count nvol|simdjson N FILE | same FILE\n", stderr);
        return 2;
    }
    size_t len;
    auto buf = load(argv[4], len);
    long n = std::atol(argv[3]);
    size_t sum = 0;
    if (!std::strcmp(argv[2], "nvol")) {
        std::vector<uint32_t> out(len + 64);
        sum += nvol(buf.data(), len, out);  // the 0-call run pays the first touch too
        for (long i = 0; i < n; i++) sum += nvol(buf.data(), len, out);
    } else {
        Simdjson s(len);
        sum += s.run(buf.data(), len);
        for (long i = 0; i < n; i++) sum += s.run(buf.data(), len);
    }
    std::printf("%zu\n", sum);
    return 0;
}
