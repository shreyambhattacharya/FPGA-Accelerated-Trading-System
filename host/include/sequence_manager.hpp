#pragma once

#include <cstdint>
#include <stdexcept>
#include <vector>

namespace trading {

class SequenceOverflow final : public std::runtime_error {
public:
    SequenceOverflow() : std::runtime_error("per-slot FPGA sequence would overflow; reset/remap is required") {}
};

class SequenceManager {
public:
    explicit SequenceManager(std::uint16_t capacity = 32);

    // Sequence numbers start at zero. Calling next() reserves and increments
    // a number only when the caller is preparing an FPGA submission.
    std::uint32_t next(std::uint16_t slot);
    std::uint32_t peek(std::uint16_t slot) const;
    void reset(std::uint16_t slot);

    // Used for restoring a persisted host state and deterministic overflow
    // tests; production remapping should use reset().
    void set_next(std::uint16_t slot, std::uint32_t next_sequence);

private:
    void validate(std::uint16_t slot) const;
    std::vector<std::uint32_t> next_sequences_;
};

} // namespace trading
