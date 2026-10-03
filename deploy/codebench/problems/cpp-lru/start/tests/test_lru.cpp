#include <string>

#include "check.hpp"
#include "lru.hpp"

int main() {
    {
        LruCache<int, std::string> c(2);
        c.put(1, "one");
        c.put(2, "two");
        CHECK(c.get(1) == std::optional<std::string>("one"));
        CHECK(c.get(2) == std::optional<std::string>("two"));
        CHECK(!c.get(3).has_value());
        CHECK(c.size() == 2);
    }
    {
        // Reading a key makes it the most recently used.
        LruCache<int, int> c(2);
        c.put(1, 10);
        c.put(2, 20);
        CHECK(c.get(1) == 10);
        c.put(3, 30);
        CHECK(c.contains(1));
        CHECK(!c.contains(2));
        CHECK(c.contains(3));
    }
    {
        // Writing a key that is already there replaces its value.
        LruCache<int, int> c(2);
        c.put(1, 10);
        c.put(2, 20);
        c.put(1, 11);
        CHECK(c.size() == 2);
        CHECK(c.get(1) == 11);
        CHECK(c.get(2) == 20);
    }
    CHECK_DONE();
}
