// Parsing from a static initializer that runs before NVOL's byte-set classes are
// built (GCC's init_priority puts it ahead of every default-priority global): the
// compat layer must fall back to plain loops instead of hanging or misparsing.
#include "rapidjson/document.h"
#include "rapidjson/stringbuffer.h"
#include "rapidjson/writer.h"

#include <cstdio>
#include <cstring>
#include <string>

using namespace rapidjson;

static const char *const kText =
    "  {\n    \"a\" :  [ 1 , 2 ,\t\"x\\ty\" ],\r\n  \"long string with no escapes at all, longer than a vector\": true }  ";

static std::string render() {
    std::string out;
    {
        Document d; d.Parse(kText);
        StringBuffer sb; Writer<StringBuffer> w(sb); d.Accept(w);
        out += d.HasParseError() ? "error" : sb.GetString();
    }
    out += "|";
    {
        Document d; d.Parse(kText, std::strlen(kText));
        StringBuffer sb; Writer<StringBuffer> w(sb); d.Accept(w);
        out += d.HasParseError() ? "error" : sb.GetString();
    }
    return out;
}

struct Early {
    std::string got;
#ifdef RAPIDJSON_NVOL
    bool built_before;
#endif
    Early() {
#ifdef RAPIDJSON_NVOL
        built_before = rapidjson_nvol::sets.ready;
#endif
        got = render();
    }
};

__attribute__((init_priority(101))) static Early early;

int main() {
    std::string late = render();
#ifdef RAPIDJSON_NVOL
    if (early.built_before) { std::printf("FAIL: the classes were already built\n"); return 1; }
#endif
    if (early.got != late || late.find("error") != std::string::npos) {
        std::printf("FAIL\n early: %s\n late:  %s\n", early.got.c_str(), late.c_str());
        return 1;
    }
    std::printf("prestatic ok: %s\n", late.c_str());
    return 0;
}
