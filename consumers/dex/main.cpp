#include <mgmem/mgmem.h>
#include <dexmeguviz/dexmeguviz.h>

#include <atomic>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>

namespace {

void require(bool condition, const char* operation) {
    if (!condition) throw std::runtime_error(operation);
}

void memory_check(mgmem_status status, const char* operation) {
    if (status != MGMEM_OK)
        throw std::runtime_error(std::string(operation) + ": mgmem status " + std::to_string(status));
}

void dex_check(dmg_status status, const char* operation) {
    if (status != DMG_OK)
        throw std::runtime_error(std::string(operation) + ": Dex status " + std::to_string(status));
}

using Engine = std::unique_ptr<dmg_engine, decltype(&dmg_engine_release)>;
using Snapshot = std::unique_ptr<dmg_snapshot, decltype(&dmg_snapshot_release)>;
using Result = std::unique_ptr<dmg_result, decltype(&dmg_result_release)>;

struct Domain {
    mgmem_domain* value = nullptr;
    mgmem_handle_v1 allocation{};
    mgmem_borrow_v1 writer{};
    bool owns_allocation = false;
    bool owns_writer = false;

    ~Domain() {
        if (!value) return;
        if (owns_writer) (void)mgmem_borrow_release_v1(value, writer.lease);
        if (owns_allocation) (void)mgmem_release_v1(value, allocation);
        (void)mgmem_domain_begin_close_v1(value);
        (void)mgmem_domain_collect_v1(value);
        if (mgmem_domain_drain_v1(value, 0) == MGMEM_OK)
            (void)mgmem_domain_destroy_v1(value);
        else
            std::cerr << "Memory domain remains alive because it did not drain\n";
    }
};

mgmem_snapshot_v1 memory_stats(mgmem_domain* domain) {
    mgmem_snapshot_v1 stats{};
    memory_check(mgmem_domain_snapshot_v1(domain, &stats, sizeof(stats)), "Memory snapshot");
    return stats;
}

// One Memory read lease keeps the single backing alive for all parser references.
// The parser's borrowed_bytes is an observation, not another owned backing.
struct InputLease {
    mgmem_domain* domain;
    mgmem_borrow_v1 borrow{};
    std::atomic<uint32_t> references{1};
    std::atomic<uint32_t> retains{0};
    std::atomic<uint32_t> releases{0};
    std::atomic<mgmem_status> final_status{MGMEM_BUSY};
    bool caller_reference = true;

    InputLease(mgmem_domain* owner, mgmem_handle_v1 allocation, uint64_t bytes) : domain(owner) {
        memory_check(mgmem_borrow_acquire_v1(owner, allocation, 0, bytes, MGMEM_READ, &borrow),
                     "Acquire immutable input lease");
    }

    ~InputLease() {
        if (caller_reference) release(this);
    }

    static void retain(void* context) noexcept {
        auto& lease = *static_cast<InputLease*>(context);
        lease.references.fetch_add(1, std::memory_order_relaxed);
        lease.retains.fetch_add(1, std::memory_order_relaxed);
    }

    static void release(void* context) noexcept {
        auto& lease = *static_cast<InputLease*>(context);
        lease.releases.fetch_add(1, std::memory_order_relaxed);
        if (lease.references.fetch_sub(1, std::memory_order_acq_rel) == 1)
            lease.final_status.store(mgmem_borrow_release_v1(lease.domain, lease.borrow.lease),
                                     std::memory_order_release);
    }

    void release_caller() noexcept {
        caller_reference = false;
        release(this);
    }

