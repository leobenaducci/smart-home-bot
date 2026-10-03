#include "ring.hpp"

#include <stdexcept>

IntRing::IntRing(std::size_t capacity) : data_(nullptr), capacity_(capacity) {
    if (capacity == 0) throw std::invalid_argument("capacity must be positive");
    data_ = new int[capacity];
}

IntRing::~IntRing() { delete[] data_; }

void IntRing::push(int value) {
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
    head_ = head_ + 1;
    --count_;
    return value;
}

int IntRing::at(std::size_t i) const {
    if (i >= count_) throw std::out_of_range("index past the end");
    return data_[head_ + i];
}
