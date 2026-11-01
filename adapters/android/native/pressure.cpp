#include <mgmem/pressure.h>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <limits>
#include <memory>
#include <mutex>
#include <new>
#include <set>
#include <thread>
#include <vector>

namespace {
std::mutex attachments_mutex;
std::set<mgmem_domain *> attachments;
std::atomic<uint32_t> v2_workers{0};
#if defined(MGMEM_MANAGED_TESTING)
struct ManagedPressureTestGate {
    std::mutex mutex;
    std::condition_variable changed;
    bool armed = false, entered = false, close_seen = false, released = false, timed_out = false;
};
ManagedPressureTestGate managed_pressure_test_gate;
void pause_managed_pressure_worker() {
    auto &gate = managed_pressure_test_gate;
    std::unique_lock<std::mutex> lock(gate.mutex);
    if (!gate.armed) return;
    gate.entered = true;
    gate.changed.notify_all();
    if (!gate.changed.wait_for(lock, std::chrono::seconds(10), [&] { return gate.released; })) gate.timed_out = true;
    gate.armed = false;
}
void observe_managed_pressure_close() {
    auto &gate = managed_pressure_test_gate;
    std::lock_guard<std::mutex> lock(gate.mutex);
    if (gate.armed && gate.entered) { gate.close_seen = true; gate.changed.notify_all(); }
}
#endif
bool reserve_v2_worker() noexcept {
    auto n = v2_workers.load(std::memory_order_relaxed);
    while (n < MGMEM_PRESSURE_V2_MAX_WORKERS) {
        if (v2_workers.compare_exchange_weak(n, n + 1, std::memory_order_acq_rel)) return true;
    }
    return false;
}
void detach(mgmem_domain *domain) {
    std::lock_guard<std::mutex> lock(attachments_mutex);
    attachments.erase(domain);
}
// Roll back a failed create without invoking an owner callback under the
// attachment lock. If detachment itself fails, keep the owner pin rather than
// leave a registry entry pointing at a potentially destroyed domain.
mgmem_status rollback_create(mgmem_domain *domain, const mgmem_domain_lifetime_v1 *lifetime,
    bool attached, bool retained, mgmem_status result) noexcept {
    try {
        if (attached) detach(domain);
        if (retained) lifetime->release(lifetime->context);
        return result;
    } catch (...) { return MGMEM_PROVIDER_ERROR; }
}
}
struct mgmem_pressure_controller {
    std::mutex mutex;
    std::condition_variable changed;
    mgmem_domain *domain = nullptr;
    mgmem_domain_lifetime_v1 lifetime{};
    mgmem_pressure_options_v1 options{};
    std::thread worker;
    uint32_t state = MGMEM_OPEN, level = MGMEM_PRESSURE_NORMAL;
    mgmem_status last = MGMEM_OK;
    bool pending = false, accessible = true;
    uint64_t sequence = 0, attempts = 0, successes = 0, deferred = 0;
    mgmem_snapshot_v1 cached{};
    const uint32_t mode;
    struct Event { uint64_t ticket = 0; uint32_t level = 0; };
    mgmem_pressure_options_v2 options2{};
    std::vector<Event> queue;
    std::vector<mgmem_pressure_event_result_v2> results;
    size_t head = 0, count = 0, result_next = 0;
    uint64_t manual = 0, effective = 0, applied_sequence = 0, domain_id = 0;
    uint64_t applied_events = 0, failed_events = 0, cancelled_events = 0, trim_failures = 0, results_evicted = 0;
    mgmem_status close_status = MGMEM_OK;
    std::atomic<uint64_t> busy_events{0}, critical_busy_events{0};
    std::atomic<bool> saturated{false};
    explicit mgmem_pressure_controller(uint32_t mode_value) : mode(mode_value) {}
};
namespace {
void increment(uint64_t &value, mgmem_pressure_controller *control) noexcept {
    if (value == std::numeric_limits<uint64_t>::max()) control->saturated.store(true);
    else ++value;
}
void increment_atomic(std::atomic<uint64_t> &counter, mgmem_pressure_controller *control) noexcept {
    auto n = counter.load(std::memory_order_relaxed);
    for (unsigned i = 0; i < 8; ++i) {
        if (n == std::numeric_limits<uint64_t>::max()) { control->saturated.store(true); return; }
        if (counter.compare_exchange_weak(n, n + 1, std::memory_order_relaxed)) return;
    }
    // Do not spin indefinitely on the submit path. Counters are explicitly
    // marked inexact if extreme contention prevents this bounded update.
    control->saturated.store(true, std::memory_order_relaxed);
}
uint64_t effective_budget(const mgmem_pressure_controller *control, uint64_t manual, uint32_t level) noexcept {
    return level == MGMEM_PRESSURE_NORMAL ? manual : std::min(manual,
        level == MGMEM_PRESSURE_MODERATE ? control->options2.moderate_budget_bytes : control->options2.critical_budget_bytes);
}
void record_event(mgmem_pressure_controller *control, uint64_t ticket, uint32_t state,
    mgmem_status status, uint64_t apply_sequence) noexcept {
    auto &result = control->results[control->result_next];
    if (result.ticket) increment(control->results_evicted, control);
    result = {sizeof(result), 2, state, status, ticket, apply_sequence};
    control->result_next = (control->result_next + 1) % control->results.size();
}
void legacy_worker(mgmem_pressure_controller *control) noexcept {
    std::unique_lock<std::mutex> lock(control->mutex);
    while (control->state == MGMEM_OPEN) {
        control->changed.wait(lock, [&] {return control->pending || control->state != MGMEM_OPEN;});
        if (control->state != MGMEM_OPEN) break;
        const uint64_t observed = control->sequence;
        ++control->attempts;
        lock.unlock();
        const auto status = mgmem_domain_trim_v1(control->domain);
        lock.lock();
        control->last = status;
        if (status == MGMEM_OK) ++control->successes;
        if (status == MGMEM_BUSY) ++control->deferred;
        if (status != MGMEM_BUSY && observed == control->sequence) control->pending = false;
        if (status == MGMEM_BUSY) {
            control->changed.wait_for(lock, std::chrono::milliseconds(control->options.deferred_poll_ms),
                [&] {return control->state != MGMEM_OPEN || observed != control->sequence;});
        }
    }
    // Stop dereferencing domain after this point; preserve an explicit cached
    // observation. Owner release happens only after join in destroy, never on
    // this worker where an owning wrapper could accidentally join itself.
    const auto snapshot_status = mgmem_domain_snapshot_v1(control->domain, &control->cached, sizeof(control->cached));
    if (snapshot_status != MGMEM_OK) control->last = snapshot_status;
    control->accessible = false;
    control->pending = false;
    control->state = MGMEM_CLOSED;
    control->changed.notify_all();
}
void v2_worker(mgmem_pressure_controller *control) noexcept {
    std::unique_lock<std::mutex> lock(control->mutex);
    while (control->state == MGMEM_OPEN) {
        control->changed.wait(lock, [&] { return control->count || control->pending || control->state != MGMEM_OPEN; });
        if (control->state != MGMEM_OPEN) break;
        if (control->count) {
#if defined(MGMEM_MANAGED_TESTING)
            lock.unlock();
            pause_managed_pressure_worker();
            lock.lock();
            if (control->state != MGMEM_OPEN) break;
            if (!control->count) continue;
#endif
            const auto event = control->queue[control->head];
            control->head = (control->head + 1) % control->queue.size();
            --control->count;
            const auto target = effective_budget(control, control->manual, event.level);
            mgmem_status status = control->applied_sequence == std::numeric_limits<uint64_t>::max()
                ? MGMEM_OVERFLOW : mgmem_domain_set_budget_v1(control->domain, target);
            if (status == MGMEM_OK) {
                ++control->applied_sequence;
                control->level = event.level;
                control->effective = target;
                if (event.level != MGMEM_PRESSURE_NORMAL) control->pending = true;
                increment(control->applied_events, control);
                record_event(control, event.ticket, MGMEM_PRESSURE_EVENT_APPLIED, status, control->applied_sequence);
            } else {
                increment(control->failed_events, control);
                record_event(control, event.ticket, MGMEM_PRESSURE_EVENT_FAILED, status, 0);
            }
            control->changed.notify_all();
            continue;
        }
        const auto observed = control->applied_sequence;
        increment(control->attempts, control);
        lock.unlock();
        const auto status = mgmem_domain_trim_v1(control->domain);
        lock.lock();
        control->last = status;
        if (status == MGMEM_OK) increment(control->successes, control);
        else if (status == MGMEM_BUSY) increment(control->deferred, control);
        else increment(control->trim_failures, control);
        if (status != MGMEM_BUSY && observed == control->applied_sequence) control->pending = false;
        if (status == MGMEM_BUSY && observed == control->applied_sequence && !control->count) {
            control->changed.wait_for(lock, std::chrono::milliseconds(control->options2.deferred_poll_ms), [&] {
                return control->state != MGMEM_OPEN || control->count || observed != control->applied_sequence;
            });
        }
    }
    while (control->count) {
        const auto event = control->queue[control->head];
        control->head = (control->head + 1) % control->queue.size();
        --control->count;
        increment(control->cancelled_events, control);
        record_event(control, event.ticket, MGMEM_PRESSURE_EVENT_CANCELLED, MGMEM_IS_CLOSED, 0);
    }
    control->close_status = mgmem_domain_snapshot_v1(control->domain, &control->cached, sizeof(control->cached));
    if (control->close_status == MGMEM_OK &&
        (control->cached.struct_size < sizeof(control->cached) || control->cached.abi_major != MGMEM_ABI_MAJOR ||
         control->cached.state != MGMEM_OPEN || control->cached.domain_id != control->domain_id ||
         control->cached.budget_bytes != control->effective)) control->close_status = MGMEM_PROVIDER_ERROR;
    control->accessible = false;
    control->pending = false;
    control->state = MGMEM_CLOSED;
    control->changed.notify_all();
}
void worker(mgmem_pressure_controller *control) noexcept {
    if (control->mode == 2) v2_worker(control);
    else legacy_worker(control);
}
}
extern "C" {
mgmem_status mgmem_pressure_create_v1(mgmem_domain *domain, const mgmem_domain_lifetime_v1 *lifetime,
    const mgmem_pressure_options_v1 *options, mgmem_pressure_controller **out) {
    if (!out) return MGMEM_INVALID_ARGUMENT;
    *out = nullptr;
    if (!domain || !lifetime || !options || lifetime->struct_size < sizeof(*lifetime) ||
        options->struct_size < sizeof(*options)) return MGMEM_INVALID_ARGUMENT;
    if (lifetime->version != 1 || options->version != 1 || options->required_flags) return MGMEM_UNSUPPORTED;
    if (!lifetime->retain || !lifetime->release || options->normal_budget_bytes == 0 ||
        options->critical_budget_bytes > options->moderate_budget_bytes ||
        options->moderate_budget_bytes > options->normal_budget_bytes ||
        options->deferred_poll_ms == 0 || options->deferred_poll_ms > 60000) return MGMEM_INVALID_ARGUMENT;
    bool retained = false, attached = false, retain_entered = false;
    try {
        auto result = std::make_unique<mgmem_pressure_controller>(1);
        result->domain = domain; result->lifetime = *lifetime; result->options = *options;
        retain_entered = true;
        lifetime->retain(lifetime->context); retained = true;
        {
            std::lock_guard<std::mutex> lock(attachments_mutex);
            attached = attachments.emplace(domain).second;
        }
        if (!attached) return rollback_create(domain, lifetime, false, retained, MGMEM_BUSY);
        const auto status = mgmem_domain_snapshot_v1(domain, &result->cached, sizeof(result->cached));
        if (status != MGMEM_OK || result->cached.state != MGMEM_OPEN ||
            result->cached.budget_bytes != options->normal_budget_bytes) {
            const auto failure = status != MGMEM_OK ? status :
                result->cached.state != MGMEM_OPEN ? MGMEM_IS_CLOSED : MGMEM_INVALID_ARGUMENT;
            return rollback_create(domain, lifetime, attached, retained, failure);
        }
        result->worker = std::thread(worker, result.get());
        *out = result.release();
        return MGMEM_OK;
    } catch (const std::bad_alloc &) {
        return rollback_create(domain, lifetime, attached, retained,
            retain_entered && !retained ? MGMEM_PROVIDER_ERROR : MGMEM_OUT_OF_MEMORY);
    } catch (...) {
        return rollback_create(domain, lifetime, attached, retained, MGMEM_PROVIDER_ERROR);
    }
}
mgmem_status mgmem_pressure_notify_v1(mgmem_pressure_controller *control, uint32_t pressure) {
    if (!control || pressure > MGMEM_PRESSURE_CRITICAL) return MGMEM_INVALID_ARGUMENT;
    if (control->mode != 1) return MGMEM_UNSUPPORTED;
    try {
        std::lock_guard<std::mutex> lock(control->mutex);
        if (control->state != MGMEM_OPEN) return MGMEM_IS_CLOSED;
        if (control->sequence == std::numeric_limits<uint64_t>::max()) return MGMEM_OVERFLOW;
        const uint64_t budgets[] = {control->options.normal_budget_bytes,control->options.moderate_budget_bytes,
                                   control->options.critical_budget_bytes};
        const auto status = mgmem_domain_set_budget_v1(control->domain, budgets[pressure]);
        if (status != MGMEM_OK) {control->last = status;return status;}
        control->level = pressure;
        ++control->sequence;
        if (pressure != MGMEM_PRESSURE_NORMAL) control->pending = true;
        control->changed.notify_all();
        return MGMEM_OK;
    } catch (...) {return MGMEM_PROVIDER_ERROR;}
}
mgmem_status mgmem_pressure_snapshot_v1_get(mgmem_pressure_controller *control,
    mgmem_pressure_snapshot_v1 *out, uint32_t out_size) {
    if (!control || !out || out_size < sizeof(*out)) return MGMEM_INVALID_ARGUMENT;
    if (control->mode != 1) return MGMEM_UNSUPPORTED;
    try {
        std::lock_guard<std::mutex> lock(control->mutex);
        mgmem_pressure_snapshot_v1 value{};
        value.struct_size = sizeof(value); value.version = 1; value.state = control->state;
        value.last_pressure = control->level; value.last_status = control->last;
        value.trim_pending = control->pending ? 1u : 0u;
        value.events_accepted = control->sequence; value.trim_attempts = control->attempts;
        value.trim_successes = control->successes; value.deferred_busy = control->deferred;
        value.domain_observation = control->accessible ? MGMEM_PRESSURE_OBSERVATION_LIVE : MGMEM_PRESSURE_OBSERVATION_LAST_BEFORE_RELEASE;
        if (control->accessible) {
            const auto status = mgmem_domain_snapshot_v1(control->domain, &value.domain, sizeof(value.domain));
            if (status != MGMEM_OK) return status;
        } else value.domain = control->cached;
        if (value.domain.backing_bytes > std::numeric_limits<uint64_t>::max()-value.domain.reserved_bytes) return MGMEM_OVERFLOW;
        const uint64_t charged = value.domain.backing_bytes+value.domain.reserved_bytes;
        value.over_budget_bytes = charged > value.domain.budget_bytes ? charged-value.domain.budget_bytes : 0;
        *out = value;
        return MGMEM_OK;
    } catch (...) {return MGMEM_PROVIDER_ERROR;}
}
mgmem_status mgmem_pressure_begin_close_v1(mgmem_pressure_controller *control) {
    if (!control) return MGMEM_INVALID_ARGUMENT;
    if (control->mode != 1) return MGMEM_UNSUPPORTED;
    try {
        std::lock_guard<std::mutex> lock(control->mutex);
        if (control->state == MGMEM_OPEN) control->state = MGMEM_CLOSING;
        control->changed.notify_all();
        return MGMEM_OK;
    } catch (...) {return MGMEM_PROVIDER_ERROR;}
}
mgmem_status mgmem_pressure_drain_v1(mgmem_pressure_controller *control, uint64_t timeout_ms) {
    if (!control) return MGMEM_INVALID_ARGUMENT;
    if (control->mode != 1) return MGMEM_UNSUPPORTED;
    using milliseconds = std::chrono::milliseconds;
    const auto maximum = std::chrono::duration_cast<milliseconds>(std::chrono::steady_clock::duration::max()/4).count();
    if (timeout_ms > static_cast<uint64_t>(maximum)) return MGMEM_OVERFLOW;
    try {
        std::unique_lock<std::mutex> lock(control->mutex);
        if (control->state == MGMEM_OPEN) return MGMEM_INVALID_ARGUMENT;
        if (control->state != MGMEM_CLOSED && (timeout_ms == 0 || !control->changed.wait_for(lock,milliseconds(timeout_ms),
            [&] {return control->state == MGMEM_CLOSED;}))) return MGMEM_TIMEOUT;
        return control->last == MGMEM_PROVIDER_ERROR ? MGMEM_PROVIDER_ERROR : MGMEM_OK;
    } catch (...) {return MGMEM_PROVIDER_ERROR;}
}
mgmem_status mgmem_pressure_destroy_v1(mgmem_pressure_controller *control) {
    if (!control) return MGMEM_INVALID_ARGUMENT;
    if (control->mode != 1) return MGMEM_UNSUPPORTED;
    try {
        {std::lock_guard<std::mutex> lock(control->mutex); if (control->state != MGMEM_CLOSED) return MGMEM_BUSY;}
        if (control->worker.joinable()) control->worker.join();
        const auto lifetime = control->lifetime;
        detach(control->domain);
        delete control;
        try { lifetime.release(lifetime.context); } catch (...) {return MGMEM_PROVIDER_ERROR;}
        return MGMEM_OK;
    } catch (...) {return MGMEM_PROVIDER_ERROR;}
}
mgmem_status mgmem_pressure_create_v2(mgmem_domain *domain, const mgmem_domain_lifetime_v1 *lifetime,
    const mgmem_pressure_options_v2 *options, mgmem_pressure_controller **out) {
    if (!out) return MGMEM_INVALID_ARGUMENT;
    *out = nullptr;
    if (!domain || !lifetime || !options || lifetime->struct_size < sizeof(*lifetime) ||
        options->struct_size < sizeof(*options)) return MGMEM_INVALID_ARGUMENT;
    if (lifetime->version != 1 || options->version != 2 || options->required_flags) return MGMEM_UNSUPPORTED;
    if (!lifetime->retain || !lifetime->release || options->critical_budget_bytes > options->moderate_budget_bytes ||
        options->deferred_poll_ms == 0 || options->deferred_poll_ms > 60000 || options->queue_capacity == 0 ||
        options->queue_capacity > MGMEM_PRESSURE_V2_MAX_QUEUE || options->result_capacity == 0 ||
        options->result_capacity > MGMEM_PRESSURE_V2_MAX_RESULTS) return MGMEM_INVALID_ARGUMENT;
    if (!reserve_v2_worker()) return MGMEM_BUSY;
    bool retained = false, attached = false, retain_entered = false;
    try {
        auto result = std::make_unique<mgmem_pressure_controller>(2);
        result->domain = domain; result->lifetime = *lifetime; result->options2 = *options;
        result->queue.resize(options->queue_capacity);
        result->results.resize(options->result_capacity);
        retain_entered = true;
        lifetime->retain(lifetime->context); retained = true;
        {
            std::lock_guard<std::mutex> lock(attachments_mutex);
            attached = attachments.emplace(domain).second;
        }
        if (!attached) {
            const auto failure = rollback_create(domain, lifetime, false, retained, MGMEM_BUSY);
            v2_workers.fetch_sub(1, std::memory_order_release);
            return failure;
        }
        auto status = mgmem_domain_snapshot_v1(domain, &result->cached, sizeof(result->cached));
        if (status == MGMEM_OK && (result->cached.struct_size < sizeof(result->cached) ||
            result->cached.abi_major != MGMEM_ABI_MAJOR || result->cached.domain_id == 0)) status = MGMEM_PROVIDER_ERROR;
        if (status != MGMEM_OK || result->cached.state != MGMEM_OPEN) {
            const auto failure = rollback_create(domain, lifetime, attached, retained,
                status != MGMEM_OK ? status : MGMEM_IS_CLOSED);
            v2_workers.fetch_sub(1, std::memory_order_release);
            return failure;
        }
        result->manual = result->effective = result->cached.budget_bytes;
        result->domain_id = result->cached.domain_id;
        result->worker = std::thread(worker, result.get());
        *out = result.release();
        return MGMEM_OK;
    } catch (const std::bad_alloc &) {
        const auto failure = rollback_create(domain, lifetime, attached, retained,
            retain_entered && !retained ? MGMEM_PROVIDER_ERROR : MGMEM_OUT_OF_MEMORY);
        v2_workers.fetch_sub(1, std::memory_order_release);
        return failure;
    } catch (...) {
        const auto failure = rollback_create(domain, lifetime, attached, retained, MGMEM_PROVIDER_ERROR);
        v2_workers.fetch_sub(1, std::memory_order_release);
        return failure;
    }
}
mgmem_status mgmem_pressure_submit_v2(mgmem_pressure_controller *control, uint32_t pressure, uint64_t *ticket) {
    if (!ticket) return MGMEM_INVALID_ARGUMENT;
    *ticket = 0;
    if (!control || pressure > MGMEM_PRESSURE_CRITICAL) return MGMEM_INVALID_ARGUMENT;
    if (control->mode != 2) return MGMEM_UNSUPPORTED;
    try {
        std::unique_lock<std::mutex> lock(control->mutex, std::try_to_lock);
        if (!lock.owns_lock()) {
            increment_atomic(control->busy_events, control);
            if (pressure == MGMEM_PRESSURE_CRITICAL) increment_atomic(control->critical_busy_events, control);
            return MGMEM_BUSY;
        }
        if (control->state != MGMEM_OPEN) return MGMEM_IS_CLOSED;
        if (control->count == control->queue.size()) {
            increment_atomic(control->busy_events, control);
            if (pressure == MGMEM_PRESSURE_CRITICAL) increment_atomic(control->critical_busy_events, control);
            return MGMEM_BUSY;
        }
        if (control->sequence == std::numeric_limits<uint64_t>::max()) return MGMEM_OVERFLOW;
        const auto id = ++control->sequence;
        control->queue[(control->head + control->count) % control->queue.size()] = {id, pressure};
        ++control->count;
        *ticket = id;
        control->changed.notify_all();
        return MGMEM_OK;
    } catch (...) { return MGMEM_PROVIDER_ERROR; }
}
mgmem_status mgmem_pressure_set_manual_budget_v2(mgmem_pressure_controller *control, uint64_t bytes,
    uint64_t *apply_sequence) {
    if (!apply_sequence) return MGMEM_INVALID_ARGUMENT;
    *apply_sequence = 0;
    if (!control) return MGMEM_INVALID_ARGUMENT;
    if (control->mode != 2) return MGMEM_UNSUPPORTED;
    try {
        std::lock_guard<std::mutex> lock(control->mutex);
        if (control->state != MGMEM_OPEN) return MGMEM_IS_CLOSED;
        if (control->applied_sequence == std::numeric_limits<uint64_t>::max()) return MGMEM_OVERFLOW;
        const auto target = effective_budget(control, bytes, control->level);
        const auto status = mgmem_domain_set_budget_v1(control->domain, target);
        if (status != MGMEM_OK) return status;
        control->manual = bytes; control->effective = target;
        *apply_sequence = ++control->applied_sequence;
        if (control->level != MGMEM_PRESSURE_NORMAL) control->pending = true;
        control->changed.notify_all();
        return MGMEM_OK;
    } catch (...) { return MGMEM_PROVIDER_ERROR; }
}
mgmem_status mgmem_pressure_event_result_v2_get(mgmem_pressure_controller *control, uint64_t ticket,
    mgmem_pressure_event_result_v2 *out, uint32_t out_size) {
    if (!control || !out || out_size < sizeof(*out) || ticket == 0) return MGMEM_INVALID_ARGUMENT;
    if (control->mode != 2) return MGMEM_UNSUPPORTED;
    try {
        std::lock_guard<std::mutex> lock(control->mutex);
        if (ticket > control->sequence) return MGMEM_INVALID_ARGUMENT;
        for (size_t i = 0; i < control->count; ++i) {
            if (control->queue[(control->head + i) % control->queue.size()].ticket == ticket) {
                *out = {sizeof(*out), 2, MGMEM_PRESSURE_EVENT_QUEUED, MGMEM_OK, ticket, 0}; return MGMEM_OK;
            }
        }
        for (const auto &result : control->results) if (result.ticket == ticket) { *out = result; return MGMEM_OK; }
        *out = {sizeof(*out), 2, MGMEM_PRESSURE_EVENT_EXPIRED, MGMEM_OK, ticket, 0};
        return MGMEM_OK;
    } catch (...) { return MGMEM_PROVIDER_ERROR; }
}
mgmem_status mgmem_pressure_snapshot_v2_get(mgmem_pressure_controller *control,
    mgmem_pressure_snapshot_v2 *out, uint32_t out_size) {
    if (!control || !out || out_size < sizeof(*out)) return MGMEM_INVALID_ARGUMENT;
    if (control->mode != 2) return MGMEM_UNSUPPORTED;
    try {
        std::lock_guard<std::mutex> lock(control->mutex);
        mgmem_pressure_snapshot_v2 value{};
        value.struct_size = sizeof(value); value.version = 2; value.state = control->state;
        value.applied_pressure = control->level; value.trim_pending = control->pending ? 1u : 0u;
        value.last_trim_status = control->last; value.queue_depth = static_cast<uint32_t>(control->count);
        value.counters_saturated = control->saturated.load() ? 1u : 0u;
        value.manual_budget_bytes = control->manual; value.effective_budget_bytes = control->effective;
        value.apply_sequence = control->applied_sequence; value.events_accepted = control->sequence;
        value.events_applied = control->applied_events; value.events_failed = control->failed_events;
        value.events_cancelled = control->cancelled_events; value.events_busy = control->busy_events.load();
        value.trim_attempts = control->attempts; value.trim_successes = control->successes;
        value.trim_busy = control->deferred; value.trim_failures = control->trim_failures;
        value.events_critical_busy = control->critical_busy_events.load(); value.results_evicted = control->results_evicted;
        *out = value; return MGMEM_OK;
    } catch (...) { return MGMEM_PROVIDER_ERROR; }
}
mgmem_status mgmem_pressure_begin_close_v2(mgmem_pressure_controller *control) {
    if (!control) return MGMEM_INVALID_ARGUMENT;
    if (control->mode != 2) return MGMEM_UNSUPPORTED;
    try {
        std::lock_guard<std::mutex> lock(control->mutex);
        if (control->state == MGMEM_OPEN) control->state = MGMEM_CLOSING;
#if defined(MGMEM_MANAGED_TESTING)
        observe_managed_pressure_close();
#endif
        control->changed.notify_all(); return MGMEM_OK;
    } catch (...) { return MGMEM_PROVIDER_ERROR; }
}
mgmem_status mgmem_pressure_drain_v2(mgmem_pressure_controller *control, uint64_t timeout_ms) {
    if (!control) return MGMEM_INVALID_ARGUMENT;
    if (control->mode != 2) return MGMEM_UNSUPPORTED;
    using milliseconds = std::chrono::milliseconds;
    const auto maximum = std::chrono::duration_cast<milliseconds>(std::chrono::steady_clock::duration::max()/4).count();
    if (timeout_ms > static_cast<uint64_t>(maximum)) return MGMEM_OVERFLOW;
    try {
        std::unique_lock<std::mutex> lock(control->mutex);
        if (control->state == MGMEM_OPEN) return MGMEM_INVALID_ARGUMENT;
        if (control->state != MGMEM_CLOSED && (timeout_ms == 0 || !control->changed.wait_for(lock, milliseconds(timeout_ms),
            [&] { return control->state == MGMEM_CLOSED; }))) return MGMEM_TIMEOUT;
        return control->close_status;
    } catch (...) { return MGMEM_PROVIDER_ERROR; }
}
mgmem_status mgmem_pressure_destroy_v2(mgmem_pressure_controller *control) {
    if (!control) return MGMEM_INVALID_ARGUMENT;
    if (control->mode != 2) return MGMEM_UNSUPPORTED;
    try {
        { std::lock_guard<std::mutex> lock(control->mutex);
          if (control->state != MGMEM_CLOSED) return MGMEM_BUSY;
          if (control->close_status != MGMEM_OK) return control->close_status; }
        if (control->worker.joinable()) control->worker.join();
        const auto lifetime = control->lifetime;
        detach(control->domain);
        delete control;
        v2_workers.fetch_sub(1, std::memory_order_release);
        try { lifetime.release(lifetime.context); } catch (...) { return MGMEM_PROVIDER_ERROR; }
        return MGMEM_OK;
    } catch (...) { return MGMEM_PROVIDER_ERROR; }
}
#if defined(MGMEM_MANAGED_PRESSURE_TESTING)
mgmem_status mgmem_pressure_test_set_sequences_v2(mgmem_pressure_controller *control,
    uint64_t ticket_sequence, uint64_t apply_sequence) {
    if (!control || control->mode != 2) return MGMEM_INVALID_ARGUMENT;
    std::lock_guard<std::mutex> lock(control->mutex);
    if (control->state != MGMEM_OPEN || control->count || control->pending) return MGMEM_BUSY;
    control->sequence = ticket_sequence;
    control->applied_sequence = apply_sequence;
    return MGMEM_OK;
}
#endif
#if defined(MGMEM_MANAGED_TESTING)
void mgmem_managed_test_pressure_gate_arm() {
    auto &gate = managed_pressure_test_gate;
    std::lock_guard<std::mutex> lock(gate.mutex);
    gate.armed = true; gate.entered = false; gate.close_seen = false; gate.released = false; gate.timed_out = false;
}
int mgmem_managed_test_pressure_gate_wait_entered() {
    auto &gate = managed_pressure_test_gate;
    std::unique_lock<std::mutex> lock(gate.mutex);
    return gate.changed.wait_for(lock, std::chrono::seconds(5), [&] { return gate.entered; }) ? 1 : 0;
}
int mgmem_managed_test_pressure_gate_wait_close() {
    auto &gate = managed_pressure_test_gate;
    std::unique_lock<std::mutex> lock(gate.mutex);
    return gate.changed.wait_for(lock, std::chrono::seconds(5), [&] { return gate.close_seen; }) ? 1 : 0;
}
int mgmem_managed_test_pressure_gate_release() {
    auto &gate = managed_pressure_test_gate;
    std::lock_guard<std::mutex> lock(gate.mutex);
    gate.released = true;
    gate.changed.notify_all();
    return gate.entered && !gate.timed_out ? 1 : 0;
}
#endif
}
