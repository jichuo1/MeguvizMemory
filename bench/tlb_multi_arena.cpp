#include <mgmem/mgmem.h>

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <iostream>
#include <limits>
#include <memory>
#include <new>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#if defined(_WIN32)
#define NOMINMAX
#include <windows.h>
#else
#include <unistd.h>
#endif

namespace {
using Clock = std::chrono::steady_clock;
constexpr uint64_t max_backing = 64u * 1024u * 1024u;
constexpr uint64_t max_pages = 1024;
constexpr uint64_t max_accesses = 16u * 1024u * 1024u;
constexpr uint64_t line_bytes_assumed = 64;

struct Options {
    uint64_t arenas = 4, slots = 4, pages = 8, passes = 8;
    bool interleaved = true, rotate = true;
};

uint64_t add(uint64_t a, uint64_t b) {
    if (b > (std::numeric_limits<uint64_t>::max)() - a) throw std::runtime_error("size_add_overflow");
    return a + b;
}
uint64_t mul(uint64_t a, uint64_t b) {
    uint64_t result = 0;
    if (mgmem_checked_mul_v1(a, b, &result) != MGMEM_OK) throw std::runtime_error("size_mul_overflow");
    return result;
}
void require(mgmem_status result, const char *operation) {
    if (result != MGMEM_OK) throw std::runtime_error(std::string(operation) + "_status_" + std::to_string(result));
}
uint64_t elapsed(Clock::time_point start) {
    return static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now() - start).count());
}
uint64_t actual_page_size() {
#if defined(_WIN32)
    SYSTEM_INFO info{};
    GetSystemInfo(&info);
    return info.dwPageSize;
#else
    const long value = sysconf(_SC_PAGESIZE);
    if (value <= 0) throw std::runtime_error("page_size_unavailable");
    return static_cast<uint64_t>(value);
#endif
}
uint64_t parse_number(const char *text) {
    if (!text || !*text) throw std::runtime_error("empty_number");
    uint64_t result = 0;
    for (const char *p = text; *p; ++p) {
        if (*p < '0' || *p > '9') throw std::runtime_error("invalid_unsigned_number");
        result = add(mul(result, 10), static_cast<uint64_t>(*p - '0'));
    }
    return result;
}
struct Slot {
    mgmem_handle_v1 owner{};
    mgmem_borrow_v1 lease{};
    bool owner_live = false, lease_live = false;
};

struct Arena {
    mgmem_domain *domain = nullptr;
    std::vector<Slot> slots;
    bool closing = false, uncertain = false;

    explicit Arena(size_t count) : slots(count) {}
    Arena(const Arena &) = delete;
    Arena &operator=(const Arena &) = delete;
    ~Arena() { cleanup(); }

    void cleanup() noexcept {
        if (!domain || uncertain) return;
        for (auto &slot : slots) {
            if (slot.lease_live) {
                slot.lease_live = false;
                if (mgmem_borrow_release_v1(domain, slot.lease.lease) != MGMEM_OK) { uncertain = true; return; }
            }
            if (slot.owner_live) {
                slot.owner_live = false;
                if (mgmem_release_v1(domain, slot.owner) != MGMEM_OK) { uncertain = true; return; }
            }
        }
        if (!closing) {
            if (mgmem_domain_begin_close_v1(domain) != MGMEM_OK) { uncertain = true; return; }
            closing = true;
        }
        if (mgmem_domain_collect_v1(domain) != MGMEM_OK || mgmem_domain_drain_v1(domain, 0) != MGMEM_OK) {
            uncertain = true; return;
        }
        auto *to_destroy = domain;
        domain = nullptr; // Never retry a destroy whose outcome might be unknown.
        (void)mgmem_domain_destroy_v1(to_destroy);
    }

