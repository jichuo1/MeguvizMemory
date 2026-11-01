#ifndef MGMEM_ANDROID_BORROW_JNI_H
#define MGMEM_ANDROID_BORROW_JNI_H

#include <mgmem/pressure.h>

#ifdef __cplusplus
extern "C" {
#endif

#define MGMEM_ANDROID_BORROW_INITIALIZED 1u
#define MGMEM_ANDROID_BORROW_MAX_WRITE_BYTES (1024u * 1024u)
#define MGMEM_ANDROID_BORROW_RUNTIME_STAGING_BYTES (16u * 1024u * 1024u)

/* Implemented by the Android JNI adapter, not the core library. The native
 * caller keeps domain/owner/handle valid through this call. lifetime pins the
 * original domain owner through the complete final borrow_release call, and
 * must obey the retain/release contract in pressure.h. On success, a READ or
 * WRITE lease is held until Java closes it or the unclaimed registration is
 * cancelled. Existing borrows remain usable while the domain is CLOSING.
 * flags MUST be MGMEM_ANDROID_BORROW_INITIALIZED: before registration the
 * caller must initialize every byte in the requested region and maintain that
 * guarantee throughout the lease. Core allocations are uninitialized; this
 * flag is an explicit origin-owner assertion, not a runtime initialization
 * check. WRITE leases also require initialization because they permit reads.
 *
 * The ID is a non-reused process-local registry token, not an address. It can
 * be claimed by exactly one Java NativeMemoryLease; it must not be persisted,
 * shared with an untrusted party, or used with a different loaded adapter or
 * Memory runtime. Use the same shared Memory runtime for domain and adapter.
 *
 * cancel applies only before Java claims the token. Both cancellation and Java
 * close may run the originating provider or owner release callback on the
 * caller's thread: invoke them on a suitable cleanup thread. Failed release
 * quarantines the registration without retry or false success; the owner pin
 * is retained when the borrow release itself fails. All callbacks must be
 * thread-safe/no-throw. A callback violation leaves ownership uncertain and is
 * not forcibly reclaimed. No JNI DSO unload while
 * registrations, access calls, or quarantined entries exist is supported. */
MGMEM_PRESSURE_API mgmem_status mgmem_android_borrow_register_v1(
    mgmem_domain *domain, const mgmem_domain_lifetime_v1 *lifetime,
    mgmem_handle_v1 owner, uint64_t offset, uint64_t length, uint32_t access, uint32_t flags,
    uint64_t *out_registration);
MGMEM_PRESSURE_API mgmem_status mgmem_android_borrow_cancel_v1(uint64_t registration);

#ifdef __cplusplus
}
#endif
#endif
