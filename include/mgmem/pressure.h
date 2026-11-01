#ifndef MGMEM_PRESSURE_H
#define MGMEM_PRESSURE_H
#include <mgmem/mgmem.h>
#if defined(_WIN32) && defined(MGMEM_PRESSURE_SHARED)
# if defined(MGMEM_PRESSURE_BUILDING)
#  define MGMEM_PRESSURE_API __declspec(dllexport)
# else
#  define MGMEM_PRESSURE_API __declspec(dllimport)
# endif
#elif defined(__GNUC__)
# define MGMEM_PRESSURE_API __attribute__((visibility("default")))
#else
# define MGMEM_PRESSURE_API
#endif
#ifdef __cplusplus
extern "C" {
#endif

#define MGMEM_PRESSURE_NORMAL 0u
#define MGMEM_PRESSURE_MODERATE 1u
#define MGMEM_PRESSURE_CRITICAL 2u
#define MGMEM_PRESSURE_OBSERVATION_LIVE 0u
#define MGMEM_PRESSURE_OBSERVATION_LAST_BEFORE_RELEASE 1u

typedef struct mgmem_pressure_controller mgmem_pressure_controller;
/* Pins the original domain owner, not the allocation provider. Callbacks must
 * be thread-safe/no-throw and must not reenter this controller. The caller must
 * keep domain valid until retain has returned, and must serialize destroy with
 * its other raw C API calls. The controller never closes or destroys domain. */
typedef struct mgmem_domain_lifetime_v1 {
    uint32_t struct_size;
    uint32_t version;
    void *context;
    void (*retain)(void *context);
    void (*release)(void *context);
} mgmem_domain_lifetime_v1;
typedef struct mgmem_pressure_options_v1 {
    uint32_t struct_size;
    uint32_t version;
    uint64_t normal_budget_bytes;
    uint64_t moderate_budget_bytes;
    uint64_t critical_budget_bytes;
    uint32_t deferred_poll_ms; /* 1..60000; retries BUSY without forced release. */
    uint32_t required_flags;
} mgmem_pressure_options_v1;
typedef struct mgmem_pressure_snapshot_v1 {
    uint32_t struct_size;
    uint32_t version;
    uint32_t state; /* OPEN/CLOSING/CLOSED controller state, not domain state. */
    uint32_t last_pressure;
    uint32_t trim_pending;
    uint32_t last_status;
    uint32_t domain_observation;
    uint32_t reserved;
    uint64_t events_accepted;
    uint64_t trim_attempts;
    uint64_t trim_successes;
    uint64_t deferred_busy;
    uint64_t over_budget_bytes; /* backing+reservation-budget, never RSS. */
    mgmem_snapshot_v1 domain;
} mgmem_pressure_snapshot_v1;

/* One active or creating controller per domain in this adapter runtime.
 * Creation reserves that slot before reading the budget; a competing create
 * returns BUSY. Initial normal budget must match the current domain budget;
 * attaching does not mutate it. Callers must coordinate direct budget writes
 * with creation and with the entire lifetime of an attached controller. */
MGMEM_PRESSURE_API mgmem_status mgmem_pressure_create_v1(mgmem_domain *domain,
    const mgmem_domain_lifetime_v1 *lifetime, const mgmem_pressure_options_v1 *options,
    mgmem_pressure_controller **out);
/* No provider callback/trim/drain executes on the notifying thread. NORMAL is
 * explicit restore only. Lower budgets preserve admitted pending operations. */
MGMEM_PRESSURE_API mgmem_status mgmem_pressure_notify_v1(mgmem_pressure_controller *controller,
    uint32_t pressure);
MGMEM_PRESSURE_API mgmem_status mgmem_pressure_snapshot_v1_get(mgmem_pressure_controller *controller,
    mgmem_pressure_snapshot_v1 *out, uint32_t out_size);
/* begin_close rejects new notifications and signals the worker; it does not
 * wait. drain waits for worker (0=poll), retaining the domain owner. A blocked
 * provider never authorizes forced destruction. destroy requires CLOSED, joins
 * the exited worker, then invokes owner.release on its caller; use cleanup work.
 * destroy consumes controller even if the no-throw release callback violates its
 * contract and returns PROVIDER_ERROR. It must never be retried in that case. */
MGMEM_PRESSURE_API mgmem_status mgmem_pressure_begin_close_v1(mgmem_pressure_controller *controller);
MGMEM_PRESSURE_API mgmem_status mgmem_pressure_drain_v1(mgmem_pressure_controller *controller,
    uint64_t timeout_ms);
MGMEM_PRESSURE_API mgmem_status mgmem_pressure_destroy_v1(mgmem_pressure_controller *controller);

/* Optional v2 mode. The v1 ABI and its synchronous notification semantics are
 * unchanged. All calls on one controller must use its creation version. */
#define MGMEM_PRESSURE_EVENT_QUEUED 0u
#define MGMEM_PRESSURE_EVENT_APPLIED 1u
#define MGMEM_PRESSURE_EVENT_FAILED 2u
#define MGMEM_PRESSURE_EVENT_CANCELLED 3u
#define MGMEM_PRESSURE_EVENT_EXPIRED 4u
#define MGMEM_PRESSURE_V2_MAX_QUEUE 256u
#define MGMEM_PRESSURE_V2_MAX_RESULTS 1024u
#define MGMEM_PRESSURE_V2_MAX_WORKERS 32u
typedef struct mgmem_pressure_options_v2 {
    uint32_t struct_size, version;
    uint64_t moderate_budget_bytes, critical_budget_bytes;
    /* poll 1..60000 ms; queue 1..MAX_QUEUE; results 1..MAX_RESULTS. */
    uint32_t deferred_poll_ms, queue_capacity, result_capacity, required_flags;
} mgmem_pressure_options_v2;
typedef struct mgmem_pressure_event_result_v2 {
    uint32_t struct_size, version, state, status;
    uint64_t ticket, apply_sequence;
} mgmem_pressure_event_result_v2;
typedef struct mgmem_pressure_snapshot_v2 {
    uint32_t struct_size, version, state, applied_pressure;
    /* counters_saturated is set on saturation or bounded-counter contention;
     * when set, counters are lower bounds rather than exact totals. */
    uint32_t trim_pending, last_trim_status, queue_depth, counters_saturated;
    uint64_t manual_budget_bytes, effective_budget_bytes, apply_sequence;
    uint64_t events_accepted, events_applied, events_failed, events_cancelled, events_busy;
    uint64_t trim_attempts, trim_successes, trim_busy, trim_failures;
    uint64_t events_critical_busy, results_evicted;
} mgmem_pressure_snapshot_v2;

/* Attaches without changing the current open domain budget, including zero.
 * The shared v1/v2 attachment slot is reserved before the budget snapshot; a
 * competing create returns BUSY. The caller must coordinate direct budget
 * writes with creation and route writes through this controller until it is
 * destroyed, and serialize destroy against all other raw calls. */
MGMEM_PRESSURE_API mgmem_status mgmem_pressure_create_v2(mgmem_domain *domain,
    const mgmem_domain_lifetime_v1 *lifetime, const mgmem_pressure_options_v2 *options,
    mgmem_pressure_controller **out);
/* Try-admission only: OK yields a nonzero queued ticket, BUSY yields no ticket.
 * No core, provider, trim or free operation runs on the submitting thread. */
MGMEM_PRESSURE_API mgmem_status mgmem_pressure_submit_v2(mgmem_pressure_controller *controller,
    uint32_t pressure, uint64_t *ticket);
/* Synchronous: on success commits manual budget and returns application order.
 * A queued event and this call are ordered by application, not enqueue time.
 * Do not call from a framework callback or while holding a managed-domain lock. */
MGMEM_PRESSURE_API mgmem_status mgmem_pressure_set_manual_budget_v2(mgmem_pressure_controller *controller,
    uint64_t bytes, uint64_t *apply_sequence);
/* An accepted ticket is QUEUED until applied, failed or cancelled. Completed
 * entries are kept in a bounded window; an evicted accepted ticket is EXPIRED,
 * not a successful event. Zero or never-accepted ticket is INVALID_ARGUMENT. */
MGMEM_PRESSURE_API mgmem_status mgmem_pressure_event_result_v2_get(mgmem_pressure_controller *controller,
    uint64_t ticket, mgmem_pressure_event_result_v2 *out, uint32_t out_size);
MGMEM_PRESSURE_API mgmem_status mgmem_pressure_snapshot_v2_get(mgmem_pressure_controller *controller,
    mgmem_pressure_snapshot_v2 *out, uint32_t out_size);
/* Close cancels queued events. An in-progress core or provider call must finish;
 * a timeout never permits forced destruction. drain takes milliseconds; zero
 * polls. A failed or inconsistent final snapshot makes drain and destroy return
 * an error and preserves the controller and owner pin; CLOSED describes worker
 * completion alone and is not proof of successful final observation. On confirmed completion,
 * destroy joins the worker then releases the pin. As with v1, a throwing release
 * can return PROVIDER_ERROR after consumption and must never be retried. */
MGMEM_PRESSURE_API mgmem_status mgmem_pressure_begin_close_v2(mgmem_pressure_controller *controller);
MGMEM_PRESSURE_API mgmem_status mgmem_pressure_drain_v2(mgmem_pressure_controller *controller, uint64_t timeout_ms);
MGMEM_PRESSURE_API mgmem_status mgmem_pressure_destroy_v2(mgmem_pressure_controller *controller);
#ifdef __cplusplus
}
#endif
#endif
