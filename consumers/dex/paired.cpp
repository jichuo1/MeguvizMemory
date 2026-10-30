#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <mgmem/mgmem.h>
#include <dexmeguviz/dexmeguviz.h>

#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <fstream>
#include <iostream>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

#if defined(_WIN32)
#include <malloc.h>
#elif defined(__linux__)
#include <sched.h>
#include <sys/resource.h>
#endif

namespace {
using Clock = std::chrono::steady_clock;
constexpr uint64_t input_budget = 32ull * 1024 * 1024;
constexpr uint64_t parser_budget = 256ull * 1024 * 1024;
constexpr uint64_t alignment = 64;

void require(bool value, const char *message) {
    if (!value) throw std::runtime_error(message);
}
void memory_check(mgmem_status status, const char *operation) {
    if (status != MGMEM_OK) throw std::runtime_error(std::string(operation) + "_mgmem_" + std::to_string(status));
}
void dex_check(dmg_status status, const char *operation) {
    if (status != DMG_OK) throw std::runtime_error(std::string(operation) + "_dex_" + std::to_string(status));
}
uint64_t elapsed(Clock::time_point start, Clock::time_point finish = Clock::now()) {
    return static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(finish - start).count());
}
std::string escaped(const std::string &value) {
    std::string result;
    for (unsigned char c : value) {
        if (c == '"' || c == '\\') { result += '\\'; result += static_cast<char>(c); }
        else if (c == '\n') result += "\\n";
        else if (c == '\r') result += "\\r";
        else if (c == '\t') result += "\\t";
        else if (c >= 32 && c < 127) result += static_cast<char>(c);
        else result += '?';
    }
    return result;
}

struct Usage {
    bool available = false;
    uint64_t minor_faults = 0, major_faults = 0, user_ns = 0, system_ns = 0;
};
Usage usage() {
    Usage value;
#if defined(__linux__)
    rusage raw{};
    if (getrusage(RUSAGE_SELF, &raw) == 0) {
        value.available = true;
        value.minor_faults = static_cast<uint64_t>(raw.ru_minflt);
        value.major_faults = static_cast<uint64_t>(raw.ru_majflt);
        value.user_ns = static_cast<uint64_t>(raw.ru_utime.tv_sec) * 1000000000ull + static_cast<uint64_t>(raw.ru_utime.tv_usec) * 1000;
        value.system_ns = static_cast<uint64_t>(raw.ru_stime.tv_sec) * 1000000000ull + static_cast<uint64_t>(raw.ru_stime.tv_usec) * 1000;
    }
#endif
    return value;
}
int current_cpu() {
#if defined(__linux__)
    return sched_getcpu();
#else
    return -1;
#endif
}
void select_cpu(int cpu) {
    if (cpu < 0) return;
#if defined(__linux__)
    require(cpu < CPU_SETSIZE, "affinity_cpu_out_of_range");
    cpu_set_t selected;
    CPU_ZERO(&selected);
    CPU_SET(cpu, &selected);
    require(sched_setaffinity(0, sizeof(selected), &selected) == 0, "affinity_set_unavailable");
    cpu_set_t actual;
    CPU_ZERO(&actual);
    require(sched_getaffinity(0, sizeof(actual), &actual) == 0, "affinity_query_unavailable");
    require(CPU_ISSET(cpu, &actual) && CPU_COUNT(&actual) == 1, "affinity_not_exactly_selected_cpu");
#else
    throw std::runtime_error("affinity_unsupported_on_this_platform");
#endif
}
int parse_cpu(const char *text) {
    require(text && *text, "missing_cpu_number");
    uint64_t result = 0;
    for (const char *p = text; *p; ++p) {
        require(*p >= '0' && *p <= '9' && result <= 100000, "invalid_cpu_number");
        result = result * 10 + static_cast<unsigned>(*p - '0');
    }
    require(result <= static_cast<uint64_t>(std::numeric_limits<int>::max()), "cpu_number_overflow");
    return static_cast<int>(result);
}

using Engine = std::unique_ptr<dmg_engine, decltype(&dmg_engine_release)>;
using Snapshot = std::unique_ptr<dmg_snapshot, decltype(&dmg_snapshot_release)>;
using Result = std::unique_ptr<dmg_result, decltype(&dmg_result_release)>;

