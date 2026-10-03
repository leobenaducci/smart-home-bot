#include <stdexcept>

#include "check.hpp"
#include "expr.hpp"

int main() {
    CHECK_NEAR(evaluate("1 + 2"), 3);
    CHECK_NEAR(evaluate("2 * 3 + 4"), 10);
    CHECK_NEAR(evaluate("2 * (3 + 4)"), 14);
    CHECK_NEAR(evaluate("10 / 4"), 2.5);
    CHECK_NEAR(evaluate("8 - 3 - 2"), 3);
    CHECK_NEAR(evaluate("-3 + 5"), 2);
    CHECK_NEAR(evaluate("2 ^ 3 ^ 2"), 512);
    CHECK_NEAR(evaluate("1.5 * 4"), 6);
    CHECK_THROWS(evaluate("1 +"), std::invalid_argument);
    CHECK_THROWS(evaluate("4 / 0"), std::domain_error);
    CHECK_DONE();
}