    void begin_close_with_leases() {
        require(mgmem_domain_begin_close_v1(domain), "begin_close");
        closing = true;
        for (auto &slot : slots) {
            slot.owner_live = false; // Do not retry a possibly consumed owner.
            const auto status = mgmem_release_v1(domain, slot.owner);
            if (status != MGMEM_OK) { uncertain = true; require(status, "owner_release"); }
        }
        mgmem_handle_v1 denied{};
        const auto denied_status = mgmem_allocate_v1(domain, 1, 8, &denied);
        if (denied_status == MGMEM_OK && mgmem_release_v1(domain, denied) != MGMEM_OK) uncertain = true;
        if (denied_status != MGMEM_IS_CLOSED)
            throw std::runtime_error("closed_domain_admitted_allocation");
        if (mgmem_domain_collect_v1(domain) != MGMEM_BUSY || mgmem_domain_drain_v1(domain, 0) != MGMEM_TIMEOUT)
            throw std::runtime_error("held_lease_did_not_prevent_close");
        mgmem_snapshot_v1 snap{};
        require(mgmem_domain_snapshot_v1(domain, &snap, sizeof(snap)), "closing_snapshot");
        uint64_t expected_retiring = 0;
        for (const auto &slot : slots) expected_retiring = add(expected_retiring, slot.lease.length);
        if (snap.state != MGMEM_CLOSING || snap.active_allocations != slots.size() ||
            snap.active_leases != slots.size() || snap.live_bytes != 0 ||
            snap.retiring_bytes != expected_retiring || snap.retained_bytes != 0 ||
            snap.backing_bytes != expected_retiring || snap.reserved_bytes != 0)
            throw std::runtime_error("closing_snapshot_mismatch");
    }

    void finish() {
        for (auto &slot : slots) {
            slot.lease_live = false;
            const auto status = mgmem_borrow_release_v1(domain, slot.lease.lease);
            if (status != MGMEM_OK) { uncertain = true; require(status, "lease_release"); }
        }
        require(mgmem_domain_collect_v1(domain), "collect");
        require(mgmem_domain_drain_v1(domain, 0), "drain");
        mgmem_snapshot_v1 snap{};
        require(mgmem_domain_snapshot_v1(domain, &snap, sizeof(snap)), "final_snapshot");
        if (snap.state != MGMEM_CLOSED || snap.backing_bytes != 0 || snap.reserved_bytes != 0 ||
            snap.active_allocations != 0 || snap.active_leases != 0)
            throw std::runtime_error("final_domain_not_zero");
        auto *to_destroy = domain;
        domain = nullptr;
        require(mgmem_domain_destroy_v1(to_destroy), "destroy");
    }
};

uint64_t offset_for(const Options &o, uint64_t page_size, uint64_t arena, uint64_t slot, uint64_t page) {
    if (!o.rotate) return 0;
    return ((arena + slot + page) % (page_size / line_bytes_assumed)) * line_bytes_assumed;
}
uint64_t initial_value(uint64_t arena, uint64_t slot, uint64_t page) {
    return 1 + arena * 100000 + slot * 1000 + page;
}
volatile uint64_t *cell(Arena &arena, const Options &o, uint64_t page_size, uint64_t a, uint64_t s, uint64_t p) {
    auto *base = static_cast<unsigned char *>(arena.slots[static_cast<size_t>(s)].lease.data);
    return reinterpret_cast<volatile uint64_t *>(base + p * page_size + offset_for(o, page_size, a, s, p));
}

