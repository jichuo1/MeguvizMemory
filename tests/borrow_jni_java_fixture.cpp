#include <mgmem/android_borrow_jni.h>
#include <jni.h>
#include <atomic>
#include <cstring>

namespace {
mgmem_domain *domain=nullptr;
std::atomic<unsigned> pins{1};
JavaVM *jvm=nullptr;
jobject release_hook=nullptr;
jmethodID release_hook_run=nullptr;
void retain(void *) { ++pins; }
void release(void *) {
    --pins;
    if (release_hook && jvm) {
        JNIEnv *env=nullptr;
        if (jvm->GetEnv(reinterpret_cast<void **>(&env),JNI_VERSION_1_6)==JNI_OK)
            env->CallVoidMethod(release_hook,release_hook_run);
    }
}
}

extern "C" JNIEXPORT jboolean JNICALL
Java_io_github_jichuo1_meguviz_memory_android_NativeMemoryLeaseJvmTest_setReleaseHook(JNIEnv *env,jclass,jobject hook) {
    if (release_hook) { env->DeleteGlobalRef(release_hook); release_hook=nullptr; }
    release_hook_run=nullptr;
    if (!hook) return JNI_TRUE;
    if (env->GetJavaVM(&jvm)!=JNI_OK) return JNI_FALSE;
    jclass type=env->GetObjectClass(hook);
    if (!type || env->ExceptionCheck()) return JNI_FALSE;
    release_hook_run=env->GetMethodID(type,"run","()V");
    env->DeleteLocalRef(type);
    if (!release_hook_run || env->ExceptionCheck()) return JNI_FALSE;
    release_hook=env->NewGlobalRef(hook);
    return release_hook && !env->ExceptionCheck() ? JNI_TRUE : JNI_FALSE;
}

namespace {
unsigned char external_data[32];
mgmem_status retainExternal(void *) { return MGMEM_OK; }
mgmem_status failExternalRelease(void *) {
    if (release_hook && jvm) {
        JNIEnv *env=nullptr;
        if (jvm->GetEnv(reinterpret_cast<void **>(&env),JNI_VERSION_1_6)==JNI_OK)
            env->CallVoidMethod(release_hook,release_hook_run);
    }
    return MGMEM_PROVIDER_ERROR;
}
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

extern "C" JNIEXPORT jlong JNICALL
Java_io_github_jichuo1_meguviz_memory_android_NativeMemoryLeaseJvmTest_createFailingClose(JNIEnv *,jclass) {
    if (domain || pins!=1) return 0;
    mgmem_domain_options_v1 options{sizeof(options),MGMEM_ABI_MAJOR,MGMEM_ABI_MINOR,0,MGMEM_SYSTEM,0,8192,0,nullptr};
    if (mgmem_domain_create_v1(&options,&domain)!=MGMEM_OK) return 0;
    std::memset(external_data,0x5a,sizeof(external_data));
    mgmem_external_buffer_checked_v1 source{};
    source.struct_size=sizeof(source);source.abi_major=MGMEM_ABI_MAJOR;source.abi_minor=MGMEM_ABI_MINOR;
    source.data=external_data;source.capacity_bytes=sizeof(external_data);source.alignment=1;
    source.external_id=1;source.access=MGMEM_READ;source.retain=&retainExternal;
    source.release_checked=&failExternalRelease;source.managing_runtime_origin=mgmem_runtime_origin_v1();
    mgmem_handle_v1 owner{};
    if (mgmem_import_external_checked_v1(domain,&source,&owner)!=MGMEM_OK) return 0;
    const mgmem_domain_lifetime_v1 life{sizeof(life),1,nullptr,&retain,&release};
    uint64_t id=0;
    if (mgmem_android_borrow_register_v1(domain,&life,owner,4,16,MGMEM_READ,
            MGMEM_ANDROID_BORROW_INITIALIZED,&id)!=MGMEM_OK) return 0;
    if (mgmem_release_v1(domain,owner)!=MGMEM_OK || mgmem_domain_begin_close_v1(domain)!=MGMEM_OK) return 0;
    return static_cast<jlong>(id);
}

extern "C" JNIEXPORT jboolean JNICALL
Java_io_github_jichuo1_meguviz_memory_android_NativeMemoryLeaseJvmTest_quarantined(JNIEnv *,jclass) {
    if (!domain || pins!=2) return JNI_FALSE;
    mgmem_snapshot_v1 snapshot{};mgmem_import_snapshot_v1 imports{};
    if (mgmem_domain_snapshot_v1(domain,&snapshot,sizeof(snapshot))!=MGMEM_OK ||
        mgmem_domain_import_snapshot_v1(domain,&imports,sizeof(imports))!=MGMEM_OK) return JNI_FALSE;
    return snapshot.state==MGMEM_CLOSING && snapshot.backing_bytes==sizeof(external_data) &&
        snapshot.active_leases==0 && imports.poisoned==1 && imports.external_backing_bytes==sizeof(external_data) &&
        mgmem_domain_collect_v1(domain)==MGMEM_PROVIDER_ERROR &&
        mgmem_domain_drain_v1(domain,0)==MGMEM_PROVIDER_ERROR &&
        mgmem_domain_destroy_v1(domain)==MGMEM_BUSY ? JNI_TRUE : JNI_FALSE;
}

extern "C" JNIEXPORT jboolean JNICALL
Java_io_github_jichuo1_meguviz_memory_android_NativeMemoryLeaseJvmTest_finish(JNIEnv *,jclass) {
    if (!domain || pins!=1 || mgmem_domain_collect_v1(domain)!=MGMEM_OK ||
        mgmem_domain_drain_v1(domain,0)!=MGMEM_OK || mgmem_domain_destroy_v1(domain)!=MGMEM_OK) return JNI_FALSE;
    domain=nullptr;return JNI_TRUE;
}
