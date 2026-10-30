#include <mgmem/mgmem.h>

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <limits>
#include <new>
#include <stdexcept>
#include <string>
#include <vector>

#if defined(_WIN32)
#define NOMINMAX
#include <windows.h>
#else
#include <unistd.h>
#endif

namespace {
using Clock = std::chrono::steady_clock;
constexpr uint64_t budget = 64u * 1024u * 1024u;
constexpr uint64_t maximum_nodes = 1024;
constexpr uint64_t maximum_accesses = 16u * 1024u * 1024u;
constexpr uint64_t assumed_cache_line = 64;
constexpr size_t streams = 4;

struct Case {
    uint64_t nodes = 256;
    uint64_t stride_pages = 1;
    uint64_t window_pages = 0;
    uint64_t seed = 1;
    uint64_t accesses = 262144;
    bool rotate = true;
    bool independent = false;
};

struct Node {
    uint64_t next;
    uint64_t value;
};

uint64_t elapsed_ns(Clock::time_point start) {
    return static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now() - start).count());
}

uint64_t checked_add(uint64_t a, uint64_t b) {
    if (b > std::numeric_limits<uint64_t>::max() - a) throw std::runtime_error("size_add_overflow");
    return a + b;
}

uint64_t checked_mul(uint64_t a, uint64_t b) {
    uint64_t result = 0;
    if (mgmem_checked_mul_v1(a, b, &result) != MGMEM_OK) throw std::runtime_error("size_mul_overflow");
    return result;
}

void require(mgmem_status result, const char* operation) {
    if (result != MGMEM_OK) throw std::runtime_error(std::string(operation) + "_status_" + std::to_string(result));
}

uint64_t page_size() {
#if defined(_WIN32)
    SYSTEM_INFO info{};
    GetSystemInfo(&info);
    return info.dwPageSize;
#else
    const long result = sysconf(_SC_PAGESIZE);
    if (result <= 0) throw std::runtime_error("page_size_unavailable");
    return static_cast<uint64_t>(result);
#endif
}

// Every pointer is valid only while this guard owns its lease. No escaped view.
struct OwnedRegion {
    mgmem_domain* domain = nullptr;
    mgmem_handle_v1 allocation{};
    mgmem_borrow_v1 borrow{};
    bool owner_live = false;
    bool lease_live = false;

    mgmem_status close() noexcept {
        if (!domain) return MGMEM_OK;
        mgmem_status result = MGMEM_OK;
        if (lease_live) {
            result = mgmem_borrow_release_v1(domain, borrow.lease);
            if (result != MGMEM_OK) return result;
            lease_live = false;
        }
        if (owner_live) {
            result = mgmem_release_v1(domain, allocation);
            if (result != MGMEM_OK) return result;
            owner_live = false;
        }
        result = mgmem_domain_begin_close_v1(domain);
        if (result != MGMEM_OK) return result;
        result = mgmem_domain_collect_v1(domain);
        if (result != MGMEM_OK) return result;
        result = mgmem_domain_drain_v1(domain, 0);
        if (result != MGMEM_OK) return result;
        result = mgmem_domain_destroy_v1(domain);
        if (result == MGMEM_OK) domain = nullptr;
        return result;
    }

    ~OwnedRegion() { (void)close(); }
    OwnedRegion() = default;
    OwnedRegion(const OwnedRegion&) = delete;
    OwnedRegion& operator=(const OwnedRegion&) = delete;
};

uint64_t random_step(uint64_t& state) {
    // Fully specified permutation generator, independent of std::shuffle implementation.
    state ^= state << 13;
    state ^= state >> 7;
    state ^= state << 17;
    return state;
}

uint64_t offset_for(const Case& c, uint64_t index, uint64_t page) {
    if (!c.rotate) return 0;
    const uint64_t lines = page / assumed_cache_line;
    return ((index + c.seed) % lines) * assumed_cache_line;
}

