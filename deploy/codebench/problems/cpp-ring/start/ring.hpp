#pragma once
#include <cstddef>

// A fixed-capacity ring of ints. push() onto a full ring overwrites the
// oldest value. Values come out oldest first.
//
// A ring owns its storage: copies are independent of the original, and a
// moved-from ring is left empty (size() == 0) and still safe to use, assign
// to and destroy.
class IntRing {
public:
    explicit IntRing(std::size_t capacity);  // std::invalid_argument when 0
    ~IntRing();

    void push(int value);
    int pop();                          // the oldest; std::out_of_range when empty
    int at(std::size_t i) const;        // i-th oldest; std::out_of_range past size()
    std::size_t size() const { return count_; }
    std::size_t capacity() const { return capacity_; }
    bool empty() const { return count_ == 0; }

private:
    int* data_;
    std::size_t capacity_;
    std::size_t head_ = 0;   // index of the oldest value
    std::size_t count_ = 0;
};
