#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace trading {

enum class RemapPhase {
    Empty,
    Active,
    AwaitingFpgaClearAck,
    ReadyForAssignment,
};

struct SymbolSlot {
    std::optional<std::string> symbol;
    bool enabled = false;
    RemapPhase phase = RemapPhase::Empty;
};

class SymbolRegistry {
public:
    explicit SymbolRegistry(std::uint16_t capacity = 32);

    std::uint16_t capacity() const { return capacity_; }
    std::uint64_t generation() const { return generation_; }

    std::optional<std::uint16_t> lookup_symbol(const std::string& symbol) const;
    std::optional<std::string> lookup_slot(std::uint16_t slot) const;
    const SymbolSlot& slot(std::uint16_t slot) const;

    bool assign_symbol(std::uint16_t slot, const std::string& symbol, std::string& error);
    bool clear_slot(std::uint16_t slot, std::string& error);
    bool enable_slot(std::uint16_t slot, std::string& error);
    bool disable_slot(std::uint16_t slot, std::string& error);
    bool is_enabled(std::uint16_t slot) const;

    // Software state machine for the future coordinated sequence:
    // disable -> CLEAR_SYMBOL_STATE -> wait for ACK -> assign -> enable.
    bool begin_remap(std::uint16_t slot, std::string& error);
    bool acknowledge_clear(std::uint16_t slot, std::string& error);
    bool complete_remap(std::uint16_t slot, const std::string& symbol, std::string& error);

private:
    bool valid_slot(std::uint16_t slot, std::string& error) const;
    bool duplicate_active_mapping(std::uint16_t slot, const std::string& symbol) const;

    std::uint16_t capacity_;
    std::uint64_t generation_ = 0;
    std::vector<SymbolSlot> slots_;
};

} // namespace trading