// The stack context outlives Engine/Snapshot/Result (declared after it). The SDK
// sees the same immutable reference-count contract for both backing choices.
struct InputOwner {
    bool arena;
    uint64_t bytes;
    void *address = nullptr;
    mgmem_domain *domain = nullptr;
    mgmem_handle_v1 allocation{};
    mgmem_borrow_v1 writer{}, reader{};
    bool allocation_live = false, writer_live = false, reader_valid = false;
    bool caller_live = true;
    std::atomic<uint32_t> references{1}, retains{0}, releases{0};
    std::atomic<mgmem_status> final_status{MGMEM_BUSY};
    std::atomic<bool> native_freed{false};
    uint64_t peak = 0;

    InputOwner(bool use_arena, uint64_t size) : arena(use_arena), bytes(size) {}
    InputOwner(const InputOwner &) = delete;
    InputOwner &operator=(const InputOwner &) = delete;

    void prepare(const std::vector<uint8_t> &source) {
        require(bytes > 0 && bytes <= input_budget && source.size() == bytes, "input_budget_rejection");
        if (!arena) {
#if defined(_WIN32)
            address = _aligned_malloc(static_cast<size_t>(bytes), alignment);
#else
            if (posix_memalign(&address, alignment, static_cast<size_t>(bytes)) != 0) address = nullptr;
#endif
            require(address != nullptr, "system_allocation_failed");
            std::memcpy(address, source.data(), static_cast<size_t>(bytes));
            peak = bytes;
            return;
        }
        const mgmem_domain_options_v1 options{sizeof(options), MGMEM_ABI_MAJOR, MGMEM_ABI_MINOR,
            0, MGMEM_ARENA, 0, input_budget, bytes, nullptr};
        memory_check(mgmem_domain_create_v1(&options, &domain), "create");
        memory_check(mgmem_allocate_v1(domain, bytes, alignment, &allocation), "allocate");
        allocation_live = true;
        memory_check(mgmem_borrow_acquire_v1(domain, allocation, 0, bytes, MGMEM_WRITE, &writer), "writer");
        writer_live = true;
        std::memcpy(writer.data, source.data(), static_cast<size_t>(bytes));
        memory_check(mgmem_borrow_release_v1(domain, writer.lease), "release_writer");
        writer_live = false;
        memory_check(mgmem_borrow_acquire_v1(domain, allocation, 0, bytes, MGMEM_READ, &reader), "reader");
        reader_valid = true;
        address = reader.data;
    }
    static void retain(void *context) noexcept {
        auto &self = *static_cast<InputOwner *>(context);
        self.references.fetch_add(1, std::memory_order_relaxed);
        self.retains.fetch_add(1, std::memory_order_relaxed);
    }
    static void release(void *context) noexcept {
        auto &self = *static_cast<InputOwner *>(context);
        self.releases.fetch_add(1, std::memory_order_relaxed);
        if (self.references.fetch_sub(1, std::memory_order_acq_rel) != 1) return;
        mgmem_status status = MGMEM_OK;
        if (self.arena) {
            if (self.reader_valid) status = mgmem_borrow_release_v1(self.domain, self.reader.lease);
        } else {
#if defined(_WIN32)
            _aligned_free(self.address);
#else
            std::free(self.address);
#endif
            self.native_freed.store(true, std::memory_order_relaxed);
        }
        self.final_status.store(status, std::memory_order_release);
    }
    dmg_buffer_lease sdk_lease() noexcept {
        return {sizeof(dmg_buffer_lease), DMG_ABI_VERSION, this, &retain, &release, 1, 0};
    }
    void release_caller() noexcept {
        if (caller_live) { caller_live = false; release(this); }
    }
    void retire_owner() {
        release_caller();
        if (arena) {
            memory_check(mgmem_release_v1(domain, allocation), "retire_owner");
            allocation_live = false;
            memory_check(mgmem_domain_begin_close_v1(domain), "begin_close");
        }
    }
    bool lease_finished() const noexcept {
        return references.load(std::memory_order_acquire) == 0 && retains.load() == 1 && releases.load() == 2 &&
               final_status.load(std::memory_order_acquire) == MGMEM_OK;
    }
    void finish() {
        require(lease_finished(), "final_input_reference_mismatch");
        if (!arena) {
            require(native_freed.load(), "system_backing_not_freed");
            address = nullptr;
            return;
        }
        reader_valid = false;
        memory_check(mgmem_domain_collect_v1(domain), "collect");
        memory_check(mgmem_domain_drain_v1(domain, 0), "drain");
        mgmem_snapshot_v1 state{};
        memory_check(mgmem_domain_snapshot_v1(domain, &state, sizeof(state)), "final_snapshot");
        require(state.state == MGMEM_CLOSED && state.backing_bytes == 0 && state.active_allocations == 0 &&
                state.active_leases == 0 && state.retiring_bytes == 0, "Memory_final_resources_not_zero");
        peak = state.peak_backing_bytes;
        memory_check(mgmem_domain_destroy_v1(domain), "destroy");
        domain = nullptr;
        address = nullptr;
    }
    ~InputOwner() noexcept {
        // SDK wrappers have already released their references on every exception
        // path. A remaining SDK reference would violate its synchronous contract;
        // do not let a callback later access a dead stack context.
        release_caller();
        if (references.load(std::memory_order_acquire) != 0) std::terminate();
        if (!arena || !domain) return;
        if (writer_live && mgmem_borrow_release_v1(domain, writer.lease) != MGMEM_OK) std::terminate();
        if (reader_valid && final_status.load(std::memory_order_acquire) != MGMEM_OK) std::terminate();
        if (allocation_live && mgmem_release_v1(domain, allocation) != MGMEM_OK) std::terminate();
        if (mgmem_domain_begin_close_v1(domain) != MGMEM_OK || mgmem_domain_collect_v1(domain) != MGMEM_OK ||
            mgmem_domain_drain_v1(domain, 0) != MGMEM_OK || mgmem_domain_destroy_v1(domain) != MGMEM_OK)
            std::terminate();
    }
};

