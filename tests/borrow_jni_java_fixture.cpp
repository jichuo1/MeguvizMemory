#include <mgmem/android_borrow_jni.h>
#include <jni.h>
#include <atomic>
#include <cstring>

namespace {
mgmem_domain *domain=nullptr;
std::atomic<unsigned> pins{1};
void retain(void *) { ++pins; }
void release(void *) { --pins; }
}

extern "C" JNIEXPORT jlong JNICALL
Java_io_github_jichuo1_meguviz_memory_android_NativeMemoryLeaseJvmTest_create(JNIEnv *,jclass,jboolean write) {
    if (domain || pins!=1) return 0;
    mgmem_domain_options_v1 options{sizeof(options),MGMEM_ABI_MAJOR,MGMEM_ABI_MINOR,0,MGMEM_SYSTEM,0,8192,0,nullptr};
    if (mgmem_domain_create_v1(&options,&domain)!=MGMEM_OK) return 0;
    mgmem_handle_v1 owner{};
    if (mgmem_allocate_v1(domain,32,64,&owner)!=MGMEM_OK) return 0;
    mgmem_borrow_v1 initial{};
    if (mgmem_borrow_acquire_v1(domain,owner,0,32,MGMEM_WRITE,&initial)!=MGMEM_OK) return 0;
    std::memset(initial.data,0x5a,32);
    if (mgmem_borrow_release_v1(domain,initial.lease)!=MGMEM_OK) return 0;
    const mgmem_domain_lifetime_v1 life{sizeof(life),1,nullptr,&retain,&release};
    uint64_t id=0;
    if (mgmem_android_borrow_register_v1(domain,&life,owner,4,16,write?MGMEM_WRITE:MGMEM_READ,
            MGMEM_ANDROID_BORROW_INITIALIZED,&id)!=MGMEM_OK) return 0;
    if (mgmem_release_v1(domain,owner)!=MGMEM_OK || mgmem_domain_begin_close_v1(domain)!=MGMEM_OK) return 0;
    return static_cast<jlong>(id);
}
extern "C" JNIEXPORT jboolean JNICALL
Java_io_github_jichuo1_meguviz_memory_android_NativeMemoryLeaseJvmTest_finish(JNIEnv *,jclass) {
    if (!domain || pins!=1 || mgmem_domain_collect_v1(domain)!=MGMEM_OK ||
        mgmem_domain_drain_v1(domain,0)!=MGMEM_OK || mgmem_domain_destroy_v1(domain)!=MGMEM_OK) return JNI_FALSE;
    domain=nullptr;return JNI_TRUE;
}