void run(const Options &o) {
    const uint64_t page_size = actual_page_size();
    if (page_size < line_bytes_assumed || (page_size & (page_size - 1)) != 0)
        throw std::runtime_error("unsupported_actual_page_geometry");
    if (o.arenas < 2 || o.arenas > 8 || o.slots == 0 || o.slots > 16 ||
        o.pages == 0 || o.pages > 64 || o.passes < 2 || o.passes > 1024)
        throw std::runtime_error("invalid_workload_parameters");
    const uint64_t touched_pages = mul(mul(o.arenas, o.slots), o.pages);
    const uint64_t accesses = mul(touched_pages, o.passes);
    const uint64_t bytes_per_slot = mul(o.pages, page_size);
    const uint64_t bytes_per_domain = mul(o.slots, bytes_per_slot);
    const uint64_t total_bytes = mul(o.arenas, bytes_per_domain);
    if (touched_pages > max_pages || accesses > max_accesses || total_bytes > max_backing ||
        total_bytes > static_cast<uint64_t>((std::numeric_limits<size_t>::max)()))
        throw std::runtime_error("workload_exceeds_budget");

    const auto total_start = Clock::now();
    const auto allocation_start = Clock::now();
    std::vector<std::unique_ptr<Arena>> arenas;
    std::vector<std::pair<uintptr_t, uintptr_t>> live_ranges;
    arenas.reserve(static_cast<size_t>(o.arenas));
    live_ranges.reserve(static_cast<size_t>(mul(o.arenas, o.slots)));
    for (uint64_t a = 0; a < o.arenas; ++a) {
        auto holder = std::make_unique<Arena>(static_cast<size_t>(o.slots));
        mgmem_domain_options_v1 options{};
        options.struct_size = sizeof(options); options.abi_major = MGMEM_ABI_MAJOR; options.abi_minor = MGMEM_ABI_MINOR;
        options.mode = MGMEM_ARENA; options.budget_bytes = bytes_per_domain; options.arena_chunk_bytes = bytes_per_domain;
        require(mgmem_domain_create_v1(&options, &holder->domain), "create");
        for (auto &slot : holder->slots) {
            require(mgmem_allocate_v1(holder->domain, bytes_per_slot, page_size, &slot.owner), "allocate");
            slot.owner_live = true;
            require(mgmem_borrow_acquire_v1(holder->domain, slot.owner, 0, bytes_per_slot, MGMEM_WRITE, &slot.lease), "borrow");
            slot.lease_live = true;
            if (!slot.lease.data || slot.lease.length != bytes_per_slot ||
                reinterpret_cast<uintptr_t>(slot.lease.data) % page_size != 0)
                throw std::runtime_error("borrow_geometry_mismatch");
            const uintptr_t begin = reinterpret_cast<uintptr_t>(slot.lease.data);
            if (bytes_per_slot > (std::numeric_limits<uintptr_t>::max)() - begin)
                throw std::runtime_error("borrow_address_overflow");
            const uintptr_t end = begin + static_cast<uintptr_t>(bytes_per_slot);
            for (const auto &range : live_ranges)
                if (begin < range.second && range.first < end) throw std::runtime_error("live_ranges_overlap");
            live_ranges.emplace_back(begin, end);
        }
        mgmem_handle_v1 rejected{};
        const auto rejected_status = mgmem_allocate_v1(holder->domain, page_size, page_size, &rejected);
        if (rejected_status == MGMEM_OK && mgmem_release_v1(holder->domain, rejected) != MGMEM_OK) holder->uncertain = true;
        if (rejected_status != MGMEM_OUT_OF_BUDGET)
            throw std::runtime_error("budget_excess_not_rejected");
        mgmem_snapshot_v1 snap{};
        require(mgmem_domain_snapshot_v1(holder->domain, &snap, sizeof(snap)), "initial_snapshot");
        if (snap.backing_bytes != bytes_per_domain || snap.reserved_bytes != 0 ||
            snap.live_bytes != bytes_per_domain || snap.retiring_bytes != 0 || snap.retained_bytes != 0 ||
            snap.peak_backing_bytes != bytes_per_domain || snap.budget_bytes != bytes_per_domain ||
            snap.active_allocations != o.slots || snap.active_leases != o.slots)
            throw std::runtime_error("initial_budget_snapshot_mismatch");
        arenas.push_back(std::move(holder));
    }
    const uint64_t allocation_ns = elapsed(allocation_start);
    const auto initialization_start = Clock::now();
    uint64_t initial_sum = 0;
    for (uint64_t a = 0; a < o.arenas; ++a) for (uint64_t s = 0; s < o.slots; ++s) for (uint64_t p = 0; p < o.pages; ++p) {
        auto *address = const_cast<uint64_t *>(cell(*arenas[static_cast<size_t>(a)], o, page_size, a, s, p));
        const uint64_t value = initial_value(a, s, p);
        new (address) uint64_t(value);
        initial_sum = add(initial_sum, value);
    }
    const uint64_t initialization_ns = elapsed(initialization_start);
    const auto warm_start = Clock::now();
    uint64_t warm_checksum = 0;
    if (o.interleaved) {
        for (uint64_t s = 0; s < o.slots; ++s) for (uint64_t p = 0; p < o.pages; ++p)
            for (uint64_t a = 0; a < o.arenas; ++a)
                warm_checksum = add(warm_checksum, *cell(*arenas[static_cast<size_t>(a)], o, page_size, a, s, p));
    } else {
        for (uint64_t a = 0; a < o.arenas; ++a) for (uint64_t s = 0; s < o.slots; ++s)
            for (uint64_t p = 0; p < o.pages; ++p)
                warm_checksum = add(warm_checksum, *cell(*arenas[static_cast<size_t>(a)], o, page_size, a, s, p));
    }
    const uint64_t warm_ns = elapsed(warm_start);
    if (warm_checksum != initial_sum) throw std::runtime_error("warm_checksum_mismatch");
    uint64_t checksum = 0;
    const auto perform = [&](uint64_t first, uint64_t last) {
        const auto step = [&](uint64_t pass, uint64_t a, uint64_t s, uint64_t p) {
            volatile uint64_t *value = cell(*arenas[static_cast<size_t>(a)], o, page_size, a, s, p);
            const uint64_t next = add(*value, pass + 1);
            *value = next;
            checksum = add(checksum, next);
        };
        for (uint64_t pass = first; pass < last; ++pass) {
            if (elapsed(total_start) > 30000000000ull) throw std::runtime_error("session_timeout");
            if (o.interleaved) {
                for (uint64_t s = 0; s < o.slots; ++s) for (uint64_t p = 0; p < o.pages; ++p)
                    for (uint64_t a = 0; a < o.arenas; ++a) step(pass, a, s, p);
            } else {
                for (uint64_t a = 0; a < o.arenas; ++a) for (uint64_t s = 0; s < o.slots; ++s)
                    for (uint64_t p = 0; p < o.pages; ++p) step(pass, a, s, p);
            }
        }
    };
    const auto first_start = Clock::now();
    perform(0, o.passes / 2);
    const uint64_t before_ns = elapsed(first_start);
    const auto close_start = Clock::now();
    for (uint64_t a = 0; a < o.arenas; a += 2) arenas[static_cast<size_t>(a)]->begin_close_with_leases();
    for (uint64_t a = 1; a < o.arenas; a += 2) {
        mgmem_snapshot_v1 snap{};
        require(mgmem_domain_snapshot_v1(arenas[static_cast<size_t>(a)]->domain, &snap, sizeof(snap)), "open_snapshot");
        if (snap.state != MGMEM_OPEN || snap.active_allocations != o.slots || snap.active_leases != o.slots ||
            snap.live_bytes != bytes_per_domain || snap.retiring_bytes != 0 || snap.retained_bytes != 0 ||
            snap.backing_bytes != bytes_per_domain || snap.reserved_bytes != 0)
            throw std::runtime_error("other_domain_not_open");
    }
    const uint64_t close_ns = elapsed(close_start);
    const auto second_start = Clock::now();
    perform(o.passes / 2, o.passes);
    const uint64_t after_ns = elapsed(second_start);
    const uint64_t expected_per_cell_delta = mul(mul(o.passes, add(o.passes, 1)), add(o.passes, 2)) / 6;
    const uint64_t expected = add(mul(initial_sum, o.passes), mul(touched_pages, expected_per_cell_delta));
    if (checksum != expected) throw std::runtime_error("access_checksum_mismatch");
    const uint64_t final_delta = mul(o.passes, add(o.passes, 1)) / 2;
    uint64_t final_checksum = 0;
    for (uint64_t a = 0; a < o.arenas; ++a) for (uint64_t s = 0; s < o.slots; ++s) for (uint64_t p = 0; p < o.pages; ++p)
        final_checksum = add(final_checksum, *cell(*arenas[static_cast<size_t>(a)], o, page_size, a, s, p));
    const uint64_t final_expected = add(initial_sum, mul(touched_pages, final_delta));
    if (final_checksum != final_expected) throw std::runtime_error("final_checksum_mismatch");
    const auto release_start = Clock::now();
    for (uint64_t a = 1; a < o.arenas; a += 2) arenas[static_cast<size_t>(a)]->begin_close_with_leases();
    for (auto &arena : arenas) arena->finish();
    const uint64_t release_ns = elapsed(release_start);
    const uint64_t total_ns = elapsed(total_start);
    if (total_ns > 30000000000ull) throw std::runtime_error("session_timeout");
    std::cout << "{\"schema_version\":1,\"protocol_id\":\"MM-021-multi-arena-functional-v1\","
              << "\"evidence_type\":\"HOST_DIAGNOSTIC\",\"success\":true,\"failure_reason\":null"
              << ",\"provider\":\"System\",\"domain_mode\":\"ARENA\",\"placement\":\"NOT_SELECTED\",\"page_hint\":\"NOT_SELECTED\""
              << ",\"order\":\"" << (o.interleaved ? "interleaved" : "grouped") << "\",\"offset_policy\":\"" << (o.rotate ? "rotate" : "fixed")
              << "\",\"actual_base_page_bytes\":" << page_size << ",\"cache_line_bytes_assumed\":" << line_bytes_assumed
              << ",\"cache_geometry_verified\":false,\"arena_count\":" << o.arenas << ",\"slots_per_arena\":" << o.slots
              << ",\"pages_per_slot\":" << o.pages << ",\"passes\":" << o.passes << ",\"touched_pages\":" << touched_pages
              << ",\"touched_payload_bytes\":" << mul(touched_pages, sizeof(uint64_t))
              << ",\"access_count\":" << accesses << ",\"warm_access_count\":" << touched_pages
              << ",\"warm_checksum\":" << warm_checksum << ",\"bytes_per_domain_budget\":" << bytes_per_domain
              << ",\"unique_backing_bytes\":" << total_bytes << ",\"initial_live_bytes\":" << total_bytes
              << ",\"initial_retiring_bytes\":0,\"initial_retained_bytes\":0,\"peak_backing_bytes\":" << total_bytes
              << ",\"mid_live_bytes\":" << mul(o.arenas / 2, bytes_per_domain)
              << ",\"mid_retiring_bytes\":" << mul((o.arenas + 1) / 2, bytes_per_domain)
              << ",\"mid_retained_bytes\":0,\"initial_sum\":" << initial_sum
              << ",\"checksum\":" << checksum << ",\"expected_checksum\":" << expected
              << ",\"final_checksum\":" << final_checksum << ",\"final_expected_checksum\":" << final_expected
              << ",\"closed_during_access\":" << ((o.arenas + 1) / 2) << ",\"budget_rejections\":" << o.arenas
              << ",\"close_blocked_by_live_leases\":" << o.arenas << ",\"final_domains_closed_zero\":" << o.arenas
              << ",\"phase_ns\":{\"allocation_and_acquire\":" << allocation_ns << ",\"initialization_and_first_touch\":" << initialization_ns
              << ",\"warm_access\":" << warm_ns << ",\"pre_close_access\":" << before_ns << ",\"begin_close_subset\":" << close_ns
              << ",\"post_close_access\":" << after_ns << ",\"final_release\":" << release_ns << "},\"phases_overlap\":false"
              << ",\"total_ns\":" << total_ns << ",\"physical_contiguity\":\"UNKNOWN\",\"tlb_geometry\":\"UNKNOWN\",\"pmu\":\"UNAVAILABLE\""
              << ",\"pfn_visibility\":\"UNAVAILABLE\",\"performance_qualification\":\"NOT_STARTED\",\"mechanism_evidence\":\"INCONCLUSIVE\"}\n";
}
} // namespace

