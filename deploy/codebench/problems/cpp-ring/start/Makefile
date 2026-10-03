CXX ?= g++
CXXFLAGS ?= -std=c++17 -Wall -Wextra -Werror -g -fsanitize=address,undefined -fno-sanitize-recover=all

test: build/test_ring
	./build/test_ring

build/test_ring: tests/test_ring.cpp tests/check.hpp ring.hpp ring.cpp
	mkdir -p build
	$(CXX) $(CXXFLAGS) -I. -o $@ tests/test_ring.cpp ring.cpp

clean:
	rm -rf build

.PHONY: test clean
