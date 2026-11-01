#include <mgmem/compute_read_bridge.h>
#include <meguviz/compute.h>
#include <atomic>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <mutex>
#include <thread>

#define CHECK(c) do { if (!(c)) { std::fprintf(stderr, "failed %s:%d: %s\n", __FILE__, __LINE__, #c); std::abort(); } } while (0)
struct MgcLease { unsigned char data[64]{}; std::atomic<unsigned> refs{1}; bool fail_unmap = false; };
extern "C" void mgc_lease_retain(MgcLease *lease) { ++lease->refs; }
extern "C" void mgc_lease_release(MgcLease *lease) { --lease->refs; }
extern "C" MgcStatus mgc_lease_info(MgcLease *, MgcLeaseInfo *info) {
    *info = {}; info->header = MGC_HEADER_INIT(MgcLeaseInfo);
    info->access = MGC_ACCESS_READ; info->dependency_kind = MGC_DEPENDENCY_READY;
    info->capacity_bytes = 64; info->byte_length = 64; return MGC_SUCCESS;
}
extern "C" MgcStatus mgc_lease_map(MgcLease *lease, void **data, uint64_t *bytes) {
    ++lease->refs; *data = lease->data; *bytes = 64; return MGC_SUCCESS;
}
extern "C" MgcStatus mgc_lease_unmap(MgcLease *lease) {
    if (lease->fail_unmap) return MGC_INTERNAL_ERROR;
    --lease->refs; return MGC_SUCCESS;
}
std::mutex gate_mutex;
std::condition_variable gate_cv;
bool entered = false, proceed = false;
extern "C" void mgmem_compute_bridge_test_after_retain() {
    std::unique_lock<std::mutex> lock(gate_mutex);
    entered = true; gate_cv.notify_all();
    gate_cv.wait(lock, [] { return proceed; });
}
struct Observation { std::atomic<unsigned> calls{0}; std::atomic<int32_t> status{0}; };
static void on_error(void *context, int32_t status) {
    auto *observation = static_cast<Observation *>(context);
    observation->status.store(status); ++observation->calls;
}
static mgmem_status dummy_retain(void *) { return MGMEM_OK; }
static mgmem_status dummy_release(void *) { return MGMEM_OK; }

static void run(bool fail) {
    mgmem_domain_options_v1 options{}; options.struct_size = sizeof(options); options.abi_major = MGMEM_ABI_MAJOR;
    options.abi_minor = MGMEM_ABI_MINOR; options.mode = MGMEM_SYSTEM; options.budget_bytes = 64;
    mgmem_domain *domain = nullptr; CHECK(mgmem_domain_create_v1(&options, &domain) == MGMEM_OK);
    MgcLease origin{}; origin.fail_unmap = fail; Observation observation;
    mgmem_handle_v1 output{}; int32_t compute_status = 0; mgmem_status result = MGMEM_OK;
    { std::lock_guard<std::mutex> lock(gate_mutex); entered = false; proceed = false; }
    std::thread worker([&] { result = mgmem_compute_import_read_v1(domain, &origin, fail ? 202 : 201,
                                    on_error, &observation, &output, &compute_status); });
    { std::unique_lock<std::mutex> lock(gate_mutex); gate_cv.wait(lock, [] { return entered; }); }
    CHECK(origin.refs.load() == 3); // caller + independent pin + mapped pin
    CHECK(mgmem_domain_begin_close_v1(domain) == MGMEM_OK);
    { std::lock_guard<std::mutex> lock(gate_mutex); proceed = true; gate_cv.notify_all(); }
    worker.join();
    CHECK(output.id == 0);
    mgmem_import_snapshot_v1 snapshot{};
    CHECK(mgmem_domain_import_snapshot_v1(domain, &snapshot, sizeof(snapshot)) == MGMEM_OK);
    if (!fail) {
        CHECK(result == MGMEM_IS_CLOSED && compute_status == MGC_SUCCESS && observation.calls.load() == 0);
        CHECK(origin.refs.load() == 1 && snapshot.poisoned == 0 && snapshot.external_backing_bytes == 0);
        mgc_lease_release(&origin); CHECK(origin.refs.load() == 0);
        CHECK(mgmem_domain_collect_v1(domain) == MGMEM_OK);
        CHECK(mgmem_domain_destroy_v1(domain) == MGMEM_OK);
        return;
    }
    CHECK(result == MGMEM_PROVIDER_ERROR && compute_status == MGC_INTERNAL_ERROR);
    CHECK(observation.calls.load() == 1 && observation.status.load() == MGC_INTERNAL_ERROR);
    CHECK(origin.refs.load() == 3 && snapshot.poisoned == 1 && snapshot.external_backing_bytes == 64);
    mgc_lease_release(&origin); CHECK(origin.refs.load() == 2); // preserve independent + map refs
    mgmem_domain *other = nullptr; CHECK(mgmem_domain_create_v1(&options, &other) == MGMEM_OK);
    mgmem_external_buffer_checked_v1 duplicate{};
    duplicate.struct_size = sizeof(duplicate); duplicate.abi_major = MGMEM_ABI_MAJOR; duplicate.abi_minor = MGMEM_ABI_MINOR;
    duplicate.data = origin.data; duplicate.capacity_bytes = 64; duplicate.alignment = 1; duplicate.external_id = 203;
    duplicate.access = MGMEM_READ; duplicate.retain = dummy_retain; duplicate.release_checked = dummy_release;
    duplicate.managing_runtime_origin = mgmem_runtime_origin_v1();
    CHECK(mgmem_import_external_checked_v1(other, &duplicate, &output) == MGMEM_BUSY);
    CHECK(mgmem_domain_collect_v1(domain) == MGMEM_PROVIDER_ERROR);
    std::puts("PASS controlled close race: checked rollback failure retains origin and claim");
    std::fflush(stdout); std::_Exit(0);
}

int main() { run(false); run(true); }
