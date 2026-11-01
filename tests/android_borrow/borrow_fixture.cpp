#include <mgmem/android_borrow_jni.h>
#include <jni.h>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstring>
#include <mutex>

namespace {
std::mutex guard;
mgmem_domain *domain=nullptr;
mgmem_handle_v1 owner{};
uint64_t registration=0;
bool owner_live=false;
std::atomic<unsigned> pins{1};
std::atomic<unsigned> failed_release_calls{0};
std::atomic<bool> inject_java_exception{false};
std::atomic<bool> fixture_fault{false};
JavaVM *vm=nullptr;
std::mutex gate_mutex;
std::condition_variable gate_changed;
bool gate_armed=false,gate_entered=false,gate_allowed=false,gate_timed_out=false;
unsigned char external_data[32];
void retain(void *) noexcept {pins.fetch_add(1);}
void release(void *) noexcept {
    try {
        std::unique_lock<std::mutex> lock(gate_mutex);
        if(gate_armed) {
            gate_entered=true;gate_changed.notify_all();
            if(!gate_changed.wait_for(lock,std::chrono::seconds(6),[]{return gate_allowed;}))gate_timed_out=true;
            gate_armed=false;
        }
    } catch(...) {fixture_fault.store(true);gate_changed.notify_all();}
    pins.fetch_sub(1);
    try {
        if(inject_java_exception.exchange(false) && vm) {
            JNIEnv *env=nullptr;
            if(vm->GetEnv(reinterpret_cast<void **>(&env),JNI_VERSION_1_6)==JNI_OK) {
                jclass type=env->FindClass("java/lang/IllegalStateException");
                if(type && !env->ExceptionCheck())env->ThrowNew(type,"fixture JNI exception");
                if(type)env->DeleteLocalRef(type);
            }
        }
    } catch(...) {fixture_fault.store(true);}
}
mgmem_status retain_external(void *) noexcept {return MGMEM_OK;}
mgmem_status fail_external_release(void *) noexcept {++failed_release_calls;return MGMEM_PROVIDER_ERROR;}
constexpr uint64_t bytes=32;
}

#define JNI_NAME(method) Java_io_github_jichuo1_meguviz_memory_android_NativeBorrowInstrumentation_##method

extern "C" JNIEXPORT jlong JNICALL JNI_NAME(nativeCreate)(JNIEnv *,jclass,jboolean writable) {
    try {
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
    } catch(...) {fixture_fault.store(true);return 0;}
}

extern "C" JNIEXPORT jlong JNICALL JNI_NAME(nativeCreateFailedRelease)(JNIEnv *,jclass) {
    try {
    std::lock_guard<std::mutex> lock(guard);
    if(domain || pins.load()!=1)return 0;
    mgmem_domain_options_v1 options{sizeof(options),MGMEM_ABI_MAJOR,MGMEM_ABI_MINOR,0,MGMEM_SYSTEM,0,8192,0,nullptr};
    if(mgmem_domain_create_v1(&options,&domain)!=MGMEM_OK)return 0;
    std::memset(external_data,0x5a,sizeof(external_data));failed_release_calls.store(0);
    mgmem_external_buffer_checked_v1 input{};
    input.struct_size=sizeof(input);input.abi_major=MGMEM_ABI_MAJOR;input.abi_minor=MGMEM_ABI_MINOR;
    input.data=external_data;input.capacity_bytes=sizeof(external_data);input.alignment=1;
    input.external_id=0x424f52524f57ull;input.access=MGMEM_READ;input.retain=&retain_external;
    input.release_checked=&fail_external_release;input.managing_runtime_origin=mgmem_runtime_origin_v1();
    if(mgmem_import_external_checked_v1(domain,&input,&owner)!=MGMEM_OK)return 0;
    owner_live=true;
    const mgmem_domain_lifetime_v1 lifetime{sizeof(lifetime),1,nullptr,&retain,&release};
    if(mgmem_android_borrow_register_v1(domain,&lifetime,owner,4,16,MGMEM_READ,
            MGMEM_ANDROID_BORROW_INITIALIZED,&registration)!=MGMEM_OK)return 0;
    return static_cast<jlong>(registration);
    } catch(...) {fixture_fault.store(true);return 0;}
}

extern "C" JNIEXPORT jboolean JNICALL JNI_NAME(nativeReleaseGate)(JNIEnv *,jclass,jint action) {
    try {
    std::unique_lock<std::mutex> lock(gate_mutex);
    if(action==0) {
        if(gate_armed)return JNI_FALSE;
        gate_armed=true;gate_entered=false;gate_allowed=false;gate_timed_out=false;return JNI_TRUE;
    }
    if(action==1) return gate_changed.wait_for(lock,std::chrono::seconds(4),[]{return gate_entered;})?JNI_TRUE:JNI_FALSE;
    if(action==2) {gate_allowed=true;gate_changed.notify_all();return JNI_TRUE;}
    if(action==3) return !gate_timed_out?JNI_TRUE:JNI_FALSE;
    return JNI_FALSE;
    } catch(...) {fixture_fault.store(true);gate_changed.notify_all();return JNI_FALSE;}
}

extern "C" JNIEXPORT jboolean JNICALL JNI_NAME(nativeArmJavaException)(JNIEnv *env,jclass) {
    try {
    std::lock_guard<std::mutex> lock(guard);
    if(!domain || owner_live || pins.load()!=2 || env->GetJavaVM(&vm)!=JNI_OK)return JNI_FALSE;
    inject_java_exception.store(true);return JNI_TRUE;
    } catch(...) {fixture_fault.store(true);return JNI_FALSE;}
}