uint64_t access_nodes(const Case& c, unsigned char* base, uint64_t stride, uint64_t page,
                      const std::vector<uint64_t>& permutation, uint64_t count) {
    uint64_t sum = 0;
    if (!c.independent) {
        uint64_t current = permutation[0];
        for (uint64_t i = 0; i < count; ++i) {
            const volatile auto* node = reinterpret_cast<const volatile Node*>(base + current * stride + offset_for(c, current, page));
            sum += node->value;
            current = node->next;
        }
    } else {
        std::array<uint64_t, streams> positions{};
        for (size_t s = 0; s < streams; ++s) positions[s] = permutation[s * (c.nodes / streams)];
        for (uint64_t i = 0; i < count / streams; ++i) {
            // Independent dependency chains expose MLP; no fences are added to the hot loop.
            for (size_t s = 0; s < streams; ++s) {
                const uint64_t current = positions[s];
                const volatile auto* node = reinterpret_cast<const volatile Node*>(base + current * stride + offset_for(c, current, page));
                sum += node->value;
                positions[s] = node->next;
            }
        }
    }
    return sum;
}

void emit_case(const Case& c, uint64_t index) {
    const auto total_start = Clock::now();
    const uint64_t page = page_size();
    if (page < assumed_cache_line || (page & (page - 1)) != 0) throw std::runtime_error("unsupported_actual_page_geometry");
    if (c.nodes < streams || c.nodes > maximum_nodes || c.nodes % streams != 0 ||
        c.accesses == 0 || c.accesses > maximum_accesses || c.accesses % c.nodes != 0 ||
        c.stride_pages == 0 || c.seed == 0) throw std::runtime_error("invalid_workload_parameters");
    const uint64_t stride = checked_mul(c.stride_pages, page);
    const uint64_t window = checked_mul(c.window_pages, page);
    uint64_t max_offset = 0;
    for (uint64_t i = 0; i < c.nodes; ++i) {
        const uint64_t offset = offset_for(c, i, page);
        if (offset > page - sizeof(Node) || offset % alignof(Node) != 0) throw std::runtime_error("node_crosses_page_or_alignment");
        max_offset = std::max(max_offset, offset);
    }
    const uint64_t span = checked_add(checked_mul(c.nodes - 1, stride), checked_add(max_offset, sizeof(Node)));
    uint64_t reserved = 0;
    require(mgmem_checked_round_up_v1(checked_add(window, span), page, &reserved), "round_span");
    if (reserved > budget || reserved > static_cast<uint64_t>(std::numeric_limits<size_t>::max())) throw std::runtime_error("reserved_span_exceeds_budget");

    std::vector<uint64_t> permutation(static_cast<size_t>(c.nodes));
    for (uint64_t i = 0; i < c.nodes; ++i) permutation[static_cast<size_t>(i)] = i;
    uint64_t rng = c.seed;
    for (uint64_t i = c.nodes - 1; i > 0; --i) std::swap(permutation[static_cast<size_t>(i)], permutation[static_cast<size_t>(random_step(rng) % (i + 1))]);
    uint64_t order_hash = 14695981039346656037ull;
    for (uint64_t i : permutation) order_hash = (order_hash ^ i) * 1099511628211ull;

    OwnedRegion region;
    mgmem_domain_options_v1 options{};
    options.struct_size = sizeof(options);
    options.abi_major = MGMEM_ABI_MAJOR;
    options.abi_minor = MGMEM_ABI_MINOR;
    options.mode = MGMEM_SYSTEM;
    options.budget_bytes = budget;
    const auto allocation_start = Clock::now();
    require(mgmem_domain_create_v1(&options, &region.domain), "create");
    require(mgmem_allocate_v1(region.domain, reserved, page, &region.allocation), "allocate");
    region.owner_live = true;
    require(mgmem_borrow_acquire_v1(region.domain, region.allocation, 0, reserved, MGMEM_WRITE, &region.borrow), "acquire");
    region.lease_live = true;
    const uint64_t allocation_ns = elapsed_ns(allocation_start);
    auto* base = static_cast<unsigned char*>(region.borrow.data) + window;
    if (reinterpret_cast<uintptr_t>(region.borrow.data) % page != 0) throw std::runtime_error("provider_alignment_violation");

    const auto initialization_start = Clock::now();
    for (uint64_t i = 0; i < c.nodes; ++i) {
        const uint64_t logical = permutation[static_cast<size_t>(i)];
        auto* destination = base + logical * stride + offset_for(c, logical, page);
        new (destination) Node{permutation[static_cast<size_t>((i + 1) % c.nodes)], logical + 1};
    }
    const uint64_t initialization_ns = elapsed_ns(initialization_start);
    const auto warm_start = Clock::now();
    const uint64_t warm_count = checked_mul(c.nodes, 4);
    const uint64_t warm_checksum = access_nodes(c, base, stride, page, permutation, warm_count);
    const uint64_t warm_ns = elapsed_ns(warm_start);
    const uint64_t sum_per_cycle = checked_mul(c.nodes, checked_add(c.nodes, 1)) / 2;
    if (warm_checksum != checked_mul(sum_per_cycle, 4)) throw std::runtime_error("warm_checksum_mismatch");

    const auto steady_start = Clock::now();
    const uint64_t checksum = access_nodes(c, base, stride, page, permutation, c.accesses);
    const uint64_t steady_ns = elapsed_ns(steady_start);
    const uint64_t expected = checked_mul(c.accesses / c.nodes, sum_per_cycle);
    if (checksum != expected) throw std::runtime_error("steady_checksum_mismatch");
    mgmem_snapshot_v1 snapshot{};
    require(mgmem_domain_snapshot_v1(region.domain, &snapshot, sizeof(snapshot)), "snapshot");
    const auto release_start = Clock::now();
    require(region.close(), "release_close_destroy");
    const uint64_t release_ns = elapsed_ns(release_start);
    const uint64_t total_ns = elapsed_ns(total_start);
    const bool within_time_budget = total_ns <= 30000000000ull;
    std::cout << "{\"schema_version\":1,\"protocol_id\":\"MM-003-host-functional-diagnostic-v1\","
              << "\"case_id\":" << index << ",\"evidence_type\":\"HOST_DIAGNOSTIC\",\"success\":" << (within_time_budget ? "true" : "false")
              << ",\"failure_reason\":" << (within_time_budget ? "null" : "\"session_timeout\"")
              << ",\"provider\":\"System\",\"placement\":\"owned_span_only\",\"page_hint\":\"NOT_SELECTED\","
              << "\"mode\":\"" << (c.independent ? "independent" : "dependent") << "\",\"chain_count\":" << (c.independent ? streams : 1)
              << ",\"offset_policy\":\"" << (c.rotate ? "rotate" : "fixed") << "\",\"actual_base_page_bytes\":" << page
              << ",\"cache_line_bytes_assumed\":" << assumed_cache_line << ",\"cache_geometry_verified\":false"
              << ",\"node_count\":" << c.nodes << ",\"node_bytes\":" << sizeof(Node) << ",\"live_payload_bytes\":" << c.nodes * sizeof(Node)
              << ",\"workload_touched_pages\":" << c.nodes << ",\"logical_stride_pages\":" << c.stride_pages
              << ",\"base_window_offset_bytes\":" << window << ",\"span_bytes\":" << span << ",\"reserved_span_bytes\":" << reserved
              << ",\"seed\":" << c.seed << ",\"permutation_hash\":" << order_hash << ",\"access_count\":" << c.accesses
              << ",\"checksum\":" << checksum << ",\"expected_checksum\":" << expected << ",\"warm_access_count\":" << warm_count
              << ",\"phase_ns\":{\"allocation_and_acquire\":" << allocation_ns << ",\"initialization_and_first_touch\":" << initialization_ns
              << ",\"warm_access\":" << warm_ns << ",\"steady_access\":" << steady_ns << ",\"release\":" << release_ns << "}"
              << ",\"total_ns\":" << total_ns << ",\"phases_overlap\":false,\"unique_backing_peak_bytes\":" << snapshot.peak_backing_bytes
              << ",\"pmu\":\"UNAVAILABLE\",\"pfn_visibility\":\"UNAVAILABLE\",\"physical_contiguity\":\"UNKNOWN\",\"tlb_geometry\":\"UNKNOWN\""
              << ",\"thermal\":null,\"core_type\":null,\"migration_count\":null,\"page_faults\":null,\"rss_pss\":null,\"energy_joules\":null"
              << ",\"performance_qualification\":\"NOT_STARTED\",\"mechanism_evidence\":\"INCONCLUSIVE\"}\n";
    if (!within_time_budget) throw std::runtime_error("session_timeout");
}

