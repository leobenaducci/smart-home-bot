#include <stdexcept>

#include "check.hpp"
#include "ring.hpp"

int main() {
    {
        IntRing r(3);
        r.push(1);
        r.push(2);
        CHECK(r.pop() == 1);
        r.push(3);
        r.push(4);
        CHECK(r.size() == 3);
        CHECK(r.at(0) == 2 && r.at(1) == 3 && r.at(2) == 4);
        CHECK(r.pop() == 2 && r.pop() == 3 && r.pop() == 4);
        CHECK(r.empty());
        CHECK_THROWS(r.pop(), std::out_of_range);
    }
    {
        // Full: the oldest is overwritten.
        IntRing r(2);
        r.push(1);
        r.push(2);
        r.push(3);
        CHECK(r.size() == 2 && r.at(0) == 2 && r.at(1) == 3);
    }
    {
        // A copy is its own ring.
        IntRing a(4);
        a.push(5);
        a.push(6);
        IntRing b = a;
        b.push(7);
        CHECK(a.size() == 2 && b.size() == 3);
        CHECK(b.at(0) == 5 && b.at(2) == 7);
        a.pop();
        CHECK(b.at(0) == 5);
    }
    CHECK_DONE();
}
