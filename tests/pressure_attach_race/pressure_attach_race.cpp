#include <mgmem/pressure.h>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <initializer_list>
#include <mutex>
#include <new>
#include <thread>

namespace {
using Clock = std::chrono::steady_clock;
struct Gate {
    std::mutex mutex;
    std::condition_variable changed;
    bool entered = false, resume = false, timed_out = false;
} gate;
thread_local bool gate_next_snapshot = false;
thread_local mgmem_domain *fail_snapshot_for = nullptr;

void check(bool value, const char *what) {
    if (!value) { std::fprintf(stderr, "FAIL %s\n", what); std::abort(); }
}
struct Lifetime {
    std::atomic<unsigned> retains{0}, releases{0};
    static void retain(void *p) noexcept { ++static_cast<Lifetime *>(p)->retains; }
    static void release(void *p) noexcept { ++static_cast<Lifetime *>(p)->releases; }
    mgmem_domain_lifetime_v1 table() noexcept { return {sizeof(mgmem_domain_lifetime_v1), 1, this, retain, release}; }
};
mgmem_domain *make_domain() {
    mgmem_domain_options_v1 o{sizeof(o), MGMEM_ABI_MAJOR, MGMEM_ABI_MINOR, 0, MGMEM_SYSTEM, 0, 100, 0, nullptr};
    mgmem_domain *d = nullptr;
    check(mgmem_domain_create_v1(&o, &d) == MGMEM_OK && d, "domain create");
    return d;
}
void destroy_domain(mgmem_domain *d) {
    check(mgmem_domain_begin_close_v1(d) == MGMEM_OK, "domain begin close");
    check(mgmem_domain_collect_v1(d) == MGMEM_OK, "domain collect");
    check(mgmem_domain_drain_v1(d, 0) == MGMEM_OK, "domain drain");
    check(mgmem_domain_destroy_v1(d) == MGMEM_OK, "domain destroy");
}
mgmem_status create(unsigned mode, mgmem_domain *d, const mgmem_domain_lifetime_v1 *life,
                    mgmem_pressure_controller **out) {
    if (mode == 1) {
        const mgmem_pressure_options_v1 options{sizeof(options), 1, 100, 50, 0, 5, 0};
        return mgmem_pressure_create_v1(d, life, &options, out);
    }
    const mgmem_pressure_options_v2 options{sizeof(options), 2, 50, 0, 5, 8, 8, 0};
    return mgmem_pressure_create_v2(d, life, &options, out);
}
mgmem_status destroy_controller(unsigned mode, mgmem_pressure_controller *control) {
    auto status = mode == 1 ? mgmem_pressure_begin_close_v1(control) : mgmem_pressure_begin_close_v2(control);
    if (status != MGMEM_OK) return status;
    status = mode == 1 ? mgmem_pressure_drain_v1(control, 3000) : mgmem_pressure_drain_v2(control, 3000);
    if (status != MGMEM_OK) return status;
    return mode == 1 ? mgmem_pressure_destroy_v1(control) : mgmem_pressure_destroy_v2(control);
}
void apply(unsigned mode, mgmem_pressure_controller *control, uint32_t level) {
    if (mode == 1) {
        check(mgmem_pressure_notify_v1(control, level) == MGMEM_OK, "v1 notify");
        return;
    }
    uint64_t ticket = 0;
    mgmem_status status;
    const auto deadline = Clock::now() + std::chrono::seconds(3);
    do {
        status = mgmem_pressure_submit_v2(control, level, &ticket);
        if (status == MGMEM_BUSY) std::this_thread::yield();
    } while (status == MGMEM_BUSY && Clock::now() < deadline);
    check(status == MGMEM_OK && ticket != 0, "v2 submit");
    for (;;) {
        mgmem_pressure_event_result_v2 result{};
        check(mgmem_pressure_event_result_v2_get(control, ticket, &result, sizeof(result)) == MGMEM_OK, "v2 result");
        if (result.state != MGMEM_PRESSURE_EVENT_QUEUED) {
            check(result.state == MGMEM_PRESSURE_EVENT_APPLIED && result.status == MGMEM_OK, "v2 applied");
            return;
        }
        check(Clock::now() < deadline, "v2 result timeout");
        std::this_thread::yield();
    }
}
uint64_t budget(mgmem_domain *d) {
    mgmem_snapshot_v1 s{};
    check(mgmem_domain_snapshot_v1(d, &s, sizeof(s)) == MGMEM_OK, "domain snapshot");
    return s.budget_bytes;
}

// Pause B immediately after its actual core snapshot. In the old code A can
// pass through an entire controller lifetime before B claims attachment.
bool attach_race(unsigned mode_b, unsigned mode_a) {
    auto *d = make_domain();
    Lifetime owner;
    auto life = owner.table();
    { std::lock_guard<std::mutex> lock(gate.mutex); gate.entered = gate.resume = gate.timed_out = false; }
    mgmem_pressure_controller *b = nullptr;
    mgmem_status status_b = MGMEM_PROVIDER_ERROR;
    std::thread creator([&] {
        gate_next_snapshot = true;
        status_b = create(mode_b, d, &life, &b);
    });
    {
        std::unique_lock<std::mutex> lock(gate.mutex);
        check(gate.changed.wait_for(lock, std::chrono::seconds(3), [] { return gate.entered; }), "snapshot gate entry");
    }
    mgmem_pressure_controller *a = nullptr;
    const auto status_a = create(mode_a, d, &life, &a);
    const bool overtook = status_a == MGMEM_OK;
    if (overtook) {
        check(a, "overtaking controller");
        apply(mode_a, a, MGMEM_PRESSURE_CRITICAL);
        check(budget(d) == 0, "critical budget applied");
        check(destroy_controller(mode_a, a) == MGMEM_OK, "overtaking controller close");
    } else check(status_a == MGMEM_BUSY && !a, "conflicting creation must be BUSY");
    { std::lock_guard<std::mutex> lock(gate.mutex); gate.resume = true; gate.changed.notify_all(); }
    creator.join();
    check(!gate.timed_out, "snapshot gate timeout");
    check(status_b == MGMEM_OK && b, "paused controller creation");
    const auto before = budget(d);
    apply(mode_b, b, MGMEM_PRESSURE_NORMAL);
    const auto after = budget(d);
    check(destroy_controller(mode_b, b) == MGMEM_OK, "paused controller close");
    if (!overtook) {
        check(before == 100 && after == 100, "reserved snapshot budget");
        check(create(mode_a, d, &life, &a) == MGMEM_OK && a, "retry after detach");
        apply(mode_a, a, MGMEM_PRESSURE_CRITICAL);
        check(budget(d) == 0, "retry critical budget");
        check(destroy_controller(mode_a, a) == MGMEM_OK, "retry controller close");
    } else check(before == 0 && after == 100, "stale snapshot counterexample");
    check(owner.retains == owner.releases, "owner lifetime balanced");
    destroy_domain(d);
    std::printf("attach b=v%u a=v%u concurrent=%s", mode_b, mode_a, overtook ? "UNSAFE_SUCCESS" : "BUSY");
    if (overtook) std::printf(" stale_budget=%llu->%llu", static_cast<unsigned long long>(before), static_cast<unsigned long long>(after));
    std::printf("\n");
    return !overtook;
}

void snapshot_failure_releases_slot(unsigned mode) {
    auto *d = make_domain();
    Lifetime owner;
    auto life = owner.table();
    for (unsigned i = 0; i < 40; ++i) {
        mgmem_pressure_controller *control = nullptr;
        fail_snapshot_for = d;
        check(create(mode, d, &life, &control) == MGMEM_PROVIDER_ERROR && !control, "snapshot failure propagated");
        check(fail_snapshot_for == nullptr, "injected snapshot failure consumed");
    }
    mgmem_pressure_controller *control = nullptr;
    check(create(mode, d, &life, &control) == MGMEM_OK && control, "attachment reusable after failures");
    check(destroy_controller(mode, control) == MGMEM_OK, "recovered controller close");
    check(owner.retains == owner.releases && owner.retains == 41, "failed create releases owner pins");
    destroy_domain(d);
}

// The documented callbacks are no-throw. These intentionally violating
// fixtures verify that exceptions do not escape the C ABI or cause a retry of
// an owner callback whose side effects could already have happened.
struct ThrowingLifetime {
    bool fail_retain = false, fail_release = false;
    unsigned retain_calls = 0, release_calls = 0;
    static void retain(void *p) {
        auto &self = *static_cast<ThrowingLifetime *>(p);
        ++self.retain_calls;
        if (self.fail_retain) throw std::bad_alloc();
    }
    static void release(void *p) {
        auto &self = *static_cast<ThrowingLifetime *>(p);
        ++self.release_calls;
        if (self.fail_release) throw std::bad_alloc();
    }
    mgmem_domain_lifetime_v1 table() {
        return {sizeof(mgmem_domain_lifetime_v1), 1, this, retain, release};
    }
};
void callback_exceptions_do_not_escape(unsigned mode) {
    auto *d = make_domain();
    ThrowingLifetime broken;
    auto life = broken.table();
    mgmem_pressure_controller *control = nullptr;
    broken.fail_retain = true;
    check(create(mode, d, &life, &control) == MGMEM_PROVIDER_ERROR && !control,
          "throwing retain is unknown provider error");
    check(broken.retain_calls == 1 && broken.release_calls == 0, "unknown retain is not retried or released");

    broken.fail_retain = false;
    broken.fail_release = true;
    fail_snapshot_for = d;
    check(create(mode, d, &life, &control) == MGMEM_PROVIDER_ERROR && !control,
          "throwing release stays inside C ABI");
    check(broken.retain_calls == 2 && broken.release_calls == 1, "throwing release is not retried");

    Lifetime good;
    auto safe = good.table();
    check(create(mode, d, &safe, &control) == MGMEM_OK && control, "attachment slot released after callback error");
    check(destroy_controller(mode, control) == MGMEM_OK, "valid controller close after callback error");
    check(good.retains == good.releases, "valid lifetime balanced");
    destroy_domain(d);
}

struct ReentrantLifetime {
    mgmem_domain *other = nullptr;
    unsigned nested_mode = 1;
    Lifetime nested;
    std::atomic<unsigned> retain_calls{0}, release_calls{0}, failures{0};
    void use_other_domain() noexcept {
        auto life = nested.table();
        mgmem_pressure_controller *control = nullptr;
        const auto status = create(nested_mode, other, &life, &control);
        if (status != MGMEM_OK || !control) { ++failures; return; }
        if (destroy_controller(nested_mode, control) != MGMEM_OK) ++failures;
    }
    static void retain(void *p) noexcept {
        auto &self = *static_cast<ReentrantLifetime *>(p);
        ++self.retain_calls;
        self.use_other_domain();
    }
    static void release(void *p) noexcept {
        auto &self = *static_cast<ReentrantLifetime *>(p);
        ++self.release_calls;
        self.use_other_domain();
    }
    mgmem_domain_lifetime_v1 table() noexcept {
        return {sizeof(mgmem_domain_lifetime_v1), 1, this, retain, release};
    }
};
void callbacks_do_not_hold_attachment_lock(unsigned outer_mode, unsigned nested_mode) {
    auto *outer = make_domain();
    auto *other = make_domain();
    ReentrantLifetime owner; owner.other = other; owner.nested_mode = nested_mode;
    auto life = owner.table();
    mgmem_pressure_controller *control = nullptr;
    check(create(outer_mode, outer, &life, &control) == MGMEM_OK && control, "outer create with nested retain");
    check(destroy_controller(outer_mode, control) == MGMEM_OK, "outer close with nested release");
    check(owner.retain_calls == 1 && owner.release_calls == 1 && owner.failures == 0, "nested callback operations");
    check(owner.nested.retains == 2 && owner.nested.releases == 2, "nested lifetime balanced");
    destroy_domain(outer);
    destroy_domain(other);
}
} // namespace

