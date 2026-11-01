#include <mgmem/compute_read_bridge.h>
#include <meguviz/compute.h>
#include <cstdio>
#include <cstdlib>

#define CHECK(c) do { if (!(c)) { std::fprintf(stderr, "failed %s:%d: %s\n", __FILE__, __LINE__, #c); std::abort(); } } while (0)
struct MgcLease { unsigned char data[64]{}; bool mapped = false; unsigned references = 1; };
extern "C" void mgc_lease_retain(MgcLease *lease) { ++lease->references; }
extern "C" void mgc_lease_release(MgcLease *lease) { --lease->references; }
extern "C" MgcStatus mgc_lease_info(MgcLease *, MgcLeaseInfo *info) {
    *info = {}; info->header = MGC_HEADER_INIT(MgcLeaseInfo);
    info->access = MGC_ACCESS_READ; info->dependency_kind = MGC_DEPENDENCY_READY;
    info->capacity_bytes = sizeof(MgcLease::data); info->byte_length = sizeof(MgcLease::data);
    return MGC_SUCCESS;
}
extern "C" MgcStatus mgc_lease_map(MgcLease *lease, void **data, uint64_t *bytes) {
    if (lease->mapped) return MGC_BUSY;
    lease->mapped = true; ++lease->references; *data = lease->data; *bytes = sizeof(lease->data); return MGC_SUCCESS;
}
extern "C" MgcStatus mgc_lease_unmap(MgcLease *) { return MGC_INTERNAL_ERROR; }
struct Observation { unsigned calls = 0; int32_t status = 0; };
static void on_error(void *context, int32_t status) {
    auto *observation = static_cast<Observation *>(context); ++observation->calls; observation->status = status;
}

int main() {
    mgmem_domain_options_v1 options{}; options.struct_size = sizeof(options); options.abi_major = MGMEM_ABI_MAJOR;
    options.abi_minor = MGMEM_ABI_MINOR; options.mode = MGMEM_SYSTEM; options.budget_bytes = 64;
    mgmem_domain *domain = nullptr; CHECK(mgmem_domain_create_v1(&options, &domain) == MGMEM_OK);
    CHECK(mgmem_domain_set_budget_v1(domain, 0) == MGMEM_OK);
    MgcLease origin{}; Observation observation; mgmem_handle_v1 handle{}; int32_t status = 0;
    CHECK(mgmem_compute_import_read_v1(domain, &origin, 7, on_error, &observation, &handle, &status)
          == MGMEM_COMPUTE_ROLLBACK_QUARANTINED);
    CHECK(handle.id == 0 && status == MGC_INTERNAL_ERROR && observation.calls == 1);
    CHECK(origin.mapped && origin.references == 3);
    mgc_lease_release(&origin); // caller drops its reference; mapping and bridge pin remain
    CHECK(origin.references == 2);
    mgmem_import_snapshot_v1 snapshot{};
    CHECK(mgmem_domain_import_snapshot_v1(domain, &snapshot, sizeof(snapshot)) == MGMEM_OK);
    CHECK(snapshot.poisoned == 0 && snapshot.external_backing_bytes == 0 && snapshot.external_backing_count == 0);
    CHECK(mgmem_domain_begin_close_v1(domain) == MGMEM_OK);
    CHECK(mgmem_domain_collect_v1(domain) == MGMEM_OK);
    CHECK(mgmem_domain_destroy_v1(domain) == MGMEM_OK);
    std::puts("PASS fake pre-admission uncertain-origin reporting; no core claim; fixture retained to exit");
    std::fflush(stdout); std::_Exit(0);
}
