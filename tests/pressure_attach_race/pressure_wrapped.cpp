// Test-only wrapper: intercept just the controller's core snapshot calls.
// The linked core itself is unchanged and uses the real snapshot function.
#include <mgmem/pressure.h>
extern "C" mgmem_status pressure_attach_test_snapshot(
    mgmem_domain *, mgmem_snapshot_v1 *, uint32_t);
#define mgmem_domain_snapshot_v1 pressure_attach_test_snapshot
#include "../../adapters/android/native/pressure.cpp"
#undef mgmem_domain_snapshot_v1
