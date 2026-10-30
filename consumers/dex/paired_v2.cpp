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
#include <memory>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#if defined(_WIN32)
#include <malloc.h>
#elif defined(__linux__)
#include <sched.h>
#endif

namespace {
using Clock = std::chrono::steady_clock;
constexpr uint64_t input_budget = 32ull * 1024 * 1024;
constexpr uint64_t parser_budget = 256ull * 1024 * 1024;
constexpr uint64_t alignment = 64;
#if defined(MGMEM_DEX_V2_DIAGNOSTIC)
constexpr bool diagnostic = true;
#else
constexpr bool diagnostic = false;
#endif
void require(bool value, const char *message) { if (!value) throw std::runtime_error(message); }
void memory_check(mgmem_status status, const char *name) {
    if (status != MGMEM_OK) throw std::runtime_error(std::string(name) + "_mgmem_" + std::to_string(status));
}
void dex_check(dmg_status status, const char *name) {
    if (status != DMG_OK) throw std::runtime_error(std::string(name) + "_dex_" + std::to_string(status));
}
uint64_t elapsed(Clock::time_point start, Clock::time_point end = Clock::now()) {
    return static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(end - start).count());
}
std::string escaped(const std::string &value) {
    std::string out;
    for (unsigned char c : value) {
        if (c == '"' || c == '\\') { out += '\\'; out += static_cast<char>(c); }
        else if (c >= 32 && c < 127) out += static_cast<char>(c);
        else out += '?';
    }
    return out;
}
void *system_allocate(uint64_t bytes) {
    void *address = nullptr;
#if defined(_WIN32)
    address = _aligned_malloc(static_cast<size_t>(bytes), alignment);
#else
    if (posix_memalign(&address, alignment, static_cast<size_t>(bytes)) != 0) address = nullptr;
#endif
    return address;
}
void system_free(void *address) noexcept {
#if defined(_WIN32)
    _aligned_free(address);
#else
    std::free(address);
#endif
}
void select_cpu(int cpu) {
    if (cpu < 0) return;
#if defined(__linux__)
    require(cpu < CPU_SETSIZE, "affinity_cpu_out_of_range");
    cpu_set_t mask; CPU_ZERO(&mask); CPU_SET(cpu, &mask);
    require(sched_setaffinity(0, sizeof(mask), &mask) == 0, "affinity_set_failed");
    CPU_ZERO(&mask);
    require(sched_getaffinity(0, sizeof(mask), &mask) == 0 && CPU_ISSET(cpu, &mask) && CPU_COUNT(&mask) == 1,
            "affinity_not_exactly_selected_cpu");
#else
    throw std::runtime_error("affinity_unsupported");
#endif
}
int current_cpu() {
#if defined(__linux__)
    return sched_getcpu();
#else
    return -1;
#endif
}
using Engine = std::unique_ptr<dmg_engine, decltype(&dmg_engine_release)>;
using Snapshot = std::unique_ptr<dmg_snapshot, decltype(&dmg_snapshot_release)>;
using Result = std::unique_ptr<dmg_result, decltype(&dmg_result_release)>;
using Query = std::unique_ptr<dmg_query, decltype(&dmg_query_release)>;
using Job = std::unique_ptr<dmg_job, decltype(&dmg_job_release)>;

// This object and the preloaded vector never move while the SDK owns a lease.
// Source itself is immutable after prepare; the callback only drops ownership.
struct InputOwner {
    enum class Kind { direct_copy, direct_borrow, memory_system, memory_arena };
    Kind kind;
    uint64_t bytes;
    void *address = nullptr;
    mgmem_domain *domain = nullptr;
    mgmem_handle_v1 allocation{};
    mgmem_borrow_v1 writer{}, reader{};
    bool allocation_live = false, writer_live = false, reader_live = false, caller_live = true;
    std::atomic<uint32_t> references{1}, retains{0}, releases{0};
    std::atomic<mgmem_status> final_status{MGMEM_BUSY};
    std::atomic<bool> final_released{false}, final_on_other_thread{false};
    const std::thread::id creating_thread = std::this_thread::get_id();
    uint64_t peak = 0, provider_published = 0, retained_before_collect = 0;

