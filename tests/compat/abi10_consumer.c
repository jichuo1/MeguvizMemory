#include <mgmem/mgmem.h>
#if MGMEM_ABI_MAJOR != 1u || MGMEM_ABI_MINOR != 0u
#error This consumer must compile against the frozen ABI 1.0 header
#endif
/* The unchanged original C consumer is compiled with the original declarations
 * and linked to the new runtime. Rebuilding against current headers would not
 * exercise backward-compatible descriptor sizes and minor-version admission. */
#include "../c_consumer.c"
