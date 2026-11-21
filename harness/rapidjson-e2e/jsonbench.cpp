// rapidjson end to end: the same program built against stock rapidjson (SSE2 or
// SSE4.2) and against rapidjson with RAPIDJSON_NVOL.
//
//   jsonbench check FILE...          one line per file and operation: error code
//                                    and offset, or output length and FNV-1a hash
//   jsonbench bench SECONDS FILE...  one line per file and operation: ns per run
//   jsonbench count OP N FILE        run one operation N times, for `perf stat` (subtract
//                                    the run with N = 0: loading and the first parse)
//
// Operations: parse (null-terminated, StringStream), parse_len (with length,
// MemoryStream), insitu (ParseInsitu on a copy), write and pretty (stringify a
// parsed document; the copy for insitu is part of its time).
#include "rapidjson/document.h"
#include "rapidjson/prettywriter.h"
#include "rapidjson/stringbuffer.h"
#include "rapidjson/writer.h"

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

using namespace rapidjson;

static std::string slurp(const char *path) {
    std::ifstream f(path, std::ios::binary);
    std::stringstream ss;
    ss << f.rdbuf();
    return ss.str();
}

static uint64_t fnv(const char *p, size_t n) {
    uint64_t h = 1469598103934665603ull;
    for (size_t i = 0; i < n; i++) { h ^= static_cast<uint8_t>(p[i]); h *= 1099511628211ull; }
    return h;
}

template <typename W> static std::string stringify(const Document &d) {
    StringBuffer sb;
    W w(sb);
    d.Accept(w);
    return std::string(sb.GetString(), sb.GetSize());
}

static void report(const char *file, const char *op, const Document &d, const std::string &out) {
    if (d.HasParseError())
        std::printf("%s %s error %d at %zu\n", file, op, static_cast<int>(d.GetParseError()), d.GetErrorOffset());
    else
        std::printf("%s %s ok %zu %016llx\n", file, op, out.size(), static_cast<unsigned long long>(fnv(out.data(), out.size())));
}

static int check(int argc, char **argv) {
    for (int i = 0; i < argc; i++) {
        const char *file = argv[i];
        std::string text = slurp(file);
        {
            Document d; d.Parse(text.c_str());
            report(file, "parse", d, d.HasParseError() ? "" : stringify<Writer<StringBuffer> >(d));
            if (!d.HasParseError()) report(file, "pretty", d, stringify<PrettyWriter<StringBuffer> >(d));
        }
        {
            Document d; d.Parse(text.data(), text.size());
            report(file, "parse_len", d, d.HasParseError() ? "" : stringify<Writer<StringBuffer> >(d));
        }
        {
            std::vector<char> buf(text.begin(), text.end()); buf.push_back('\0');
            Document d; d.ParseInsitu(buf.data());
            report(file, "insitu", d, d.HasParseError() ? "" : stringify<Writer<StringBuffer> >(d));
        }
    }
    return 0;
}

template <typename F> static double time_ns(double seconds, F f) {
    typedef std::chrono::steady_clock clock;
    f();   // warm-up
    long n = 0;
    clock::time_point t0 = clock::now(), t;
    do { f(); n++; t = clock::now(); } while (std::chrono::duration<double>(t - t0).count() < seconds);
    return std::chrono::duration<double, std::nano>(t - t0).count() / n;
}

static volatile size_t sink;

static int bench(double seconds, int argc, char **argv) {
    for (int i = 0; i < argc; i++) {
        const char *file = argv[i];
        std::string text = slurp(file);
        std::vector<char> buf(text.size() + 1);
        Document parsed; parsed.Parse(text.c_str());
        if (parsed.HasParseError()) { std::fprintf(stderr, "%s: parse error\n", file); return 1; }
        std::printf("%s parse %.0f\n", file, time_ns(seconds, [&] { Document d; d.Parse(text.c_str()); sink += d.IsObject(); }));
        std::printf("%s parse_len %.0f\n", file, time_ns(seconds, [&] { Document d; d.Parse(text.data(), text.size()); sink += d.IsObject(); }));
        std::printf("%s insitu %.0f\n", file, time_ns(seconds, [&] {
            std::memcpy(buf.data(), text.c_str(), text.size() + 1);
            Document d; d.ParseInsitu(buf.data()); sink += d.IsObject(); }));
        std::printf("%s write %.0f\n", file, time_ns(seconds, [&] {
            StringBuffer sb; Writer<StringBuffer> w(sb); parsed.Accept(w); sink += sb.GetSize(); }));
        std::printf("%s pretty %.0f\n", file, time_ns(seconds, [&] {
            StringBuffer sb; PrettyWriter<StringBuffer> w(sb); parsed.Accept(w); sink += sb.GetSize(); }));
        std::fflush(stdout);
    }
    return 0;
}

static int count(const char *op, long n, const char *file) {
    std::string text = slurp(file);
    std::vector<char> buf(text.size() + 1);
    Document parsed; parsed.Parse(text.c_str());
    if (parsed.HasParseError()) { std::fprintf(stderr, "%s: parse error\n", file); return 1; }
    std::string o(op);
    if (o != "parse" && o != "parse_len" && o != "insitu" && o != "write" && o != "pretty") {
        std::fprintf(stderr, "unknown op %s\n", op);
        return 2;
    }
    for (long i = 0; i < n; i++) {
        if (o == "parse") { Document d; d.Parse(text.c_str()); sink += d.IsObject(); }
        else if (o == "parse_len") { Document d; d.Parse(text.data(), text.size()); sink += d.IsObject(); }
        else if (o == "insitu") {
            std::memcpy(buf.data(), text.c_str(), text.size() + 1);
            Document d; d.ParseInsitu(buf.data()); sink += d.IsObject();
        }
        else if (o == "write") { StringBuffer sb; Writer<StringBuffer> w(sb); parsed.Accept(w); sink += sb.GetSize(); }
        else { StringBuffer sb; PrettyWriter<StringBuffer> w(sb); parsed.Accept(w); sink += sb.GetSize(); }
    }
    return 0;
}

int main(int argc, char **argv) {
    if (argc >= 2 && std::strcmp(argv[1], "check") == 0) return check(argc - 2, argv + 2);
    if (argc >= 3 && std::strcmp(argv[1], "bench") == 0) return bench(std::atof(argv[2]), argc - 3, argv + 3);
    if (argc == 5 && std::strcmp(argv[1], "count") == 0) return count(argv[2], std::atol(argv[3]), argv[4]);
    std::fprintf(stderr, "usage: jsonbench check FILE... | jsonbench bench SECONDS FILE... | jsonbench count OP N FILE\n");
    return 2;
}
