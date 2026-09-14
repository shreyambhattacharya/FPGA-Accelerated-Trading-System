#include "symbol_registry.hpp"

#include <algorithm>
#include <stdexcept>

namespace trading {

SymbolRegistry::SymbolRegistry(std::uint16_t capacity) : capacity_(capacity), slots_(capacity) {
    if (capacity == 0 || capacity > 32) {
        throw std::invalid_argument("symbol registry capacity must be between 1 and 32");
    }
}

bool SymbolRegistry::valid_slot(std::uint16_t slot_value, std::string& error) const {
    if (slot_value >= capacity_) {
        error = "symbol slot is outside configured capacity";
        return false;
    }
    return true;
}

bool SymbolRegistry::duplicate_active_mapping(std::uint16_t slot_value, const std::string& symbol) const {
    for (std::uint16_t index = 0; index < capacity_; ++index) {
        if (index != slot_value && slots_[index].enabled && slots_[index].symbol == symbol) {
            return true;
        }
    }
    return false;
}

std::optional<std::uint16_t> SymbolRegistry::lookup_symbol(const std::string& symbol) const {
    for (std::uint16_t index = 0; index < capacity_; ++index) {
        if (slots_[index].symbol == symbol) return index;
    }
    return std::nullopt;
}

std::optional<std::string> SymbolRegistry::lookup_slot(std::uint16_t slot_value) const {
    std::string error;
    if (!valid_slot(slot_value, error) || !slots_[slot_value].symbol) return std::nullopt;
    return slots_[slot_value].symbol;
}

const SymbolSlot& SymbolRegistry::slot(std::uint16_t slot_value) const {
    if (slot_value >= capacity_) throw std::out_of_range("symbol slot is outside configured capacity");
    return slots_[slot_value];
}

bool SymbolRegistry::assign_symbol(std::uint16_t slot_value, const std::string& symbol, std::string& error) {
    if (!valid_slot(slot_value, error)) return false;
    if (symbol.empty()) {
        error = "symbol cannot be empty";
        return false;
    }
    if (duplicate_active_mapping(slot_value, symbol)) {
        error = "symbol is already assigned to an active slot";
        return false;
    }
    if (slots_[slot_value].enabled || slots_[slot_value].phase == RemapPhase::AwaitingFpgaClearAck) {
        error = "slot must be disabled and clear-acknowledged before assignment";
        return false;
    }
    slots_[slot_value].symbol = symbol;
    slots_[slot_value].enabled = false;
    slots_[slot_value].phase = RemapPhase::ReadyForAssignment;
    ++generation_;
    return true;
}

bool SymbolRegistry::clear_slot(std::uint16_t slot_value, std::string& error) {
    if (!valid_slot(slot_value, error)) return false;
    if (slots_[slot_value].phase == RemapPhase::AwaitingFpgaClearAck) {
        error = "cannot clear a slot while its FPGA clear is awaiting ACK";
        return false;
    }
    slots_[slot_value] = SymbolSlot{};
    ++generation_;
    return true;
}

bool SymbolRegistry::enable_slot(std::uint16_t slot_value, std::string& error) {
    if (!valid_slot(slot_value, error)) return false;
    auto& selected = slots_[slot_value];
    if (!selected.symbol) {
        error = "cannot enable an unassigned slot";
        return false;
    }
    if (selected.phase == RemapPhase::AwaitingFpgaClearAck) {
        error = "cannot enable before FPGA clear ACK";
        return false;
    }
    if (duplicate_active_mapping(slot_value, *selected.symbol)) {
        error = "symbol is already assigned to another active slot";
        return false;
    }
    selected.enabled = true;
    selected.phase = RemapPhase::Active;
    return true;
}

bool SymbolRegistry::disable_slot(std::uint16_t slot_value, std::string& error) {
    if (!valid_slot(slot_value, error)) return false;
    if (!slots_[slot_value].symbol) {
        error = "cannot disable an unassigned slot";
        return false;
    }
    slots_[slot_value].enabled = false;
    if (slots_[slot_value].phase == RemapPhase::Active) slots_[slot_value].phase = RemapPhase::ReadyForAssignment;
    return true;
}

bool SymbolRegistry::is_enabled(std::uint16_t slot_value) const {
    return slot_value < capacity_ && slots_[slot_value].enabled;
}

bool SymbolRegistry::begin_remap(std::uint16_t slot_value, std::string& error) {
    if (!valid_slot(slot_value, error)) return false;
    auto& selected = slots_[slot_value];
    if (!selected.symbol || !selected.enabled || selected.phase != RemapPhase::Active) {
        error = "remap requires an active assigned slot";
        return false;
    }
    selected.enabled = false;
    selected.phase = RemapPhase::AwaitingFpgaClearAck;
    ++generation_;
    return true;
}

bool SymbolRegistry::acknowledge_clear(std::uint16_t slot_value, std::string& error) {
    if (!valid_slot(slot_value, error)) return false;
    auto& selected = slots_[slot_value];
    if (selected.phase != RemapPhase::AwaitingFpgaClearAck) {
        error = "no FPGA clear is awaiting acknowledgement";
        return false;
    }
    selected.symbol.reset();
    selected.enabled = false;
    selected.phase = RemapPhase::ReadyForAssignment;
    ++generation_;
    return true;
}

bool SymbolRegistry::complete_remap(std::uint16_t slot_value, const std::string& symbol, std::string& error) {
    if (!valid_slot(slot_value, error)) return false;
    if (slots_[slot_value].phase != RemapPhase::ReadyForAssignment || slots_[slot_value].symbol) {
        error = "slot is not ready for a coordinated remap assignment";
        return false;
    }
    if (!assign_symbol(slot_value, symbol, error)) return false;
    return enable_slot(slot_value, error);
}

} // namespace trading
