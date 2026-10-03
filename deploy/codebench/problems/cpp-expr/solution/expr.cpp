#include "expr.hpp"

#include <cctype>
#include <cmath>
#include <stdexcept>

namespace {

class Parser {
public:
    explicit Parser(const std::string& s) : s_(s) {}

    double run() {
        double v = expr();
        skip();
        if (i_ != s_.size()) throw std::invalid_argument("unexpected text");
        return v;
    }

private:
    const std::string& s_;
    std::size_t i_ = 0;

    void skip() {
        while (i_ < s_.size() && std::isspace(static_cast<unsigned char>(s_[i_]))) ++i_;
    }
    bool eat(char c) {
        skip();
        if (i_ < s_.size() && s_[i_] == c) {
            ++i_;
            return true;
        }
        return false;
    }
    double expr() {
        double v = term();
        for (;;) {
            if (eat('+')) v += term();
            else if (eat('-')) v -= term();
            else return v;
        }
    }
    double term() {
        double v = unary();
        for (;;) {
            if (eat('*')) {
                v *= unary();
            } else if (eat('/')) {
                double d = unary();
                if (d == 0) throw std::domain_error("division by zero");
                v /= d;
            } else {
                return v;
            }
        }
    }
    double unary() {
        if (eat('-')) return -unary();
        return power();
    }
    double power() {
        double base = atom();
        if (eat('^')) return std::pow(base, unary());
        return base;
    }
    double atom() {
        if (eat('(')) {
            double v = expr();
            if (!eat(')')) throw std::invalid_argument("missing )");
            return v;
        }
        skip();
        std::size_t start = i_;
        while (i_ < s_.size() && std::isdigit(static_cast<unsigned char>(s_[i_]))) ++i_;
        if (i_ == start) throw std::invalid_argument("expected a number");
        if (i_ < s_.size() && s_[i_] == '.') {
            ++i_;
            std::size_t frac = i_;
            while (i_ < s_.size() && std::isdigit(static_cast<unsigned char>(s_[i_]))) ++i_;
            if (i_ == frac) throw std::invalid_argument("expected digits after .");
        }
        return std::stod(s_.substr(start, i_ - start));
    }
};

}  // namespace

double evaluate(const std::string& text) { return Parser(text).run(); }