int main(int argc, char **argv) {
    try {
        Options o;
        for (int i = 1; i < argc; ++i) {
            const std::string argument = argv[i];
            if (argument == "--help") {
                std::cout << "mgmem_tlb_multi_arena [--arenas N] [--slots N] [--pages N] [--passes N] [--order grouped|interleaved] [--offset fixed|rotate]\n";
                return 0;
            }
            if (++i >= argc) throw std::runtime_error("missing_option_value");
            const std::string value = argv[i];
            if (argument == "--arenas") o.arenas = parse_number(argv[i]);
            else if (argument == "--slots") o.slots = parse_number(argv[i]);
            else if (argument == "--pages") o.pages = parse_number(argv[i]);
            else if (argument == "--passes") o.passes = parse_number(argv[i]);
            else if (argument == "--order" && (value == "grouped" || value == "interleaved")) o.interleaved = value == "interleaved";
            else if (argument == "--offset" && (value == "fixed" || value == "rotate")) o.rotate = value == "rotate";
            else throw std::runtime_error("unknown_option_or_value");
        }
        run(o);
        return 0;
    } catch (const std::exception &error) {
        // Exception messages contain fixed identifiers and decimal status codes only.
        std::cout << "{\"schema_version\":1,\"evidence_type\":\"HOST_DIAGNOSTIC\",\"success\":false,\"failure_reason\":\""
                  << error.what() << "\",\"performance_qualification\":\"NOT_STARTED\",\"mechanism_evidence\":\"INCONCLUSIVE\"}\n";
        return 1;
    }
}
