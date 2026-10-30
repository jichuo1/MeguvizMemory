#include <mgmem/mgmem.h>
#include <stdint.h>
#include <string.h>
#include <stdio.h>

#define CHECK(call) do { if ((call) != MGMEM_OK) { fprintf(stderr,"C ABI failure at %d\n",__LINE__); return 1; } } while (0)
int main(void) {
    if (!(mgmem_capabilities_v1() & MGMEM_CAP_SYSTEM)) return 4;
    mgmem_domain_options_v1 options = { sizeof(options), 1, 0, 0, MGMEM_SYSTEM, 0, 4096, 0, NULL };
    mgmem_domain *domain = NULL;
    mgmem_handle_v1 allocation = {0, 0};
    mgmem_borrow_v1 borrow;
    mgmem_snapshot_v1 snapshot;
    CHECK(mgmem_domain_create_v1(&options, &domain));
    CHECK(mgmem_allocate_v1(domain, 128, 64, &allocation));
    CHECK(mgmem_borrow_acquire_v1(domain, allocation, 0, 128, MGMEM_WRITE, &borrow));
    memset(borrow.data, 0x5a, (size_t)borrow.length);
    CHECK(mgmem_release_v1(domain, allocation));
    CHECK(mgmem_domain_begin_close_v1(domain));
    if (((unsigned char*)borrow.data)[127] != 0x5a || mgmem_domain_drain_v1(domain,0) != MGMEM_TIMEOUT) return 2;
    CHECK(mgmem_borrow_release_v1(domain,borrow.lease));
    CHECK(mgmem_domain_collect_v1(domain));
    CHECK(mgmem_domain_drain_v1(domain,0));
    CHECK(mgmem_domain_snapshot_v1(domain,&snapshot,sizeof(snapshot)));
    if (snapshot.backing_bytes || snapshot.active_leases || snapshot.state != MGMEM_CLOSED) return 3;
    CHECK(mgmem_domain_destroy_v1(domain));
    puts("{\"consumer\":\"pure_c_abi\",\"success\":true,\"backing_after_close\":0}");
    return 0;
}
