#include <mgmem/mgmem.h>
#include <stdio.h>

static unsigned char bytes[64];
static unsigned retains, releases;
static mgmem_status retain(void *context) { (void)context; ++retains; return MGMEM_OK; }
static mgmem_status release_checked(void *context) { (void)context; ++releases; return MGMEM_OK; }

int main(void) {
    mgmem_domain_options_v1 options = {0};
    mgmem_external_buffer_checked_v1 input = {0};
    mgmem_domain *domain = NULL;
    mgmem_handle_v1 owner = {0};
    mgmem_borrow_v1 borrow = {0};
    options.struct_size = sizeof(options); options.abi_major = MGMEM_ABI_MAJOR;
    options.abi_minor = MGMEM_ABI_MINOR; options.mode = MGMEM_SYSTEM; options.budget_bytes = 64;
    input.struct_size = sizeof(input); input.abi_major = MGMEM_ABI_MAJOR; input.abi_minor = MGMEM_ABI_MINOR;
    input.data = bytes; input.capacity_bytes = sizeof(bytes); input.alignment = 1;
    input.external_id = 1; input.access = MGMEM_READ;
    input.retain = retain; input.release_checked = release_checked;
    input.managing_runtime_origin = mgmem_runtime_origin_v1();
    if (!(mgmem_capabilities_v1() & MGMEM_CAP_CHECKED_EXTERNAL_RELEASE) ||
        mgmem_domain_create_v1(&options, &domain) != MGMEM_OK ||
        mgmem_import_external_checked_v1(domain, &input, &owner) != MGMEM_OK ||
        mgmem_borrow_acquire_v1(domain, owner, 0, sizeof(bytes), MGMEM_READ, &borrow) != MGMEM_OK ||
        mgmem_release_v1(domain, owner) != MGMEM_OK || releases != 0 ||
        mgmem_borrow_release_v1(domain, borrow.lease) != MGMEM_OK || retains != 1 || releases != 1 ||
        mgmem_domain_begin_close_v1(domain) != MGMEM_OK || mgmem_domain_collect_v1(domain) != MGMEM_OK ||
        mgmem_domain_drain_v1(domain, 0) != MGMEM_OK || mgmem_domain_destroy_v1(domain) != MGMEM_OK) return 1;
    puts("{\"suite\":\"checked-import-c-abi\",\"status\":\"PASS\"}");
    return 0;
}