    InputOwner(Kind value, uint64_t length) : kind(value), bytes(length) {}
    InputOwner(const InputOwner &) = delete;
    InputOwner &operator=(const InputOwner &) = delete;
    bool memory() const noexcept { return kind == Kind::memory_system || kind == Kind::memory_arena; }
    bool copied() const noexcept { return kind != Kind::direct_borrow; }
    void allocate(std::vector<uint8_t> &source) {
        require(bytes > 0 && bytes <= input_budget && bytes == source.size(), "input_budget_rejection");
        if (kind == Kind::direct_borrow) { address = source.data(); return; }
        if (!memory()) {
            address = system_allocate(bytes);
            require(address != nullptr, "system_allocation_failed");
            peak = bytes;
            return;
        }
        const mgmem_domain_options_v1 options{sizeof(options), MGMEM_ABI_MAJOR, MGMEM_ABI_MINOR,
            0, kind == Kind::memory_arena ? MGMEM_ARENA : MGMEM_SYSTEM, 0, input_budget, bytes, nullptr};
        memory_check(mgmem_domain_create_v1(&options, &domain), "domain_create");
        memory_check(mgmem_allocate_v1(domain, bytes, alignment, &allocation), "allocate");
        allocation_live = true;
    }
    void initialize(const std::vector<uint8_t> &source) {
        if (!copied()) return;
        if (memory()) {
            memory_check(mgmem_borrow_acquire_v1(domain, allocation, 0, bytes, MGMEM_WRITE, &writer), "write_acquire");
            writer_live = true;
            address = writer.data;
        }
        std::memcpy(address, source.data(), static_cast<size_t>(bytes));
        if (memory()) {
            memory_check(mgmem_borrow_release_v1(domain, writer.lease), "write_release");
            writer_live = false;
            memory_check(mgmem_borrow_acquire_v1(domain, allocation, 0, bytes, MGMEM_READ, &reader), "read_acquire");
            reader_live = true;
            address = reader.data;
        }
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
        if (self.memory() && self.reader_live) {
            status = mgmem_borrow_release_v1(self.domain, self.reader.lease);
            self.reader_live = false;
        } else if (self.kind == Kind::direct_copy) system_free(self.address);
        self.final_on_other_thread.store(std::this_thread::get_id() != self.creating_thread, std::memory_order_relaxed);
        self.final_status.store(status, std::memory_order_release);
        self.final_released.store(true, std::memory_order_release);
    }
    dmg_buffer_lease sdk_lease() noexcept { return {sizeof(dmg_buffer_lease), DMG_ABI_VERSION, this, &retain, &release, 1, 0}; }
    void release_caller() noexcept { if (caller_live) { caller_live = false; release(this); } }
    void retire_owner() {
        release_caller();
        if (memory()) {
            memory_check(mgmem_release_v1(domain, allocation), "owner_release");
            allocation_live = false;
            memory_check(mgmem_domain_begin_close_v1(domain), "begin_close");
        }
    }
    void finish() {
        require(final_released.load(std::memory_order_acquire) && references.load() == 0 &&
                releases.load() == retains.load() + 1 && final_status.load(std::memory_order_acquire) == MGMEM_OK,
                "input_callback_balance_failed");
        if (memory()) {
            mgmem_snapshot_v1 before_collect{};
            memory_check(mgmem_domain_snapshot_v1(domain, &before_collect, sizeof(before_collect)), "snapshot_before_collect");
            retained_before_collect = before_collect.retained_bytes;
            memory_check(mgmem_domain_collect_v1(domain), "collect");
            memory_check(mgmem_domain_drain_v1(domain, 0), "drain");
            mgmem_snapshot_v1 state{};
            memory_check(mgmem_domain_snapshot_v1(domain, &state, sizeof(state)), "snapshot_final");
            require(state.state == MGMEM_CLOSED && state.backing_bytes == 0 && state.reserved_bytes == 0 &&
                    state.active_allocations == 0 && state.active_leases == 0 && state.retiring_bytes == 0,
                    "final_resources_not_zero");
            peak = state.peak_backing_bytes;
            provider_published = state.provider_allocation_count;
            memory_check(mgmem_domain_destroy_v1(domain), "destroy");
            domain = nullptr;
        }
        address = nullptr;
    }
    ~InputOwner() noexcept {
        release_caller();
        // SDK RAII wrappers declared after this object have already been joined
        // or destroyed. Leaking a callback into a dead stack context is fatal.
        if (!final_released.load(std::memory_order_acquire) || references.load() != 0) std::terminate();
        if (!memory() || !domain) return;
        if (writer_live && mgmem_borrow_release_v1(domain, writer.lease) != MGMEM_OK) std::terminate();
        if (reader_live || final_status.load(std::memory_order_acquire) != MGMEM_OK) std::terminate();
        if (allocation_live && mgmem_release_v1(domain, allocation) != MGMEM_OK) std::terminate();
        if (mgmem_domain_begin_close_v1(domain) != MGMEM_OK || mgmem_domain_collect_v1(domain) != MGMEM_OK ||
            mgmem_domain_drain_v1(domain, 0) != MGMEM_OK || mgmem_domain_destroy_v1(domain) != MGMEM_OK) std::terminate();
    }
};
InputOwner::Kind kind(const std::string &variant) {
    if (variant == "D-C0") return InputOwner::Kind::direct_copy;
    if (variant == "D-Z0") return InputOwner::Kind::direct_borrow;
    if (variant == "D-C1") return InputOwner::Kind::memory_system;
    if (variant == "D-C2") return InputOwner::Kind::memory_arena;
    throw std::runtime_error("variant_must_be_D-C0_D-Z0_D-C1_or_D-C2");
}
uint64_t mix(uint64_t hash, uint64_t word) {
    for (unsigned i = 0; i < 8; ++i) { hash = (hash ^ (word & 255u)) * UINT64_C(1099511628211); word >>= 8; }
    return hash;
}
uint64_t consume(dmg_result *result, uint32_t expected) {
    dmg_result_info outcome{};
    dex_check(dmg_result_get_info(result, &outcome), "query_outcome");
    require(outcome.coverage == DMG_COVERAGE_COMPLETE && outcome.execution_status == DMG_OK &&
            outcome.candidates_count == expected, "query_incomplete");
    uint64_t hash = UINT64_C(14695981039346656037);
    for (uint32_t index = 0; index < expected; ++index) {
        dmg_method_row row{};
        dex_check(dmg_result_get_method(result, index, &row), "query_row");
        hash = mix(hash, row.method_index); hash = mix(hash, row.access_flags);
        hash = mix(hash, row.declaration_offset); hash = mix(hash, row.code_offset);
        const uint16_t *texts[] = {row.owner, row.name, row.prototype};
        const uint64_t lengths[] = {row.owner_length, row.name_length, row.prototype_length};
        for (unsigned part = 0; part < 3; ++part) {
            require(texts[part] || lengths[part] == 0, "invalid_method_view");
            hash = mix(hash, lengths[part]);
            for (uint64_t i = 0; i < lengths[part]; ++i) hash = mix(hash, texts[part][i]);
        }
    }
    return hash;
}
struct Metrics {
    std::string variant = "UNKNOWN", test_case = "normal", source_sha256, failure;
    bool success = false, source_final_released = false, lease_final_correct = false, snapshot_released_before_last_read = false;
    bool final_release_on_other_thread = false;
    int selected_cpu = -1, cpu_before = -1, cpu_after = -1, expected_status = DMG_OK, observed_status = DMG_OK;
    uint64_t bytes = 0, external_capacity = 0, methods = 0, checksum = 0, preload_ns = 0, total_ns = 0;
    uint64_t peak = 0, provider_published = 0, parser_peak = 0, borrowed_peak = 0, retains = 0, releases = 0, retained = 0;
    // Nine non-overlapping external API phases. Parsing/digest remains a single
    // snapshot_open phase; no allocator-only interpretation is supported.
    uint64_t phases[9]{};
};
void print(const Metrics &m) {
    std::cout << "{\"schema_version\":2,\"workload\":\"dex_source_strategy_complete_owner_v2\",\"variant\":\""
              << escaped(m.variant) << "\",\"case\":\"" << escaped(m.test_case) << "\",\"success\":" << (m.success ? "true" : "false")
              << ",\"diagnostic\":" << (diagnostic ? "true" : "false") << ",\"failure_reason\":";
    if (m.failure.empty()) std::cout << "null"; else std::cout << '"' << escaped(m.failure) << '"';
    std::cout << ",\"source_sha256\":";
    if (m.source_sha256.empty()) std::cout << "null"; else std::cout << '"' << m.source_sha256 << '"';
    std::cout << ",\"input_bytes\":" << m.bytes << ",\"method_count\":" << m.methods << ",\"result_checksum\":" << m.checksum
              << ",\"preload_ns\":" << m.preload_ns << ",\"total_ns\":" << m.total_ns << ",\"phases\":";
    if constexpr (diagnostic) {
        const char *names[] = {"owner_allocate", "copy_first_touch_and_lease", "engine_create", "snapshot_open_parse_digest",
            "info_and_retire", "query_and_complete_consume", "sdk_release", "memory_close", "source_final_release"};
        std::cout << '{';
        uint64_t sum = 0;
        for (unsigned i = 0; i < 9; ++i) { if (i) std::cout << ','; std::cout << '"' << names[i] << "_ns\":" << m.phases[i]; sum += m.phases[i]; }
        std::cout << ",\"unattributed_residual_ns\":" << (m.total_ns >= sum ? m.total_ns - sum : 0) << '}';
    } else std::cout << "null";
    std::cout << ",\"logical_requested_bytes\":" << m.bytes << ",\"unique_owned_backing_peak_bytes\":" << m.peak
              << ",\"external_source_capacity_bytes\":" << m.external_capacity << ",\"borrowed_view_peak_bytes\":" << m.borrowed_peak
              << ",\"source_plus_input_unique_capacity_bytes\":" << m.external_capacity + m.peak
              << ",\"retained_before_collect_bytes\":" << m.retained
              << ",\"reserved_final_bytes\":" << (m.success ? "0" : "null")
              << ",\"arena_used_bytes\":" << (m.variant == "D-C2" && m.success ? m.bytes : 0)
              << ",\"input_backing_padding_bytes\":" << (m.success ? "0" : "null")
              << ",\"memory_metadata_bytes\":null,\"rss_pss_bytes\":null,\"energy\":null,\"provider_attempt_count\":null"
              << ",\"provider_published_count\":" << m.provider_published << ",\"parser_peak_owned_bytes\":" << m.parser_peak
              << ",\"input_budget_bytes\":" << input_budget << ",\"parser_budget_bytes\":" << parser_budget
              << ",\"callback_retains\":" << m.retains << ",\"callback_releases_including_caller\":" << m.releases
              << ",\"expected_status\":" << m.expected_status << ",\"observed_status\":" << m.observed_status
              << ",\"lease_final_correct\":" << (m.lease_final_correct ? "true" : "false")
              << ",\"snapshot_released_before_last_read\":" << (m.snapshot_released_before_last_read ? "true" : "false")
              << ",\"source_final_released\":" << (m.source_final_released ? "true" : "false")
              << ",\"final_release_on_other_thread\":" << (m.final_release_on_other_thread ? "true" : "false")
              << ",\"closed_backing_bytes\":";
    if (m.success) std::cout << 0; else std::cout << "null";
    std::cout << ",\"final_resource_state\":\"" << (m.success ? "CLOSED_ZERO" : "UNQUALIFIED") << '"'
              << ",\"selected_cpu\":";
    if (m.selected_cpu < 0) std::cout << "null"; else std::cout << m.selected_cpu;
    std::cout << ",\"cpu_before\":"; if (m.cpu_before < 0) std::cout << "null"; else std::cout << m.cpu_before;
    std::cout << ",\"cpu_after\":"; if (m.cpu_after < 0) std::cout << "null"; else std::cout << m.cpu_after;
    std::cout << ",\"timing_scope\":\"input_strategy_through_common_source_final_release_excludes_preload_and_loader\""
              << ",\"resource_scope\":\"source_capacity_plus_unique_input_backing_parser_separate_metadata_RSS_unknown\""
              << ",\"product_scope\":\"independent_DexMeguviz_not_BIL_DexKit\"}\n";
}
} // namespace

