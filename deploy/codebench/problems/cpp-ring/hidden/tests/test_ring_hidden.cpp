#include <deque>
#include <random>
#include <stdexcept>
#include <utility>

#include "check.hpp"
#include "ring.hpp"

int main() {
    CHECK_THROWS(IntRing(0), std::invalid_argument);
    {
        IntRing a(3), b(5);
        a.push(1);
        a.push(2);
        b.push(9);
        b = a;
        CHECK(b.size() == 2 && b.capacity() == 3 && b.at(1) == 2);
        a.push(3);
        a.push(4);
        CHECK(b.size() == 2 && b.at(0) == 1);
        b = b;
        CHECK(b.size() == 2 && b.at(0) == 1 && b.at(1) == 2);
        CHECK_THROWS(b.at(2), std::out_of_range);
    }
    {
        IntRing a(3);
        a.push(1);
        a.push(2);
        IntRing m(std::move(a));
        CHECK(m.size() == 2 && m.at(0) == 1);
        CHECK(a.size() == 0);
        IntRing n(1);
        n = std::move(m);
        CHECK(n.size() == 2 && n.at(1) == 2);
        CHECK(m.size() == 0);
        m = n;
        CHECK(m.size() == 2);
    }
    {
        // Against a deque, many random operations, wrapping many times.
        std::mt19937 rng(11);
        IntRing r(7);
        std::deque<int> ref;
        for (int i = 0; i < 20000; ++i) {
            if (rng() % 3 != 0) {
                int v = static_cast<int>(rng() % 1000);
                r.push(v);
                ref.push_back(v);
                if (ref.size() > 7) ref.pop_front();
            } else if (!ref.empty()) {
                CHECK(r.pop() == ref.front());
                ref.pop_front();
            }
            CHECK(r.size() == ref.size());
            if (!ref.empty()) CHECK(r.at(ref.size() - 1) == ref.back() && r.at(0) == ref.front());
            if (check_failures() > 5) break;
        }
    }
    CHECK_DONE();
}
