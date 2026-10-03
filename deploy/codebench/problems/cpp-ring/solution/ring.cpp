#include "ring.hpp"

#include <algorithm>
#include <stdexcept>
#include <utility>

IntRing::IntRing(std::size_t capacity) : data_(nullptr), capacity_(capacity) {
    if (capacity == 0) throw std::invalid_argument("capacity must be positive");
    data_ = new int[capacity];
}

IntRing::~IntRing() { delete[] data_; }

IntRing::IntRing(const IntRing& o)
    : data_(o.capacity_ ? new int[o.capacity_] : nullptr), capacity_(o.capacity_), head_(o.head_), count_(o.count_) {
    std::copy(o.data_, o.data_ + o.capacity_, data_);
}

IntRing& IntRing::operator=(const IntRing& o) {
    if (this != &o) {
        IntRing copy(o);
        *this = std::move(copy);
    }
    return *this;
}

IntRing::IntRing(IntRing&& o) noexcept : data_(o.data_), capacity_(o.capacity_), head_(o.head_), count_(o.count_) {
    o.data_ = nullptr;
    o.capacity_ = o.head_ = o.count_ = 0;
}

IntRing& IntRing::operator=(IntRing&& o) noexcept {
    if (this != &o) {
        delete[] data_;
        data_ = o.data_;
        capacity_ = o.capacity_;
        head_ = o.head_;
        count_ = o.count_;
        o.data_ = nullptr;
        o.capacity_ = o.head_ = o.count_ = 0;
    }
    return *this;
}

void IntRing::push(int value) {
    if (capacity_ == 0) return;
    if (count_ == capacity_) {
        data_[head_] = value;
        head_ = (head_ + 1) % capacity_;
        return;
    }
    data_[(head_ + count_) % capacity_] = value;
    ++count_;
}

int IntRing::pop() {
    if (count_ == 0) throw std::out_of_range("empty ring");
    int value = data_[head_];
    head_ = (head_ + 1) % capacity_;
    --count_;
    return value;
}

int IntRing::at(std::size_t i) const {
    if (i >= count_) throw std::out_of_range("index past the end");
    return data_[(head_ + i) % capacity_];
}