int main(int argc, char **argv) {
    Metrics m;
    Clock::time_point started{};
    bool task_started = false;
    try {
        require(argc >= 4 && argc <= 8, "usage_file_--variant_D-C0_D-Z0_D-C1_D-C2_optional_--case_name_--cpu_N");
        bool variant_set = false, case_set = false, cpu_set = false;
        for (int i = 2; i < argc; ++i) {
            const std::string option = argv[i];
            if (option == "--variant") {
                require(!variant_set && ++i < argc, "missing_or_duplicate_variant"); m.variant = argv[i]; variant_set = true;
            } else if (option == "--case") {
                require(!case_set && ++i < argc, "missing_or_duplicate_case"); m.test_case = argv[i]; case_set = true;
            } else if (option == "--cpu") {
                require(!cpu_set && ++i < argc, "missing_or_duplicate_cpu");
                const std::string value = argv[i];
                require(!value.empty() && value.size() <= 5 && value.find_first_not_of("0123456789") == std::string::npos, "invalid_cpu");
                m.selected_cpu = std::stoi(value); cpu_set = true;
            } else throw std::runtime_error("unknown_option");
        }
        require(variant_set, "missing_variant");
        const auto mode = kind(m.variant);
        require(m.test_case == "normal" || m.test_case == "cross-thread" || m.test_case == "invalid-lease" ||
                m.test_case == "corrupt-input" || m.test_case == "borrow-budget" || m.test_case == "query-closed" ||
                m.test_case == "cancel-job", "unknown_case");
        select_cpu(m.selected_cpu);
        require(dmg_abi_version() == DMG_ABI_VERSION, "SDK_ABI_mismatch");
        const auto preload = Clock::now();
        std::ifstream file(argv[1], std::ios::binary | std::ios::ate);
        require(file.good(), "open_input_failed");
        const auto end = file.tellg();
        require(end > 0 && static_cast<uint64_t>(end) <= input_budget && static_cast<uint64_t>(end) <= DMG_MAX_SOURCE_BYTES, "input_size_outside_budget");
        m.bytes = static_cast<uint64_t>(end);
        std::vector<uint8_t> source(static_cast<size_t>(m.bytes));
        file.seekg(0); file.read(reinterpret_cast<char *>(source.data()), static_cast<std::streamsize>(m.bytes));
        require(file.good() && static_cast<uint64_t>(file.gcount()) == m.bytes, "read_input_incomplete");
        file.close();
        if (m.test_case == "corrupt-input") source[0] = 0;
        m.external_capacity = source.capacity();
        m.preload_ns = elapsed(preload);
        m.cpu_before = current_cpu();
        started = Clock::now(); task_started = true;
        auto phase = started;
        const auto mark = [&](unsigned index) {
            if constexpr (diagnostic) { const auto now = Clock::now(); m.phases[index] = elapsed(phase, now); phase = now; }
        };
        {
            InputOwner input(mode, m.bytes);
            input.allocate(source); mark(0);
            input.initialize(source); mark(1);
            const dmg_engine_options engine_options{sizeof(engine_options), DMG_ABI_VERSION, parser_budget,
                m.test_case == "borrow-budget" ? m.bytes - 1 : input_budget, input_budget, 1, 0};
            dmg_engine *raw_engine = nullptr; dmg_error error{};
            dex_check(dmg_engine_create(&engine_options, &raw_engine, &error), "engine_create");
            Engine engine(raw_engine, &dmg_engine_release); mark(2);
            const dmg_open_options options{sizeof(options), DMG_ABI_VERSION, input_budget, "memory-source-strategy-v2", "fixed-input", nullptr};
            auto lease = input.sdk_lease();
            if (m.test_case == "invalid-lease") lease.immutable = 0;
            dmg_snapshot *raw_snapshot = nullptr;
            const auto opened = dmg_snapshot_open_borrowed(engine.get(), static_cast<const uint8_t *>(input.address), m.bytes,
                &lease, &options, &raw_snapshot, &error);
            Snapshot snapshot(raw_snapshot, &dmg_snapshot_release); mark(3);
            uint32_t expected_retains = 1;
            if (m.test_case == "invalid-lease" || m.test_case == "corrupt-input" || m.test_case == "borrow-budget") {
                m.expected_status = m.test_case == "invalid-lease" ? DMG_INVALID_ARGUMENT :
                    (m.test_case == "corrupt-input" ? DMG_CORRUPT_INPUT : DMG_BUDGET_EXCEEDED);
                m.observed_status = opened;
                expected_retains = m.test_case == "corrupt-input" ? 1 : 0;
                require(static_cast<int>(opened) == m.expected_status && !snapshot, "expected_open_rejection_missing");
                m.borrowed_peak = expected_retains ? m.bytes : 0;
                input.retire_owner(); mark(4); mark(5);
            } else {
                dex_check(opened, "snapshot_open_borrowed");
                require(input.references.load() == 2 && input.retains.load() == 1, "source_not_retained");
                dmg_snapshot_info info{};
                dex_check(dmg_snapshot_get_info(snapshot.get(), &info), "snapshot_info");
                require(info.method_def_count > 0 && info.method_def_count <= 65536 && info.source_sha256 && std::strlen(info.source_sha256) == 64,
                        "input_identity_unavailable");
                m.source_sha256 = info.source_sha256; m.methods = info.method_def_count; m.borrowed_peak = m.bytes;
                input.retire_owner(); mark(4);
                dmg_method_query query{}; query.struct_size = sizeof(query); query.abi_version = DMG_ABI_VERSION;
                query.intent = DMG_ENUMERATE; query.max_results = 65536;
                if (m.test_case == "cancel-job") {
                    require(m.methods > 1, "cancel_case_requires_multiple_methods");
                    dmg_query *raw_query = nullptr;
                    dex_check(dmg_query_compile_methods(engine.get(), &query, &raw_query, &error), "compile");
                    Query compiled(raw_query, &dmg_query_release);
                    const dmg_job_options job_options{sizeof(job_options), DMG_ABI_VERSION, 1, 1, 0};
                    dmg_job *raw_job = nullptr;
                    dex_check(dmg_job_start(snapshot.get(), compiled.get(), &job_options, &raw_job, &error), "job_start");
                    Job job(raw_job, &dmg_job_release);
                    snapshot.reset(); compiled.reset();
                    dmg_job_cancel(job.get());
                    dmg_job_status status{};
                    dex_check(dmg_job_wait(job.get(), 10000, &status), "job_wait");
                    require(status.terminal && status.lifecycle == DMG_JOB_CANCELLED && status.outcome.execution_status == DMG_CANCELLED,
                            "job_not_cancelled");
                    m.expected_status = m.observed_status = DMG_CANCELLED;
                    mark(5);
                    // Join and drop queued batches on another thread; final SDK
                    // release may now occur there, with context still alive.
                    std::thread releaser([raw = job.release()] { dmg_job_release(raw); }); releaser.join();
                } else if (m.test_case == "query-closed") {
                    dmg_snapshot_close(snapshot.get());
                    dmg_result *raw_result = nullptr;
                    const auto status = dmg_query_methods(snapshot.get(), &query, &raw_result, &error);
                    Result result(raw_result, &dmg_result_release);
                    m.expected_status = DMG_CLOSED; m.observed_status = status;
                    require(status == DMG_CLOSED && !result, "closed_query_not_rejected");
                    mark(5);
                } else {
                    dmg_result *raw_result = nullptr;
                    dex_check(dmg_query_methods(snapshot.get(), &query, &raw_result, &error), "query_methods");
                    Result result(raw_result, &dmg_result_release);
                    snapshot.reset();
                    require(input.references.load() == 1 && !input.final_released.load(), "result_did_not_keep_input_alive");
                    m.checksum = consume(result.get(), static_cast<uint32_t>(m.methods));
                    m.snapshot_released_before_last_read = true;
                    mark(5);
                    if (m.test_case == "cross-thread") {
                        std::thread releaser([raw = result.release()] { dmg_result_release(raw); }); releaser.join();
                    }
                }
            }
            snapshot.reset();
            require(input.retains.load() == expected_retains, "unexpected_retain_count");
            dmg_engine_stats stats{};
            dex_check(dmg_engine_get_stats(engine.get(), &stats), "final_stats");
            require(stats.borrowed_bytes == 0 && stats.active_jobs == 0, "SDK_final_resources_not_zero");
            m.parser_peak = stats.peak_owned_bytes;
            engine.reset(); mark(6);
            input.finish();
            m.peak = input.peak; m.provider_published = input.provider_published;
            m.retained = input.retained_before_collect;
            m.final_release_on_other_thread = input.final_on_other_thread.load();
            require((m.test_case != "cross-thread" && m.test_case != "cancel-job") || m.final_release_on_other_thread,
                    "final_release_did_not_run_on_other_thread");
            m.retains = input.retains.load(); m.releases = input.releases.load(); m.lease_final_correct = true;
            mark(7);
        }
        // Equal policy: the original preloaded source is owned by this task in
        // every variant, and its final vector deallocation is inside total_ns.
        std::vector<uint8_t>().swap(source);
        m.source_final_released = source.capacity() == 0;
        require(m.source_final_released, "source_not_finally_released"); mark(8);
        m.total_ns = elapsed(started);
        m.cpu_after = current_cpu();
        require(m.selected_cpu < 0 || (m.cpu_before == m.selected_cpu && m.cpu_after == m.selected_cpu), "selected_cpu_not_observed");
        m.success = true; print(m); return 0;
    } catch (const std::exception &error) { m.failure = error.what(); }
      catch (...) { m.failure = "unknown_exception"; }
    if (task_started) m.total_ns = elapsed(started);
    m.cpu_after = current_cpu(); print(m); return 1;
}
