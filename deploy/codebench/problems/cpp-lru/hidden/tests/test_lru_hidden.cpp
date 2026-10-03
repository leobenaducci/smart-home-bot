#include <algorithm>
#include <random>
#include <string>
#include <utility>
#include <vector>

#include "check.hpp"
#include "lru.hpp"

int main() {
    CHECK_THROWS((LruCache<int, int>(0)), std::invalid_argument);
    {
        LruCache<std::string, int> c(1);
        c.put("a", 1);
        c.put("b", 2);
        CHECK(!c.contains("a"));
        CHECK(c.get("b") == 2);
        c.put("b", 3);
        CHECK(c.size() == 1 && c.get("b") == 3);
    }
    {
        // Updating a key counts as a use: the other one goes first.
        LruCache<int, int> c(2);
        c.put(1, 10);
        c.put(2, 20);
        c.put(1, 11);
        c.put(3, 30);
        CHECK(c.contains(1) && !c.contains(2) && c.contains(3));
    }
    {
        // Updating a key in a full cache evicts nothing.
        LruCache<int, int> c(2);
        c.put(1, 10);
        c.put(2, 20);
        c.put(2, 21);
        CHECK(c.contains(1) && c.contains(2) && c.size() == 2);
    }
    {
        // A missing get is not a use and changes nothing.
        LruCache<int, int> c(2);
        c.put(1, 10);
        c.put(2, 20);
        CHECK(!c.get(9).has_value());
        c.put(3, 30);
        CHECK(!c.contains(1) && c.contains(2) && c.contains(3));
    }
    {
        // Against a plain list, many random operations.
        std::mt19937 rng(7);
        LruCache<int, int> c(5);
        std::vector<std::pair<int, int>> ref;  // most recently used first
        for (int i = 0; i < 5000; ++i) {
            int key = static_cast<int>(rng() % 12);
            auto at = std::find_if(ref.begin(), ref.end(), [&](auto& p) { return p.first == key; });
            if (rng() % 2) {
                int value = static_cast<int>(rng() % 1000);
                c.put(key, value);
                if (at != ref.end()) ref.erase(at);
                else if (ref.size() == 5) ref.pop_back();
                ref.insert(ref.begin(), {key, value});
            } else {
                auto got = c.get(key);
                if (at == ref.end()) {
                    CHECK(!got.has_value());
                } else {
                    CHECK(got == at->second);
                    auto p = *at;
                    ref.erase(at);
                    ref.insert(ref.begin(), p);
                }
            }
            CHECK(c.size() == ref.size());
            if (check_failures() > 5) break;
        }
    }
    CHECK_DONE();
}
