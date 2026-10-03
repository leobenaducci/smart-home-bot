CXX ?= g++
CXXFLAGS ?= -std=c++17 -Wall -Wextra -Werror -g -fsanitize=address,undefined

test: build/test_expr
	./build/test_expr

build/test_expr: tests/test_expr.cpp tests/check.hpp expr.hpp expr.cpp
	mkdir -p build
	$(CXX) $(CXXFLAGS) -I. -o $@ tests/test_expr.cpp expr.cpp

clean:
	rm -rf build

.PHONY: test clean
