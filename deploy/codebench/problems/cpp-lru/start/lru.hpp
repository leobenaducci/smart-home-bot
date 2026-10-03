#pragma once
#include <cstddef>
#include <list>
#include <optional>
#include <stdexcept>
#include <unordered_map>
#include <utility>

// A cache of at most `capacity` entries. When it is full, adding a new key
// evicts the least recently used one. Both get() of a present key and put()
// count as a use of that key.
template <typename K, typename V>
class LruCache {
public:
    explicit LruCache(std::size_t capacity) : capacity_(capacity) {
        if (capacity == 0) throw std::invalid_argument("capacity must be positive");
    }

    std::optional<V> get(const K& key) {
        auto it = index_.find(key);
        if (it == index_.end()) return std::nullopt;
        return it->second->second;
    }

    void put(const K& key, V value) {
        if (items_.size() == capacity_) {
            index_.erase(items_.back().first);
            items_.pop_back();
        }
        items_.emplace_front(key, std::move(value));
        index_[key] = items_.begin();
    }

    bool contains(const K& key) const { return index_.count(key) != 0; }
    std::size_t size() const { return items_.size(); }
    std::size_t capacity() const { return capacity_; }

private:
    using Item = std::pair<K, V>;
    std::size_t capacity_;
    std::list<Item> items_;  // most recently used first
    std::unordered_map<K, typename std::list<Item>::iterator> index_;
};
