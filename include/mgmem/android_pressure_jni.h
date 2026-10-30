#ifndef MGMEM_ANDROID_PRESSURE_JNI_H
#define MGMEM_ANDROID_PRESSURE_JNI_H
#include <mgmem/pressure.h>
#ifdef __cplusplus
extern "C" {
#endif
/* Implemented by the Android JNI composition, not core. It owns a controller
 * registration, not domain storage. The same final mgmem runtime must be used.
 * Java wraps the returned monotonic ID with fromNativeRegistration; it is not
 * an address and must not be cast or persisted across process/runtime lifetime. */
MGMEM_PRESSURE_API mgmem_status mgmem_android_pressure_register_v1(mgmem_domain *domain,
    const mgmem_domain_lifetime_v1 *lifetime, const mgmem_pressure_options_v1 *options,
    uint64_t *out_registration);
/* Async detach/close. For external registrations it never closes the foreign
 * domain. Owned Java factory registrations include their domain's quiet close. */
MGMEM_PRESSURE_API mgmem_status mgmem_android_pressure_unregister_v1(uint64_t registration);
#ifdef __cplusplus
}
#endif
#endif
