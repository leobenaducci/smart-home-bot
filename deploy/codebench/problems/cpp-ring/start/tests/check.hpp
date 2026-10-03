// A few assertions that count instead of stopping, so one run reports every
// failing check. main() ends with CHECK_DONE().
#pragma once
#include <iostream>

inline int& check_failures() {
    static int n = 0;
    return n;
}

#define CHECK(cond)                                                                    \
    do {                                                                               \
        if (!(cond)) {                                                                 \
            std::cerr << __FILE__ << ":" << __LINE__ << ": CHECK failed: " #cond "\n"; \
            ++check_failures();                                                        \
        }                                                                              \
    } while (0)

#define CHECK_NEAR(a, b)                                                                   \
    do {                                                                                   \
        double a_ = (a), b_ = (b);                                                         \
        if (!(a_ - b_ < 1e-9 && b_ - a_ < 1e-9)) {                                         \
            std::cerr << __FILE__ << ":" << __LINE__ << ": " #a " is " << a_ << ", not " << b_ \
                      << "\n";                                                             \
            ++check_failures();                                                            \
        }                                                                                  \
    } while (0)

#define CHECK_THROWS(expr, type)                                                       \
    do {                                                                               \
        bool thrown_ = false;                                                          \
        try {                                                                          \
            (void)(expr);                                                              \
        } catch (const type&) {                                                        \
            thrown_ = true;                                                            \
        } catch (...) {                                                                \
        }                                                                              \
        if (!thrown_) {                                                                \
            std::cerr << __FILE__ << ":" << __LINE__ << ": expected " #type " from " #expr \
                      << "\n";                                                         \
            ++check_failures();                                                        \
        }                                                                              \
    } while (0)

#define CHECK_DONE()                                                        \
    do {                                                                    \
        if (check_failures()) {                                             \
            std::cerr << check_failures() << " check(s) failed\n";          \
            return 1;                                                       \
        }                                                                   \
        std::cout << "all checks passed\n";                                 \
        return 0;                                                           \
    } while (0)