extern "C" mgmem_status pressure_attach_test_snapshot(mgmem_domain *domain, mgmem_snapshot_v1 *out, uint32_t size) {
    if (fail_snapshot_for == domain) { fail_snapshot_for = nullptr; return MGMEM_PROVIDER_ERROR; }
    const auto status = mgmem_domain_snapshot_v1(domain, out, size);
    if (gate_next_snapshot) {
        gate_next_snapshot = false;
        std::unique_lock<std::mutex> lock(gate.mutex);
        gate.entered = true;
        gate.changed.notify_all();
        if (!gate.changed.wait_for(lock, std::chrono::seconds(5), [] { return gate.resume; })) gate.timed_out = true;
    }
    return status;
}

int main(int argc, char **argv) {
    const bool race_only = argc == 2 && std::strcmp(argv[1], "--race-only") == 0;
    check(argc == 1 || race_only, "unknown test argument");
    unsigned violations = 0;
    for (unsigned b : {1u, 2u}) for (unsigned a : {1u, 2u})
        if (!attach_race(b, a)) ++violations;
    if (!race_only) {
        for (unsigned mode : {1u, 2u}) snapshot_failure_releases_slot(mode);
        for (unsigned mode : {1u, 2u}) callback_exceptions_do_not_escape(mode);
        for (unsigned outer : {1u, 2u}) for (unsigned nested : {1u, 2u})
            callbacks_do_not_hold_attachment_lock(outer, nested);
    }
    if (violations) {
        std::fprintf(stderr, "FAIL stale attach snapshot in %u mode combinations\n", violations);
        return 1;
    }
    std::printf("PASS pressure attach snapshot races, failure rollback and callback lock contracts\n");
    return 0;
}