uint64_t mix(uint64_t hash, uint64_t word) {
    for (unsigned i = 0; i < 8; ++i) { hash = (hash ^ (word & 255u)) * UINT64_C(1099511628211); word >>= 8; }
    return hash;
}
uint64_t consume(dmg_result *result, uint32_t expected) {
    dmg_result_info outcome{};
    dex_check(dmg_result_get_info(result, &outcome), "query_outcome");
    require(outcome.coverage == DMG_COVERAGE_COMPLETE && outcome.execution_status == DMG_OK &&
            outcome.candidates_count == expected, "query_did_not_fully_cover_method_definitions");
    uint64_t hash = UINT64_C(14695981039346656037);
    for (uint32_t index = 0; index < expected; ++index) {
        dmg_method_row row{};
        dex_check(dmg_result_get_method(result, index, &row), "query_row");
        hash = mix(hash, row.method_index); hash = mix(hash, row.access_flags);
        hash = mix(hash, row.declaration_offset); hash = mix(hash, row.code_offset);
        const uint16_t *texts[] = {row.owner, row.name, row.prototype};
        const uint64_t lengths[] = {row.owner_length, row.name_length, row.prototype_length};
        for (unsigned part = 0; part < 3; ++part) {
            require(texts[part] || lengths[part] == 0, "invalid_method_text_view");
            hash = mix(hash, lengths[part]);
            for (uint64_t i = 0; i < lengths[part]; ++i) hash = mix(hash, texts[part][i]);
        }
    }
    return hash;
}