uint64_t parse_number(const char* text) {
    if (!text || !*text) throw std::runtime_error("empty_number");
    uint64_t result = 0;
    for (const char* p = text; *p; ++p) {
        if (*p < '0' || *p > '9') throw std::runtime_error("invalid_unsigned_number");
        result = checked_add(checked_mul(result, 10), static_cast<uint64_t>(*p - '0'));
    }
    return result;
}
} // namespace

int main(int argc, char** argv) {
    try {
        Case c;
        bool matrix = false;
        for (int i = 1; i < argc; ++i) {
            const std::string argument = argv[i];
            if (argument == "--matrix") { matrix = true; continue; }
            if (argument == "--help") {
                std::cout << "mgmem_tlb_owned [--matrix] [--nodes N] [--stride-pages N] [--window-pages N] [--seed N] [--accesses N] [--offset rotate|fixed] [--mode dependent|independent]\n";
                return 0;
            }
            if (++i >= argc) throw std::runtime_error("missing_option_value");
            const std::string value = argv[i];
            if (argument == "--nodes") c.nodes = parse_number(argv[i]);
            else if (argument == "--stride-pages") c.stride_pages = parse_number(argv[i]);
            else if (argument == "--window-pages") c.window_pages = parse_number(argv[i]);
            else if (argument == "--seed") c.seed = parse_number(argv[i]);
            else if (argument == "--accesses") c.accesses = parse_number(argv[i]);
            else if (argument == "--offset" && (value == "rotate" || value == "fixed")) c.rotate = value == "rotate";
            else if (argument == "--mode" && (value == "dependent" || value == "independent")) c.independent = value == "independent";
            else throw std::runtime_error("unknown_option_or_value");
        }
        if (!matrix) emit_case(c, 0);
        else {
            const auto session_start = Clock::now();
            uint64_t index = 0;
            for (uint64_t stride : {1u, 2u, 3u}) for (uint64_t window : {0u, 1u})
                for (bool rotate : {true, false}) for (bool independent : {false, true}) for (uint64_t seed : {1u, 7u}) {
                    if (elapsed_ns(session_start) > 30000000000ull) throw std::runtime_error("matrix_timeout");
                    Case item = c;
                    item.stride_pages = stride;
                    item.window_pages = window;
                    item.rotate = rotate;
                    item.independent = independent;
                    item.seed = seed;
                    emit_case(item, index++);
                }
        }
        return 0;
    } catch (const std::exception& error) {
        // Messages are fixed identifiers plus decimal status codes, never user strings.
        std::cout << "{\"schema_version\":1,\"evidence_type\":\"HOST_DIAGNOSTIC\",\"success\":false,\"failure_reason\":\"" << error.what()
                  << "\",\"performance_qualification\":\"NOT_STARTED\",\"mechanism_evidence\":\"INCONCLUSIVE\"}\n";
        return 1;
    }
}
