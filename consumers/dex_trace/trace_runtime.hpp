#pragma once
#include <cstddef>
#include <cstdint>
#include <span>

namespace mgmem_dex_trace {
void observed(std::span<const uint8_t> input, uint64_t offset, uint32_t site) noexcept;
inline uint8_t load(std::span<const uint8_t> input, uint64_t offset, uint32_t site) noexcept {
    const uint8_t value = input[static_cast<std::size_t>(offset)];
    observed(input, offset, site);
    return value;
}
}
