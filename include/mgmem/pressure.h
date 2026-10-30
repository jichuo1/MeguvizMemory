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

/* One active controller per domain in this adapter runtime. Initial normal
 * budget must match domain's current budget; attaching does not mutate it. */
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
#ifdef __cplusplus
}
#endif
#endif