extern "C" JNIEXPORT jboolean JNICALL JNI_NAME(nativeFailureQuarantined)(JNIEnv *,jclass) {
    try {
    std::lock_guard<std::mutex> lock(guard);
    if(fixture_fault.load() || !domain || owner_live || pins.load()!=2 || failed_release_calls.load()!=1)return JNI_FALSE;
    mgmem_snapshot_v1 snapshot{};mgmem_import_snapshot_v1 imports{};
    if(mgmem_domain_snapshot_v1(domain,&snapshot,sizeof(snapshot))!=MGMEM_OK ||
       mgmem_domain_import_snapshot_v1(domain,&imports,sizeof(imports))!=MGMEM_OK)return JNI_FALSE;
    if(snapshot.state!=MGMEM_CLOSING || snapshot.backing_bytes!=sizeof(external_data) ||
       snapshot.active_allocations!=0 || snapshot.active_leases!=0 || imports.poisoned!=1 ||
       imports.external_backing_bytes!=sizeof(external_data) ||
       mgmem_domain_collect_v1(domain)!=MGMEM_PROVIDER_ERROR ||
       mgmem_domain_drain_v1(domain,0)!=MGMEM_PROVIDER_ERROR)return JNI_FALSE;
    const auto status=mgmem_domain_destroy_v1(domain);
    if(status!=MGMEM_BUSY) {
        // Unexpected destruction status can consume the domain. Never retry it.
        domain=nullptr;fixture_fault.store(true);return JNI_FALSE;
    }
    return JNI_TRUE;
    } catch(...) {fixture_fault.store(true);return JNI_FALSE;}
}

extern "C" JNIEXPORT jboolean JNICALL JNI_NAME(nativeBeginClose)(JNIEnv *,jclass) {
    try {
    std::lock_guard<std::mutex> lock(guard);
    if(!domain || !owner_live)return JNI_FALSE;
    owner_live=false;
    if(mgmem_release_v1(domain,owner)!=MGMEM_OK){fixture_fault.store(true);return JNI_FALSE;}
    return mgmem_domain_begin_close_v1(domain)==MGMEM_OK?JNI_TRUE:JNI_FALSE;
    } catch(...) {fixture_fault.store(true);return JNI_FALSE;}
}

extern "C" JNIEXPORT jlongArray JNICALL JNI_NAME(nativeSnapshot)(JNIEnv *env,jclass) {
    try {
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
    } catch(...) {fixture_fault.store(true);return nullptr;}
}

extern "C" JNIEXPORT jboolean JNICALL JNI_NAME(nativeCollectBusy)(JNIEnv *,jclass) {
    try {
    std::lock_guard<std::mutex> lock(guard);
    return domain && mgmem_domain_collect_v1(domain)==MGMEM_BUSY?JNI_TRUE:JNI_FALSE;
    } catch(...) {fixture_fault.store(true);return JNI_FALSE;}
}

extern "C" JNIEXPORT jboolean JNICALL JNI_NAME(nativeFinish)(JNIEnv *,jclass) {
    try {
    std::lock_guard<std::mutex> lock(guard);
    if(fixture_fault.load() || !domain || owner_live || pins.load()!=1 || mgmem_domain_collect_v1(domain)!=MGMEM_OK ||
        mgmem_domain_drain_v1(domain,0)!=MGMEM_OK)return JNI_FALSE;
    mgmem_snapshot_v1 snapshot{};
    if(mgmem_domain_snapshot_v1(domain,&snapshot,sizeof(snapshot))!=MGMEM_OK || snapshot.state!=MGMEM_CLOSED ||
        snapshot.backing_bytes || snapshot.reserved_bytes || snapshot.live_bytes || snapshot.retiring_bytes ||
        snapshot.active_allocations || snapshot.active_leases)return JNI_FALSE;
    const auto status=mgmem_domain_destroy_v1(domain);
    if(status!=MGMEM_OK) {
        if(status!=MGMEM_BUSY){domain=nullptr;fixture_fault.store(true);}
        return JNI_FALSE;
    }
    domain=nullptr;owner={};registration=0;return JNI_TRUE;
    } catch(...) {fixture_fault.store(true);return JNI_FALSE;}
}

extern "C" JNIEXPORT void JNICALL JNI_NAME(nativeCleanupBestEffort)(JNIEnv *,jclass) {
    try {
    std::lock_guard<std::mutex> lock(guard);
    if(!domain)return;
    if(registration) (void)mgmem_android_borrow_cancel_v1(registration); // Only unclaimed IDs can cancel.
    if(owner_live) {
        owner_live=false;
        if(mgmem_release_v1(domain,owner)!=MGMEM_OK){fixture_fault.store(true);return;}
    }
    if(!fixture_fault.load() && mgmem_domain_begin_close_v1(domain)==MGMEM_OK && pins.load()==1 &&
        mgmem_domain_collect_v1(domain)==MGMEM_OK && mgmem_domain_drain_v1(domain,0)==MGMEM_OK) {
        const auto status=mgmem_domain_destroy_v1(domain);
        if(status==MGMEM_OK){domain=nullptr;owner={};registration=0;}
        else if(status!=MGMEM_BUSY){domain=nullptr;fixture_fault.store(true);}
    }
    } catch(...) {fixture_fault.store(true);}
}
