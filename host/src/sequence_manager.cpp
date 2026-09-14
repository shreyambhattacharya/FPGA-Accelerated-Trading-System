#include "sequence_manager.hpp"

#include <limits>
#include <stdexcept>

namespace trading {

SequenceManager::SequenceManager(std::uint16_t capacity) : next_sequences_(capacity, 0) {
    if (capacity == 0 || capacity > 32) throw std::invalid_argument("sequence capacity must be between 1 and 32");
}

void SequenceManager::validate(std::uint16_t slot) const {
    if (slot >= next_sequences_.size()) throw std::out_of_range("sequence slot is outside configured capacity");
}

std::uint32_t SequenceManager::next(std::uint16_t slot) {
    validate(slot);
    auto& value = next_sequences_[slot];
    if (value == std::numeric_limits<std::uint32_t>::max()) throw SequenceOverflow{};
    return value++;
}

std::uint32_t SequenceManager::peek(std::uint16_t slot) const {
    validate(slot);
    return next_sequences_[slot];
}

void SequenceManager::reset(std::uint16_t slot) {
    validate(slot);
    next_sequences_[slot] = 0;
}

void SequenceManager::set_next(std::uint16_t slot, std::uint32_t next_sequence) {
    validate(slot);
    next_sequences_[slot] = next_sequence;
}

} // namespace trading
