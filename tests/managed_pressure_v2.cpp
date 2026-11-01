#include <mgmem/pressure.h>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <mutex>
#include <limits>
#include <thread>
#include <vector>
#if defined(_WIN32)
#include <malloc.h>
#endif
#if defined(MGMEM_MANAGED_PRESSURE_TESTING)
extern "C" mgmem_status mgmem_pressure_test_set_sequences_v2(mgmem_pressure_controller *, uint64_t, uint64_t);
#endif
#if defined(MGMEM_MANAGED_TESTING)
extern "C" void mgmem_managed_test_pressure_gate_arm();
extern "C" int mgmem_managed_test_pressure_gate_wait_entered();
extern "C" int mgmem_managed_test_pressure_gate_wait_close();
extern "C" int mgmem_managed_test_pressure_gate_release();
#endif

namespace {
unsigned checks = 0;
#define CHECK(x) do { ++checks; if (!(x)) { std::fprintf(stderr, "FAIL %s:%d %s\n", __FILE__, __LINE__, #x); std::abort(); } } while (false)
struct Lifetime {
    std::atomic<unsigned> retains{0}, releases{0};
    static void retain(void *p) { ++static_cast<Lifetime *>(p)->retains; }
    static void release(void *p) { ++static_cast<Lifetime *>(p)->releases; }
    mgmem_domain_lifetime_v1 table() { return {sizeof(mgmem_domain_lifetime_v1), 1, this, retain, release}; }
};
struct BlockedProvider {
    std::mutex mutex; std::condition_variable changed;
    bool entered = false, allow = false; std::thread::id thread;
    static mgmem_status allocate(void *, uint64_t size, uint64_t align, void **out) {
#if defined(_WIN32)
        *out = _aligned_malloc(static_cast<size_t>(size), static_cast<size_t>(align));
#else
        *out = nullptr; if (posix_memalign(out, static_cast<size_t>(align), static_cast<size_t>(size))) return MGMEM_OUT_OF_MEMORY;
#endif
        return *out ? MGMEM_OK : MGMEM_OUT_OF_MEMORY;
    }
    static void deallocate(void *p, void *address, uint64_t, uint64_t) {
        auto &self = *static_cast<BlockedProvider *>(p);
        { std::unique_lock<std::mutex> lock(self.mutex); self.thread = std::this_thread::get_id(); self.entered = true;
          self.changed.notify_all(); self.changed.wait(lock, [&] { return self.allow; }); }
#if defined(_WIN32)
        _aligned_free(address);
#else
        std::free(address);
#endif
    }
    static void retain(void *) {} static void release(void *) {}
};
mgmem_domain *domain(const mgmem_provider_v1 *provider = nullptr) {
    mgmem_domain_options_v1 o{sizeof(o), MGMEM_ABI_MAJOR, MGMEM_ABI_MINOR, 0, MGMEM_ARENA, 0, 1024, 256, provider};
    mgmem_domain *d = nullptr; CHECK(mgmem_domain_create_v1(&o, &d) == MGMEM_OK); return d;
}
void cleanup(mgmem_domain *d) {
    CHECK(mgmem_domain_begin_close_v1(d) == MGMEM_OK); CHECK(mgmem_domain_collect_v1(d) == MGMEM_OK);
    CHECK(mgmem_domain_drain_v1(d, 0) == MGMEM_OK); CHECK(mgmem_domain_destroy_v1(d) == MGMEM_OK);
}
mgmem_pressure_options_v2 options(uint32_t q = 8, uint32_t r = 8) { return {sizeof(mgmem_pressure_options_v2), 2, 512, 128, 5, q, r, 0}; }
mgmem_pressure_snapshot_v2 snap(mgmem_pressure_controller *c) {
    mgmem_pressure_snapshot_v2 s{}; CHECK(mgmem_pressure_snapshot_v2_get(c, &s, sizeof(s)) == MGMEM_OK); return s;
}
uint64_t submit(mgmem_pressure_controller *c, uint32_t level) {
    auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(3); uint64_t t = 0; mgmem_status status;
    do { status = mgmem_pressure_submit_v2(c, level, &t); if (status == MGMEM_BUSY) std::this_thread::yield();
    } while (status == MGMEM_BUSY && std::chrono::steady_clock::now() < deadline);
    CHECK(status == MGMEM_OK && t); return t;
}
mgmem_pressure_event_result_v2 result(mgmem_pressure_controller *c, uint64_t ticket) {
    mgmem_pressure_event_result_v2 r{}; CHECK(mgmem_pressure_event_result_v2_get(c, ticket, &r, sizeof(r)) == MGMEM_OK); return r;
}
mgmem_pressure_event_result_v2 complete(mgmem_pressure_controller *c, uint64_t ticket) {
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(3);
    for (;;) { auto r = result(c, ticket); if (r.state != MGMEM_PRESSURE_EVENT_QUEUED) return r;
        CHECK(std::chrono::steady_clock::now() < deadline); std::this_thread::yield(); }
}
void normal_and_zero() {
    auto *d = domain(); CHECK(mgmem_domain_set_budget_v1(d, 0) == MGMEM_OK);
    Lifetime life; auto l = life.table(); auto o = options(8, 1); mgmem_pressure_controller *c = nullptr;
    CHECK(mgmem_pressure_create_v2(d, &l, &o, &c) == MGMEM_OK);
    CHECK(snap(c).manual_budget_bytes == 0 && snap(c).effective_budget_bytes == 0);
    auto t1 = submit(c, MGMEM_PRESSURE_CRITICAL); CHECK(complete(c, t1).state == MGMEM_PRESSURE_EVENT_APPLIED);
    uint64_t seq = 0; CHECK(mgmem_pressure_set_manual_budget_v2(c, 1000, &seq) == MGMEM_OK && seq > 0);
    auto s = snap(c); CHECK(s.manual_budget_bytes == 1000 && s.effective_budget_bytes == 128);
    auto t2 = submit(c, MGMEM_PRESSURE_NORMAL); auto r2 = complete(c, t2);
    CHECK(r2.state == MGMEM_PRESSURE_EVENT_APPLIED && r2.apply_sequence > seq);
    CHECK(snap(c).effective_budget_bytes == 1000); CHECK(result(c, t1).state == MGMEM_PRESSURE_EVENT_EXPIRED);
    CHECK(snap(c).results_evicted == 1);
    mgmem_pressure_event_result_v2 invalid{}; CHECK(mgmem_pressure_event_result_v2_get(c, t2 + 1, &invalid, sizeof(invalid)) == MGMEM_INVALID_ARGUMENT);
    mgmem_pressure_options_v1 old{sizeof(old), 1, 1000, 512, 128, 5, 0}; mgmem_pressure_controller *dup = nullptr;
    CHECK(mgmem_pressure_create_v1(d, &l, &old, &dup) == MGMEM_BUSY && !dup);
    CHECK(mgmem_pressure_notify_v1(c, 1) == MGMEM_UNSUPPORTED);
    CHECK(mgmem_pressure_begin_close_v2(c) == MGMEM_OK); CHECK(mgmem_pressure_drain_v2(c, 3000) == MGMEM_OK);
    CHECK(mgmem_pressure_destroy_v2(c) == MGMEM_OK); CHECK(life.retains == life.releases);
    CHECK(mgmem_pressure_create_v1(d, &l, &old, &c) == MGMEM_OK);
    CHECK(mgmem_pressure_create_v2(d, &l, &o, &dup) == MGMEM_BUSY && !dup);
    CHECK(mgmem_pressure_begin_close_v1(c) == MGMEM_OK && mgmem_pressure_drain_v1(c, 3000) == MGMEM_OK);
    CHECK(mgmem_pressure_destroy_v1(c) == MGMEM_OK && life.retains == life.releases); cleanup(d);
}
void blocked_shutdown() {
    BlockedProvider provider;
    mgmem_provider_v1 table{sizeof(table), MGMEM_ABI_MAJOR, MGMEM_ABI_MINOR, 0, &provider,
        BlockedProvider::allocate, BlockedProvider::deallocate, BlockedProvider::retain, BlockedProvider::release};
    auto *d = domain(&table); mgmem_handle_v1 owner{}; CHECK(mgmem_allocate_v1(d, 128, 64, &owner) == MGMEM_OK);
    CHECK(mgmem_release_v1(d, owner) == MGMEM_OK);
    Lifetime life; auto l = life.table(); auto o = options(2, 4); mgmem_pressure_controller *c = nullptr;
    CHECK(mgmem_pressure_create_v2(d, &l, &o, &c) == MGMEM_OK);
    auto first = submit(c, MGMEM_PRESSURE_CRITICAL); CHECK(complete(c, first).state == MGMEM_PRESSURE_EVENT_APPLIED);
    { std::unique_lock<std::mutex> lock(provider.mutex); CHECK(provider.changed.wait_for(lock, std::chrono::seconds(3), [&] { return provider.entered; })); }
    CHECK(provider.thread != std::this_thread::get_id());
    uint64_t manual_seq = 0; CHECK(mgmem_pressure_set_manual_budget_v2(c, 500, &manual_seq) == MGMEM_OK);
    CHECK(manual_seq > 0 && snap(c).trim_pending == 1 && snap(c).effective_budget_bytes == 128);
    auto a = submit(c, MGMEM_PRESSURE_MODERATE); auto b = submit(c, MGMEM_PRESSURE_NORMAL); uint64_t rejected = 55;
    CHECK(mgmem_pressure_submit_v2(c, MGMEM_PRESSURE_CRITICAL, &rejected) == MGMEM_BUSY && rejected == 0);
    CHECK(mgmem_pressure_begin_close_v2(c) == MGMEM_OK); CHECK(mgmem_pressure_drain_v2(c, 5) == MGMEM_TIMEOUT);
    CHECK(mgmem_pressure_destroy_v2(c) == MGMEM_BUSY && life.releases == 0);
    { std::lock_guard<std::mutex> lock(provider.mutex); provider.allow = true; provider.changed.notify_all(); }
    CHECK(mgmem_pressure_drain_v2(c, 3000) == MGMEM_OK);
    CHECK(result(c, a).state == MGMEM_PRESSURE_EVENT_CANCELLED && result(c, b).state == MGMEM_PRESSURE_EVENT_CANCELLED);
    auto s = snap(c); CHECK(s.events_accepted == 3 && s.events_applied == 1 && s.events_cancelled == 2 && s.events_busy >= 1);
    CHECK(s.events_critical_busy >= 1);
    CHECK(mgmem_pressure_destroy_v2(c) == MGMEM_OK); CHECK(life.retains == life.releases); cleanup(d);
}
void applied_failure() {
    auto *d = domain(); Lifetime life; auto l = life.table(); auto o = options(); mgmem_pressure_controller *c = nullptr;
    CHECK(mgmem_pressure_create_v2(d, &l, &o, &c) == MGMEM_OK);
    CHECK(mgmem_domain_begin_close_v1(d) == MGMEM_OK); // Deliberate contract violation tests error propagation.
    auto t = submit(c, MGMEM_PRESSURE_MODERATE); auto r = complete(c, t);
    CHECK(r.state == MGMEM_PRESSURE_EVENT_FAILED && r.status == MGMEM_IS_CLOSED);
    uint64_t seq = 1; CHECK(mgmem_pressure_set_manual_budget_v2(c, 10, &seq) == MGMEM_IS_CLOSED && seq == 0);
    auto s = snap(c); CHECK(s.manual_budget_bytes == 1024 && s.applied_pressure == MGMEM_PRESSURE_NORMAL && s.events_failed == 1);
    CHECK(mgmem_pressure_begin_close_v2(c) == MGMEM_OK); CHECK(mgmem_pressure_drain_v2(c, 3000) == MGMEM_PROVIDER_ERROR);
    CHECK(mgmem_pressure_destroy_v2(c) == MGMEM_PROVIDER_ERROR && life.releases == 0);
    // Deliberately leave the quarantined record and owner pin intact until process exit.
}
void concurrent_arbitration() {
    auto *d = domain(); Lifetime life; auto l = life.table(); auto o = options(8, 512); mgmem_pressure_controller *c = nullptr;
    CHECK(mgmem_pressure_create_v2(d, &l, &o, &c) == MGMEM_OK);
    constexpr size_t per_thread = 100, writers = 3;
    uint64_t tickets[writers * per_thread]{}; std::atomic<unsigned> failures{0};
    std::vector<std::thread> threads;
    for (size_t w = 0; w < writers; ++w) threads.emplace_back([&, w] {
        for (size_t i = 0; i < per_thread; ++i) {
            uint64_t ticket = 0; mgmem_status status;
            do { status = mgmem_pressure_submit_v2(c, static_cast<uint32_t>((w + i) % 3), &ticket);
                 if (status == MGMEM_BUSY) std::this_thread::yield(); } while (status == MGMEM_BUSY);
            if (status != MGMEM_OK || !ticket) { ++failures; return; }
            tickets[w * per_thread + i] = ticket;
        }
    });
    threads.emplace_back([&] {
        for (unsigned i = 0; i < 300; ++i) {
            uint64_t seq = 0;
            if (mgmem_pressure_set_manual_budget_v2(c, (i % 5) * 200, &seq) != MGMEM_OK || !seq) { ++failures; return; }
            mgmem_pressure_snapshot_v2 s{};
            if (mgmem_pressure_snapshot_v2_get(c, &s, sizeof(s)) != MGMEM_OK) { ++failures; return; }
            const auto expected = s.applied_pressure == MGMEM_PRESSURE_NORMAL ? s.manual_budget_bytes :
                (std::min)(s.manual_budget_bytes, s.applied_pressure == MGMEM_PRESSURE_MODERATE ? uint64_t{512} : uint64_t{128});
            if (s.effective_budget_bytes != expected) { ++failures; return; }
        }
    });
    for (auto &thread : threads) thread.join();
    CHECK(failures == 0);
    for (auto ticket : tickets) CHECK(complete(c, ticket).state == MGMEM_PRESSURE_EVENT_APPLIED);
    auto s = snap(c); mgmem_snapshot_v1 ds{};
    CHECK(s.events_accepted == writers * per_thread && s.events_applied == s.events_accepted);
    CHECK(mgmem_domain_snapshot_v1(d, &ds, sizeof(ds)) == MGMEM_OK && ds.budget_bytes == s.effective_budget_bytes);
    CHECK(mgmem_pressure_begin_close_v2(c) == MGMEM_OK); CHECK(mgmem_pressure_drain_v2(c, 3000) == MGMEM_OK);
    CHECK(mgmem_pressure_destroy_v2(c) == MGMEM_OK && life.retains == life.releases); cleanup(d);
}
#if defined(MGMEM_MANAGED_PRESSURE_TESTING)
void sequence_limits() {
    auto *d = domain(); Lifetime life; auto l = life.table(); auto o = options(); mgmem_pressure_controller *c = nullptr;
    CHECK(mgmem_pressure_create_v2(d, &l, &o, &c) == MGMEM_OK);
    const auto max = (std::numeric_limits<uint64_t>::max)();
    CHECK(mgmem_pressure_test_set_sequences_v2(c, max - 1, max) == MGMEM_OK);
    auto ticket = submit(c, MGMEM_PRESSURE_MODERATE); CHECK(ticket == max);
    auto r = complete(c, ticket); CHECK(r.state == MGMEM_PRESSURE_EVENT_FAILED && r.status == MGMEM_OVERFLOW);
    uint64_t next = 1; CHECK(mgmem_pressure_submit_v2(c, MGMEM_PRESSURE_NORMAL, &next) == MGMEM_OVERFLOW && next == 0);
    CHECK(mgmem_pressure_set_manual_budget_v2(c, 0, &next) == MGMEM_OVERFLOW && next == 0);
    CHECK(snap(c).effective_budget_bytes == 1024 && snap(c).manual_budget_bytes == 1024);
    CHECK(mgmem_pressure_begin_close_v2(c) == MGMEM_OK && mgmem_pressure_drain_v2(c, 3000) == MGMEM_OK);
    CHECK(mgmem_pressure_destroy_v2(c) == MGMEM_OK && life.retains == life.releases); cleanup(d);
}
#endif
void worker_limit() {
    Lifetime life; auto l = life.table(); auto o = options();
    mgmem_domain *domains[MGMEM_PRESSURE_V2_MAX_WORKERS + 1]{};
    mgmem_pressure_controller *controls[MGMEM_PRESSURE_V2_MAX_WORKERS + 1]{};
    for (unsigned i = 0; i <= MGMEM_PRESSURE_V2_MAX_WORKERS; ++i) {
        domains[i] = domain();
        const auto status = mgmem_pressure_create_v2(domains[i], &l, &o, &controls[i]);
        CHECK(status == (i < MGMEM_PRESSURE_V2_MAX_WORKERS ? MGMEM_OK : MGMEM_BUSY));
    }
    CHECK(!controls[MGMEM_PRESSURE_V2_MAX_WORKERS]);
    for (unsigned i = 0; i < MGMEM_PRESSURE_V2_MAX_WORKERS; ++i) {
        CHECK(mgmem_pressure_begin_close_v2(controls[i]) == MGMEM_OK);
        CHECK(mgmem_pressure_drain_v2(controls[i], 3000) == MGMEM_OK);
        CHECK(mgmem_pressure_destroy_v2(controls[i]) == MGMEM_OK); cleanup(domains[i]);
    }
    CHECK(life.retains == life.releases); cleanup(domains[MGMEM_PRESSURE_V2_MAX_WORKERS]);
}
#if defined(MGMEM_MANAGED_TESTING)
void controlled_worker_close() {
    auto *d = domain(); Lifetime life; auto l = life.table(); auto o = options(); mgmem_pressure_controller *c = nullptr;
    CHECK(mgmem_pressure_create_v2(d, &l, &o, &c) == MGMEM_OK);
    mgmem_managed_test_pressure_gate_arm();
    const auto first = submit(c, MGMEM_PRESSURE_CRITICAL);
    CHECK(mgmem_managed_test_pressure_gate_wait_entered() == 1);
    const auto second = submit(c, MGMEM_PRESSURE_NORMAL);
    CHECK(mgmem_pressure_begin_close_v2(c) == MGMEM_OK);
    CHECK(mgmem_managed_test_pressure_gate_wait_close() == 1);
    CHECK(mgmem_managed_test_pressure_gate_release() == 1);
    CHECK(mgmem_pressure_drain_v2(c, 3000) == MGMEM_OK);
    CHECK(result(c, first).state == MGMEM_PRESSURE_EVENT_CANCELLED);
    CHECK(result(c, second).state == MGMEM_PRESSURE_EVENT_CANCELLED);
    CHECK(mgmem_pressure_destroy_v2(c) == MGMEM_OK && life.retains == life.releases); cleanup(d);
}
#endif
}
int main() { normal_and_zero(); blocked_shutdown(); concurrent_arbitration();
#if defined(MGMEM_MANAGED_PRESSURE_TESTING)
    sequence_limits();
#endif
    worker_limit();
#if defined(MGMEM_MANAGED_TESTING)
    controlled_worker_close();
#endif
    applied_failure(); std::printf("managed-pressure-v2 PASS (%u checks)\n", checks); }