    dmg_buffer_lease options() noexcept {
        return {sizeof(dmg_buffer_lease), DMG_ABI_VERSION, this, &retain, &release, 1, 0};
    }
};

Result query_all(dmg_snapshot* snapshot) {
    dmg_method_query query{};
    query.struct_size = sizeof(query);
    query.abi_version = DMG_ABI_VERSION;
    query.intent = DMG_ENUMERATE;
    query.max_results = 65536;
    dmg_result* raw = nullptr;
    dmg_error error{};
    dex_check(dmg_query_methods(snapshot, &query, &raw, &error), "Enumerate real parser methods");
    return Result(raw, &dmg_result_release);
}

uint64_t mix(uint64_t hash, uint64_t word) {
    for (unsigned i = 0; i != 8; ++i) {
        hash = (hash ^ (word & 255u)) * UINT64_C(1099511628211);
        word >>= 8;
    }
    return hash;
}

// Consume every field of every method row, including the parser's borrowed text.
uint64_t consume(dmg_result* result, uint32_t expected_methods) {
    dmg_result_info outcome{};
    dex_check(dmg_result_get_info(result, &outcome), "Read query outcome");
    require(outcome.coverage == DMG_COVERAGE_COMPLETE && outcome.execution_status == DMG_OK &&
            outcome.candidates_count == expected_methods, "Query did not completely enumerate definitions");
    uint64_t hash = UINT64_C(14695981039346656037);
    for (uint32_t index = 0; index < outcome.candidates_count; ++index) {
        dmg_method_row row{};
        dex_check(dmg_result_get_method(result, index, &row), "Read method row");
        hash = mix(hash, row.method_index);
        hash = mix(hash, row.access_flags);
        hash = mix(hash, row.declaration_offset);
        hash = mix(hash, row.code_offset);
        const uint16_t* texts[] = {row.owner, row.name, row.prototype};
        const uint64_t lengths[] = {row.owner_length, row.name_length, row.prototype_length};
        for (unsigned part = 0; part != 3; ++part) {
            require(texts[part] != nullptr || lengths[part] == 0, "Invalid parser text view");
            hash = mix(hash, lengths[part]);
            for (uint64_t offset = 0; offset < lengths[part]; ++offset)
                hash = mix(hash, texts[part][offset]);
        }
    }
    return hash;
}

uint64_t input_checksum(const mgmem_borrow_v1& borrow) {
    const auto* bytes = static_cast<const uint8_t*>(borrow.data);
    uint64_t hash = UINT64_C(14695981039346656037);
    for (uint64_t index = 0; index < borrow.length; ++index)
        hash = (hash ^ bytes[index]) * UINT64_C(1099511628211);
    return hash;
}

} // namespace

