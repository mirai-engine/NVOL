// C++ smoke test for the generated header-only nvol.hpp
#include "../../generated/cpp/nvol.hpp"
#include <cassert>
#include <cstdio>
#include <string>
#include <vector>
int main() {
    std::string s(10000, 'a'); s[7777] = 'z'; s[9000] = '\n';
    std::span<const std::uint8_t> h(reinterpret_cast<const std::uint8_t*>(s.data()), s.size());
    assert(nvol::memchr('z', h) == 7777u);
    assert(nvol::memrchr('a', h) == 9999u);
    assert(nvol::memchr3('q', '\n', 'z', h) == 7777u);
    assert(!nvol::memchr2('q', 'w', h).has_value());
    assert(nvol::count('a', h) == 9998u);
    assert(!nvol::first_non_ascii(h).has_value());
    std::size_t lines = 0, last = 0;
    for (std::size_t i : nvol::memchr_iter('\n', h)) { lines++; last = i; }
    assert(lines == 1 && last == 9000u);
    nvol::memchr_iter it2('a', 'z', h);
    assert(it2.next_back() == 9999u && it2.next() == 0u);
    assert(nvol::memchr_iter('a', h).count() == 9998u);
    std::string pat = "aZa";
    s[100] = 'a'; s[101] = 'Z'; s[102] = 'a';
    std::span<const std::uint8_t> nd(reinterpret_cast<const std::uint8_t*>(pat.data()), pat.size());
    nvol::memmem::Finder mf(nd);
    assert(mf.find(h) == 100u);
    std::size_t hits = 0; mf.for_each(h, [&](std::size_t) { hits++; });
    assert(hits == 1);
    assert(nvol::memmem::find(h, nd) == 100u);
    std::vector<std::string> pats = {"Zaa", "aZa", "zz"};
    auto ps = nvol::packed::Searcher::build(pats);
    assert(ps.has_value());
    auto pm = ps->find(h);
    assert(pm && pm->pattern == 1 && pm->start == 100u && pm->end == 103u);
    std::size_t nm = 0; ps->for_each(h, [&](const nvol::packed::Match &) { nm++; });
    assert(nm == 1);
    std::puts("cpp smoke ok");
}
