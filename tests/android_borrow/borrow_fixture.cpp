#include <mgmem/android_borrow_jni.h>
#include <jni.h>
#include <atomic>
#include <cstring>
#include <mutex>

namespace {
std::mutex guard;
mgmem_domain *domain=nullptr;
mgmem_handle_v1 owner{};
uint64_t registration=0;
bool owner_live=false;
std::atomic<unsigned> pins{1};
void retain(void *) {pins.fetch_add(1);}
void release(void *) {pins.fetch_sub(1);}
constexpr uint64_t bytes=32;
}

#define JNI_NAME(method) Java_io_github_jichuo1_meguviz_memory_android_NativeBorrowInstrumentation_##method

extern "C" JNIEXPORT jlong JNICALL JNI_NAME(nativeCreate)(JNIEnv *,jclass,jboolean writable) {
    std::lock_guard<std::mutex> lock(guard);
    if(domain || pins.load()!=1)return 0;
    mgmem_domain_options_v1 options{sizeof(options),MGMEM_ABI_MAJOR,MGMEM_ABI_MINOR,0,MGMEM_SYSTEM,0,8192,0,nullptr};
    if(mgmem_domain_create_v1(&options,&domain)!=MGMEM_OK)return 0;
    if(mgmem_allocate_v1(domain,bytes,64,&owner)!=MGMEM_OK)return 0;
    owner_live=true;
    mgmem_borrow_v1 initial{};
    if(mgmem_borrow_acquire_v1(domain,owner,0,bytes,MGMEM_WRITE,&initial)!=MGMEM_OK)return 0;
    std::memset(initial.data,0x5a,static_cast<size_t>(bytes));
    if(mgmem_borrow_release_v1(domain,initial.lease)!=MGMEM_OK)return 0;
    const mgmem_domain_lifetime_v1 lifetime{sizeof(lifetime),1,nullptr,&retain,&release};
    uint64_t rejected=99;
    const auto access=writable?MGMEM_WRITE:MGMEM_READ;
    if(mgmem_android_borrow_register_v1(domain,&lifetime,owner,4,16,access,0,&rejected)!=MGMEM_INVALID_ARGUMENT || rejected!=0)return 0;
    if(mgmem_android_borrow_register_v1(domain,&lifetime,owner,4,16,access,
            MGMEM_ANDROID_BORROW_INITIALIZED,&registration)!=MGMEM_OK)return 0;
    return static_cast<jlong>(registration);
}

extern "C" JNIEXPORT jboolean JNICALL JNI_NAME(nativeBeginClose)(JNIEnv *,jclass) {
    std::lock_guard<std::mutex> lock(guard);
    if(!domain || !owner_live)return JNI_FALSE;
    if(mgmem_release_v1(domain,owner)!=MGMEM_OK)return JNI_FALSE;
    owner_live=false;
    return mgmem_domain_begin_close_v1(domain)==MGMEM_OK?JNI_TRUE:JNI_FALSE;
}

extern "C" JNIEXPORT jlongArray JNICALL JNI_NAME(nativeSnapshot)(JNIEnv *env,jclass) {
    std::lock_guard<std::mutex> lock(guard);
    if(!domain)return nullptr;
    mgmem_snapshot_v1 snapshot{};
    if(mgmem_domain_snapshot_v1(domain,&snapshot,sizeof(snapshot))!=MGMEM_OK)return nullptr;
    jlong values[]={static_cast<jlong>(snapshot.state),static_cast<jlong>(snapshot.backing_bytes),
        static_cast<jlong>(snapshot.reserved_bytes),static_cast<jlong>(snapshot.live_bytes),
        static_cast<jlong>(snapshot.retiring_bytes),static_cast<jlong>(snapshot.active_allocations),
        static_cast<jlong>(snapshot.active_leases),static_cast<jlong>(pins.load())};
    auto result=env->NewLongArray(8);
    if(result && !env->ExceptionCheck())env->SetLongArrayRegion(result,0,8,values);
    return result;
}

extern "C" JNIEXPORT jboolean JNICALL JNI_NAME(nativeCollectBusy)(JNIEnv *,jclass) {
    std::lock_guard<std::mutex> lock(guard);
    return domain && mgmem_domain_collect_v1(domain)==MGMEM_BUSY?JNI_TRUE:JNI_FALSE;
}

extern "C" JNIEXPORT jboolean JNICALL JNI_NAME(nativeFinish)(JNIEnv *,jclass) {
    std::lock_guard<std::mutex> lock(guard);
    if(!domain || owner_live || pins.load()!=1 || mgmem_domain_collect_v1(domain)!=MGMEM_OK ||
        mgmem_domain_drain_v1(domain,0)!=MGMEM_OK)return JNI_FALSE;
    mgmem_snapshot_v1 snapshot{};
    if(mgmem_domain_snapshot_v1(domain,&snapshot,sizeof(snapshot))!=MGMEM_OK || snapshot.state!=MGMEM_CLOSED ||
        snapshot.backing_bytes || snapshot.reserved_bytes || snapshot.live_bytes || snapshot.retiring_bytes ||
        snapshot.active_allocations || snapshot.active_leases || mgmem_domain_destroy_v1(domain)!=MGMEM_OK)return JNI_FALSE;
    domain=nullptr;owner={};registration=0;return JNI_TRUE;
}

extern "C" JNIEXPORT void JNICALL JNI_NAME(nativeCleanupBestEffort)(JNIEnv *,jclass) {
    std::lock_guard<std::mutex> lock(guard);
    if(!domain)return;
    if(registration) (void)mgmem_android_borrow_cancel_v1(registration); // Only unclaimed IDs can cancel.
    if(owner_live && mgmem_release_v1(domain,owner)==MGMEM_OK)owner_live=false;
    if(!owner_live && mgmem_domain_begin_close_v1(domain)==MGMEM_OK && pins.load()==1 &&
        mgmem_domain_collect_v1(domain)==MGMEM_OK && mgmem_domain_drain_v1(domain,0)==MGMEM_OK &&
        mgmem_domain_destroy_v1(domain)==MGMEM_OK) {domain=nullptr;owner={};registration=0;}
}
