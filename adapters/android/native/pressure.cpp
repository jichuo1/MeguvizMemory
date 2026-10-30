#include <mgmem/pressure.h>
#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <limits>
#include <memory>
#include <mutex>
#include <new>
#include <set>
#include <thread>

namespace {
std::mutex attachments_mutex;
std::set<mgmem_domain *> attachments;
void detach(mgmem_domain *domain) {
    std::lock_guard<std::mutex> lock(attachments_mutex);
    attachments.erase(domain);
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
};
namespace {
void worker(mgmem_pressure_controller *control) noexcept {
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
    bool retained = false, attached = false;
    try {
        auto result = std::make_unique<mgmem_pressure_controller>();
        result->domain = domain; result->lifetime = *lifetime; result->options = *options;
        lifetime->retain(lifetime->context); retained = true;
        const auto status = mgmem_domain_snapshot_v1(domain, &result->cached, sizeof(result->cached));
        if (status != MGMEM_OK || result->cached.state != MGMEM_OPEN ||
            result->cached.budget_bytes != options->normal_budget_bytes) {
            retained = false; lifetime->release(lifetime->context);
            return status != MGMEM_OK ? status : result->cached.state != MGMEM_OPEN ? MGMEM_IS_CLOSED : MGMEM_INVALID_ARGUMENT;
        }
        bool unique = false;
        {
            std::lock_guard<std::mutex> lock(attachments_mutex);
            unique = attachments.emplace(domain).second;
            attached = unique;
        }
        if (!unique) {retained = false; lifetime->release(lifetime->context); return MGMEM_BUSY;}
        result->worker = std::thread(worker, result.get());
        *out = result.release();
        return MGMEM_OK;
    } catch (const std::bad_alloc &) {
        if (attached) detach(domain);
        if (retained) {try {lifetime->release(lifetime->context);} catch (...) {return MGMEM_PROVIDER_ERROR;}}
        return MGMEM_OUT_OF_MEMORY;
    } catch (...) {
        if (attached) detach(domain);
        if (retained) {try {lifetime->release(lifetime->context);} catch (...) {}}
        return MGMEM_PROVIDER_ERROR;
    }
}
mgmem_status mgmem_pressure_notify_v1(mgmem_pressure_controller *control, uint32_t pressure) {
    if (!control || pressure > MGMEM_PRESSURE_CRITICAL) return MGMEM_INVALID_ARGUMENT;
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
    try {
        std::lock_guard<std::mutex> lock(control->mutex);
        if (control->state == MGMEM_OPEN) control->state = MGMEM_CLOSING;
        control->changed.notify_all();
        return MGMEM_OK;
    } catch (...) {return MGMEM_PROVIDER_ERROR;}
}
mgmem_status mgmem_pressure_drain_v1(mgmem_pressure_controller *control, uint64_t timeout_ms) {
    if (!control) return MGMEM_INVALID_ARGUMENT;
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
}
