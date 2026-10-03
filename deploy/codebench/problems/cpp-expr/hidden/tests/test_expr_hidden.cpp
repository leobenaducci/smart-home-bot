#include <stdexcept>

#include "check.hpp"
#include "expr.hpp"

int main() {
    CHECK_NEAR(evaluate("-2^2"), -4);
    CHECK_NEAR(evaluate("(-2)^2"), 4);
    CHECK_NEAR(evaluate("2^-1"), 0.5);
    CHECK_NEAR(evaluate("--3"), 3);
    CHECK_NEAR(evaluate("2 - -3"), 5);
    CHECK_NEAR(evaluate("  7  "), 7);
    CHECK_NEAR(evaluate("3.25*4"), 13);
    CHECK_NEAR(evaluate("1 + 2 * (3 - 4) / 5"), 0.6);
    CHECK_NEAR(evaluate("((((1))))"), 1);
    CHECK_NEAR(evaluate("100 / 10 / 5"), 2);
    CHECK_NEAR(evaluate("2 * 3 ^ 2"), 18);
    CHECK_NEAR(evaluate("(1 + 2) ^ (1 + 1)"), 9);
    CHECK_NEAR(evaluate("0.5 + 0.25"), 0.75);
    CHECK_THROWS(evaluate(""), std::invalid_argument);
    CHECK_THROWS(evaluate("   "), std::invalid_argument);
    CHECK_THROWS(evaluate("(1 + 2"), std::invalid_argument);
    CHECK_THROWS(evaluate("1 + 2)"), std::invalid_argument);
    CHECK_THROWS(evaluate("2 3"), std::invalid_argument);
    CHECK_THROWS(evaluate("2 $ 3"), std::invalid_argument);
    CHECK_THROWS(evaluate("1e3"), std::invalid_argument);
    CHECK_THROWS(evaluate("1."), std::invalid_argument);
    CHECK_THROWS(evaluate("()"), std::invalid_argument);
    CHECK_THROWS(evaluate("* 2"), std::invalid_argument);
    CHECK_THROWS(evaluate("1 / (2 - 2)"), std::domain_error);
    CHECK_DONE();
}