struct Metrics {
    std::string variant = "UNKNOWN", source_sha256, failure;
    bool success = false, lease_final_correct = false, diagnostic = false;
    int selected_cpu = -1, cpu_before = -1, cpu_after = -1;
    uint64_t bytes = 0, methods = 0, checksum = 0, preload_ns = 0;
    uint64_t total_ns = 0, setup_ns = 0, query_ns = 0, cleanup_ns = 0, peak = 0;
    Usage before, after;
};
void print(const Metrics &m) {
    const bool resource_available = m.before.available && m.after.available;
    std::cout << "{\"schema_version\":1,\"workload\":\"dex_borrowed_parse_and_complete_method_query_v1\",\"success\":"
              << (m.success ? "true" : "false") << ",\"variant\":\"" << m.variant << "\",\"failure_reason\":";
    if (m.failure.empty()) std::cout << "null"; else std::cout << '"' << escaped(m.failure) << '"';
    std::cout << ",\"source_sha256\":";
    if (m.source_sha256.empty()) std::cout << "null"; else std::cout << '"' << m.source_sha256 << '"';
    std::cout << ",\"input_bytes\":" << m.bytes << ",\"method_count\":" << m.methods << ",\"result_checksum\":" << m.checksum
              << ",\"preload_ns\":" << m.preload_ns << ",\"total_ns\":" << m.total_ns;
    const auto phase = [&](const char *name, uint64_t ns) {
        std::cout << ",\"" << name << "\":";
        if (m.diagnostic) std::cout << ns; else std::cout << "null";
    };
    phase("setup_ns", m.setup_ns); phase("query_ns", m.query_ns); phase("cleanup_ns", m.cleanup_ns);
    std::cout << ",\"diagnostic\":" << (m.diagnostic ? "true" : "false") << ",\"phases_overlap\":false"
              << ",\"a3_observation_API_cost_included\":true"
              << ",\"memory_allocator_and_lease_used\":" << (m.variant == "A3" ? "true" : "false")
              << ",\"backing_peak_bytes\":" << m.peak << ",\"input_budget_bytes\":" << input_budget
              << ",\"parser_budget_bytes\":" << parser_budget << ",\"closed_backing_bytes\":";
    if (m.success) std::cout << 0; else std::cout << "null";
    std::cout << ",\"lease_final_correct\":" << (m.lease_final_correct ? "true" : "false")
              << ",\"affinity_selected\":" << (m.selected_cpu >= 0 ? "true" : "false") << ",\"selected_cpu\":";
    if (m.selected_cpu < 0) std::cout << "null"; else std::cout << m.selected_cpu;
    std::cout << ",\"cpu_before\":";
    if (m.cpu_before < 0) std::cout << "null"; else std::cout << m.cpu_before;
    std::cout << ",\"cpu_after\":";
    if (m.cpu_after < 0) std::cout << "null"; else std::cout << m.cpu_after;
    std::cout << ",\"getrusage_status\":\"" << (resource_available ? "AVAILABLE" : "UNAVAILABLE") << '"';
    const auto delta = [&](const char *name, uint64_t before, uint64_t after) {
        std::cout << ",\"" << name << "\":";
        if (resource_available && after >= before) std::cout << after - before; else std::cout << "null";
    };
    delta("minor_faults", m.before.minor_faults, m.after.minor_faults);
    delta("major_faults", m.before.major_faults, m.after.major_faults);
    delta("user_cpu_ns", m.before.user_ns, m.after.user_ns);
    delta("system_cpu_ns", m.before.system_ns, m.after.system_ns);
    std::cout << ",\"preloaded_external_bytes\":" << m.bytes
              << ",\"storage_scope\":\"single_immutable_input_backing_excludes_parser_and_preload_metadata\""
              << ",\"timing_scope\":\"CPU_task_includes_setup_parse_query_consume_and_final_cleanup_excludes_file_preload_and_process_start\""
              << ",\"product_scope\":\"independent_DexMeguviz_task_not_BIL_DexKit_or_playback\"}\n";
}
} // namespace

