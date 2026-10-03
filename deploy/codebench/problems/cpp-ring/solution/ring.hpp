#pragma once
#include <cstddef>

class IntRing {
public:
    explicit IntRing(std::size_t capacity);
    ~IntRing();
    IntRing(const IntRing& other);
    IntRing& operator=(const IntRing& other);
    IntRing(IntRing&& other) noexcept;
    IntRing& operator=(IntRing&& other) noexcept;

    void push(int value);
    int pop();
    int at(std::size_t i) const;
    std::size_t size() const { return count_; }
    std::size_t capacity() const { return capacity_; }
    bool empty() const { return count_ == 0; }

private:
    int* data_;
    std::size_t capacity_;
    std::size_t head_ = 0;
    std::size_t count_ = 0;
};
