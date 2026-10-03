CXX ?= g++
CXXFLAGS ?= -std=c++17 -Wall -Wextra -Werror -g -fsanitize=address,undefined

test: build/test_lru
	./build/test_lru

build/test_lru: tests/test_lru.cpp tests/check.hpp lru.hpp
	mkdir -p build
	$(CXX) $(CXXFLAGS) -I. -o $@ tests/test_lru.cpp

clean:
	rm -rf build

.PHONY: test clean