int main(int argc, char **argv) {
    Metrics m;
    Clock::time_point started{};
    bool task_started = false;
    try {
        require(argc >= 4 && argc <= 7, "usage_paired_file_dex_--variant_A1_or_A3_optional_--cpu_N_--diagnostic");
        bool variant_set = false, cpu_set = false;
        for (int i = 2; i < argc; ++i) {
            const std::string option = argv[i];
            if (option == "--diagnostic") {
                require(!m.diagnostic, "duplicate_diagnostic_option");
                m.diagnostic = true;
            } else if (option == "--variant") {
                require(!variant_set && ++i < argc, "missing_or_duplicate_variant");
                const std::string variant = argv[i];
                require(variant == "A1" || variant == "A3", "variant_must_be_A1_or_A3");
                m.variant = variant;
                variant_set = true;
            } else if (option == "--cpu") {
                require(!cpu_set && ++i < argc, "missing_or_duplicate_cpu");
                m.selected_cpu = parse_cpu(argv[i]);
                cpu_set = true;
            } else throw std::runtime_error("unknown_option");
        }
        require(variant_set, "missing_variant_option");
        select_cpu(m.selected_cpu);
        require(dmg_abi_version() == DMG_ABI_VERSION, "parser_ABI_mismatch");
        const auto preload = Clock::now();
        std::ifstream file(argv[1], std::ios::binary | std::ios::ate);
        require(file.good(), "open_input_failed");
        const auto end = file.tellg();
        require(end > 0 && static_cast<uint64_t>(end) <= input_budget && static_cast<uint64_t>(end) <= DMG_MAX_SOURCE_BYTES,
                "input_size_outside_fixed_budget");
        m.bytes = static_cast<uint64_t>(end);
        std::vector<uint8_t> source(static_cast<size_t>(m.bytes));
        file.seekg(0);
        file.read(reinterpret_cast<char *>(source.data()), static_cast<std::streamsize>(m.bytes));
        require(file.good() && static_cast<uint64_t>(file.gcount()) == m.bytes, "read_input_incomplete");
        m.preload_ns = elapsed(preload);
        m.before = usage();
        m.cpu_before = current_cpu();
        started = Clock::now();
        task_started = true;
        Clock::time_point query_finished{};
        {
            InputOwner input(m.variant == "A3", m.bytes);
            input.prepare(source);
            const dmg_engine_options engine_options{sizeof(engine_options), DMG_ABI_VERSION,
                parser_budget, input_budget, input_budget, 1, 0};
            dmg_engine *raw_engine = nullptr;
            dmg_error error{};
            dex_check(dmg_engine_create(&engine_options, &raw_engine, &error), "engine_create");
            Engine engine(raw_engine, &dmg_engine_release);
            const dmg_open_options options{sizeof(options), DMG_ABI_VERSION, input_budget,
                "meguviz-memory-paired-v1", "fixed-input", nullptr};
            auto lease = input.sdk_lease();
            dmg_snapshot *raw_snapshot = nullptr;
            dex_check(dmg_snapshot_open_borrowed(engine.get(), static_cast<const uint8_t *>(input.address),
                m.bytes, &lease, &options, &raw_snapshot, &error), "snapshot_open_borrowed");
            Snapshot snapshot(raw_snapshot, &dmg_snapshot_release);
            require(input.references.load() == 2 && input.retains.load() == 1, "source_lease_not_retained_before_acceptance");
            dmg_snapshot_info info{};
            dex_check(dmg_snapshot_get_info(snapshot.get(), &info), "snapshot_info");
            require(info.method_def_count > 0 && info.method_def_count <= 65536, "method_workload_out_of_bounds");
            require(info.source_sha256 && std::strlen(info.source_sha256) == 64, "source_digest_unavailable");
            m.source_sha256 = info.source_sha256;
            for (char c : m.source_sha256) require((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'), "invalid_source_digest");
            m.methods = info.method_def_count;
            input.retire_owner();
            Clock::time_point query_started{};
            if (m.diagnostic) { query_started = Clock::now(); m.setup_ns = elapsed(started, query_started); }
            dmg_method_query query{};
            query.struct_size = sizeof(query);
            query.abi_version = DMG_ABI_VERSION;
            query.intent = DMG_ENUMERATE;
            query.max_results = 65536;
            dmg_result *raw_result = nullptr;
            dex_check(dmg_query_methods(snapshot.get(), &query, &raw_result, &error), "query_methods");
            Result result(raw_result, &dmg_result_release);
            m.checksum = consume(result.get(), static_cast<uint32_t>(m.methods));
            if (m.diagnostic) { query_finished = Clock::now(); m.query_ns = elapsed(query_started, query_finished); }
            snapshot.reset();
            result.reset();
            m.lease_final_correct = input.lease_finished();
            require(m.lease_final_correct, "final_source_lease_not_released_exactly_once");
            dmg_engine_stats stats{};
            dex_check(dmg_engine_get_stats(engine.get(), &stats), "engine_final_stats");
            require(stats.borrowed_bytes == 0 && stats.active_jobs == 0, "parser_final_borrow_not_zero");
            engine.reset();
            input.finish();
            m.peak = input.peak;
        }
        const auto finished = Clock::now();
        m.total_ns = elapsed(started, finished);
        if (m.diagnostic) m.cleanup_ns = elapsed(query_finished, finished);
        m.cpu_after = current_cpu();
        m.after = usage();
        require(m.selected_cpu < 0 || (m.cpu_before == m.selected_cpu && m.cpu_after == m.selected_cpu), "selected_cpu_not_observed");
        m.success = true;
        print(m);
        return 0;
    } catch (const std::exception &failure) {
        m.failure = failure.what();
    } catch (...) {
        m.failure = "unknown_exception";
    }
    if (task_started) m.total_ns = elapsed(started);
    m.cpu_after = current_cpu();
    m.after = usage();
    print(m);
    return 1;
}