int main(int argc, char** argv) {
    try {
        require(argc == 2 || (argc == 3 && std::string(argv[2]) == "--arena"),
                "Usage: mgmem_dex_consumer <real.dex> [--arena]");
        const uint32_t mode = argc == 3 ? MGMEM_ARENA : MGMEM_SYSTEM;
        require(mgmem_abi_version_v1() == (MGMEM_ABI_MAJOR << 16 | MGMEM_ABI_MINOR), "Memory ABI mismatch");
        require(dmg_abi_version() == DMG_ABI_VERSION, "Dex ABI mismatch");

        std::ifstream input(argv[1], std::ios::binary | std::ios::ate);
        require(input.good(), "Open DEX input");
        const auto end = input.tellg();
        require(end > 0 && static_cast<uint64_t>(end) <= DMG_MAX_SOURCE_BYTES,
                "DEX input must be nonempty and at most the parser's source limit");
        const uint64_t length = static_cast<uint64_t>(end);
        input.seekg(0);
        require(input.good(), "Seek DEX input");

        Domain memory;
        const mgmem_domain_options_v1 domain_options{sizeof(mgmem_domain_options_v1), MGMEM_ABI_MAJOR,
            MGMEM_ABI_MINOR, 0, mode, 0, 32u * 1024u * 1024u, 64u * 1024u, nullptr};
        memory_check(mgmem_domain_create_v1(&domain_options, &memory.value), "Create Memory domain");
        memory_check(mgmem_allocate_v1(memory.value, length, 64, &memory.allocation), "Allocate DEX backing");
        memory.owns_allocation = true;
        memory_check(mgmem_borrow_acquire_v1(memory.value, memory.allocation, 0, length, MGMEM_WRITE,
                                            &memory.writer), "Acquire input writer");
        memory.owns_writer = true;
        input.read(static_cast<char*>(memory.writer.data), static_cast<std::streamsize>(length));
        require(input.good() && static_cast<uint64_t>(input.gcount()) == length, "Read complete DEX input");
        memory_check(mgmem_borrow_release_v1(memory.value, memory.writer.lease), "Release input writer");
        memory.owns_writer = false;

        InputLease lease(memory.value, memory.allocation, length);
        const uint64_t original_checksum = input_checksum(lease.borrow);
        const dmg_engine_options engine_options{sizeof(dmg_engine_options), DMG_ABI_VERSION,
            64u * 1024u * 1024u, DMG_MAX_SOURCE_BYTES, DMG_MAX_SOURCE_BYTES, 1, 0};
        dmg_engine* raw_engine = nullptr;
        dmg_error error{};
        dex_check(dmg_engine_create(&engine_options, &raw_engine, &error), "Create real parser engine");
        Engine engine(raw_engine, &dmg_engine_release);
        const dmg_open_options options{sizeof(dmg_open_options), DMG_ABI_VERSION, DMG_MAX_SOURCE_BYTES,
            "meguviz-memory-consumer", "input", nullptr};
        const auto parser_lease = lease.options();
        dmg_snapshot* raw_snapshot = nullptr;
        dex_check(dmg_snapshot_open_borrowed(engine.get(), static_cast<const uint8_t*>(lease.borrow.data),
            length, &parser_lease, &options, &raw_snapshot, &error), "Open parser borrowed snapshot");
        Snapshot snapshot(raw_snapshot, &dmg_snapshot_release);
        require(lease.references.load() == 2 && lease.retains.load() == 1,
                "Parser did not retain the input before accepting it");
        dmg_snapshot_info source{};
        dex_check(dmg_snapshot_get_info(snapshot.get(), &source), "Read source identity");
        const auto method_count = source.method_def_count;
        require(method_count > 0 && method_count <= 65536, "Input needs a bounded, nonempty method workload");
        require(source.source_sha256 && std::string(source.source_sha256).size() == 64, "Missing parser input hash");
        const std::string source_sha256(source.source_sha256);

        // A separate real parser copy-in path is the functional oracle; this is
        // deliberately not a performance comparison of these storage contracts.
        dmg_snapshot* raw_copy = nullptr;
        dex_check(dmg_snapshot_open_copy(static_cast<const uint8_t*>(lease.borrow.data), length,
                                        &options, &raw_copy, &error), "Open independent copy-in oracle");
        Snapshot copied(raw_copy, &dmg_snapshot_release);
        auto copy_result = query_all(copied.get());
        const uint64_t oracle_checksum = consume(copy_result.get(), method_count);
        copy_result.reset();
        copied.reset();

        lease.release_caller();
        memory_check(mgmem_release_v1(memory.value, memory.allocation), "Release original input owner");
        memory.owns_allocation = false;
        memory_check(mgmem_domain_begin_close_v1(memory.value), "Begin nonblocking close");
        require(mgmem_domain_drain_v1(memory.value, 0) == MGMEM_TIMEOUT,
                "Drain must not free an input still used by the parser");
        auto during = memory_stats(memory.value);
        require(during.state == MGMEM_CLOSING && during.live_bytes == 0 &&
                during.retiring_bytes == length && during.active_leases == 1,
                "Released owner was not kept retiring by its single backing lease");

        auto result = query_all(snapshot.get());
        snapshot.reset();
        require(lease.references.load() == 1, "Result did not preserve the source after snapshot release");
        require(consume(result.get(), method_count) == oracle_checksum,
                "Borrowed query rows differ from the copy-in oracle");
        require(input_checksum(lease.borrow) == original_checksum,
                "Immutable input changed while its owner was closing");
        dmg_engine_stats parser_stats{};
        dex_check(dmg_engine_get_stats(engine.get(), &parser_stats), "Read parser accounting");
        require(parser_stats.borrowed_bytes == length, "Parser borrowed bytes were not accounted once");

        result.reset();
        require(lease.references.load() == 0 && lease.releases.load() == 2 &&
                lease.final_status.load(std::memory_order_acquire) == MGMEM_OK,
                "Final result did not release its Memory lease exactly once");
        dex_check(dmg_engine_get_stats(engine.get(), &parser_stats), "Read drained parser accounting");
        require(parser_stats.borrowed_bytes == 0, "Parser retained borrowed bytes after the final result");
        engine.reset();
        memory_check(mgmem_domain_collect_v1(memory.value), "Collect on cleanup caller");
        memory_check(mgmem_domain_drain_v1(memory.value, 0), "Drain completed Memory domain");
        const auto finished = memory_stats(memory.value);
        require(finished.state == MGMEM_CLOSED && finished.backing_bytes == 0 &&
                finished.retiring_bytes == 0 && finished.active_leases == 0 && finished.active_allocations == 0,
                "Memory resources were not fully reclaimed");
        memory_check(mgmem_domain_destroy_v1(memory.value), "Destroy closed domain");
        memory.value = nullptr;
        std::cout << "{\"consumer\":\"DexMeguviz\",\"status\":\"PASS\",\"mode\":\""
                  << (mode == MGMEM_ARENA ? "arena" : "system") << "\",\"input_bytes\":" << length
                  << ",\"method_count\":" << method_count << ",\"source_sha256\":\"" << source_sha256
                  << "\",\"copied_oracle_equal\":true,\"result_checksum\":" << oracle_checksum
                  << ",\"peak_backing_bytes\":" << finished.peak_backing_bytes
                  << ",\"closed_backing_bytes\":0,\"runtime_scope\":\"host_functional\"}\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "Dex consumer failed: " << error.what() << '\n';
        return 1;
    }
}
