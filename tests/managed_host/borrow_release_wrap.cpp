#include <mgmem/android_borrow_jni.h>
#include <jni.h>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <mutex>

namespace {
std::atomic<bool> fail_next{false}, checked_next{false}, fail_claim{false},
    fail_cancel_tail{false}, checked_close_tail{false}, checked_owner_tail{false}, checked_allocate_tail{false};
JavaVM *vm = nullptr;
struct Gate { std::mutex mutex; std::condition_variable changed; bool armed=false, entered=false, released=false, timed_out=false; };
Gate close_tail, cancel_tail, owner_tail, allocate_tail, state_tail;
Gate *gate_for(jint kind) { return kind == 1 ? &close_tail : kind == 2 ? &cancel_tail : kind == 3 ? &owner_tail : kind == 4 ? &allocate_tail : kind == 5 ? &state_tail : nullptr; }
void wait_gate(Gate &gate) {
    std::unique_lock<std::mutex> lock(gate.mutex);
    if (!gate.armed) return;
    gate.armed = false; gate.entered = true; gate.changed.notify_all();
    if (!gate.changed.wait_for(lock, std::chrono::seconds(10), [&] { return gate.released; })) gate.timed_out = true;
    gate.armed = false;
}
void throw_java(JNIEnv *env, bool checked) {
    jclass type = env->FindClass(checked ? "java/io/IOException" : "java/lang/IllegalStateException");
    if (type && !env->ExceptionCheck()) env->ThrowNew(type, "managed adapter test exception");
    if (type) env->DeleteLocalRef(type);
}
}

extern "C" JNIEXPORT jint JNICALL JNI_OnLoad(JavaVM *value, void *) {
    vm = value;
    return JNI_VERSION_1_6;
}

extern "C" mgmem_status managed_test_borrow_release_v1(mgmem_domain *domain, mgmem_handle_v1 lease) {
    const auto status = mgmem_borrow_release_v1(domain, lease);
    if (status == MGMEM_OK && checked_next.exchange(false) && vm) {
        JNIEnv *env = nullptr;
        if (vm->GetEnv(reinterpret_cast<void **>(&env), JNI_VERSION_1_6) == JNI_OK && env) {
            jclass type = env->FindClass("java/io/IOException");
            if (type && !env->ExceptionCheck()) env->ThrowNew(type, "managed test checked exception");
            if (type) env->DeleteLocalRef(type);
        }
    }
    // Simulate an uncertain failure after the underlying core consumed the
    // handle. The borrowing adapter must not retry or drop its lifetime pin.
    return status == MGMEM_OK && fail_next.exchange(false) ? MGMEM_PROVIDER_ERROR : status;
}

extern "C" JNIEXPORT void JNICALL
Java_io_github_jichuo1_meguviz_memory_android_ManagedMemoryDomainJvmTest_nativeFailBorrowRelease(JNIEnv *, jclass) {
    fail_next.store(true);
}

extern "C" JNIEXPORT void JNICALL
Java_io_github_jichuo1_meguviz_memory_android_ManagedMemoryDomainJvmTest_nativeCheckedBorrowRelease(JNIEnv *, jclass) {
    checked_next.store(true);
}

extern "C" jlong JNICALL managed_test_real_nativeClaim(JNIEnv *, jclass, jlong);
extern "C" jint JNICALL managed_test_real_nativeClose(JNIEnv *, jclass, jlong);

extern "C" JNIEXPORT jlong JNICALL
Java_io_github_jichuo1_meguviz_memory_android_NativeMemoryLease_nativeClaim(JNIEnv *env, jclass type, jlong token) {
    if (fail_claim.exchange(false)) { throw_java(env, false); return 0; }
    return managed_test_real_nativeClaim(env, type, token);
}

extern "C" JNIEXPORT jint JNICALL
Java_io_github_jichuo1_meguviz_memory_android_NativeMemoryLease_nativeClose(JNIEnv *env, jclass type, jlong token) {
    const auto status = managed_test_real_nativeClose(env, type, token);
    if (env->ExceptionCheck()) return status;
    try { wait_gate(close_tail); } catch (...) { return MGMEM_PROVIDER_ERROR; }
    if (status == MGMEM_OK && checked_close_tail.exchange(false)) throw_java(env, true);
    return status;
}

extern "C" jint JNICALL managed_test_real_managedRelease(JNIEnv *, jclass, jlong);
extern "C" jlong JNICALL managed_test_real_managedAllocate(JNIEnv *, jclass, jlong, jlong, jlong);
extern "C" JNIEXPORT jint JNICALL
Java_io_github_jichuo1_meguviz_memory_android_ManagedMemoryDomain_nativeRelease(JNIEnv *env, jclass type, jlong token) {
    const auto status = managed_test_real_managedRelease(env, type, token);
    if (env->ExceptionCheck()) return status;
    try { wait_gate(owner_tail); } catch (...) { return MGMEM_PROVIDER_ERROR; }
    if (checked_owner_tail.exchange(false)) throw_java(env, true);
    return status;
}
extern "C" JNIEXPORT jlong JNICALL
Java_io_github_jichuo1_meguviz_memory_android_ManagedMemoryDomain_nativeAllocate(JNIEnv *env, jclass type, jlong token, jlong size, jlong align) {
    const auto result = managed_test_real_managedAllocate(env, type, token, size, align);
    if (env->ExceptionCheck()) return result;
    try { wait_gate(allocate_tail); } catch (...) { return -static_cast<jlong>(MGMEM_PROVIDER_ERROR); }
    if (checked_allocate_tail.exchange(false)) throw_java(env, true);
    return result;
}

extern "C" jint JNICALL managed_test_real_managedState(JNIEnv *, jclass, jlong);
extern "C" JNIEXPORT jint JNICALL
Java_io_github_jichuo1_meguviz_memory_android_ManagedMemoryDomain_nativeState(JNIEnv *env, jclass type, jlong token) {
    const auto status = managed_test_real_managedState(env, type, token);
    if (env->ExceptionCheck()) return status;
    try { wait_gate(state_tail); } catch (...) { return MGMEM_PROVIDER_ERROR; }
    return status;
}

extern "C" mgmem_status managed_test_borrow_cancel_v1(uint64_t token) {
    const auto status = mgmem_android_borrow_cancel_v1(token);
    try { wait_gate(cancel_tail); } catch (...) { return MGMEM_PROVIDER_ERROR; }
    return status == MGMEM_OK && fail_cancel_tail.exchange(false) ? MGMEM_PROVIDER_ERROR : status;
}

#define TEST_NAME(method) Java_io_github_jichuo1_meguviz_memory_android_ManagedMemoryDomainJvmTest_##method
extern "C" JNIEXPORT void JNICALL TEST_NAME(nativeBorrowInject)(JNIEnv *, jclass, jint kind) {
    if (kind == 1) fail_claim.store(true);
    else if (kind == 2) fail_cancel_tail.store(true);
    else if (kind == 3) checked_close_tail.store(true);
    else if (kind == 4) checked_owner_tail.store(true);
    else if (kind == 5) checked_allocate_tail.store(true);
}
extern "C" JNIEXPORT void JNICALL TEST_NAME(nativeArmBorrowGate)(JNIEnv *, jclass, jint kind) {
    try {
        auto *gate = gate_for(kind); if (!gate) return;
        std::lock_guard<std::mutex> lock(gate->mutex);
        gate->armed=true;gate->entered=false;gate->released=false;gate->timed_out=false;
    } catch (...) {}
}
extern "C" JNIEXPORT jboolean JNICALL TEST_NAME(nativeWaitBorrowGate)(JNIEnv *, jclass, jint kind) {
    try {
        auto *gate=gate_for(kind);if(!gate)return JNI_FALSE;
        std::unique_lock<std::mutex> lock(gate->mutex);
        return gate->changed.wait_for(lock,std::chrono::seconds(5),[&]{return gate->entered;})?JNI_TRUE:JNI_FALSE;
    } catch (...) { return JNI_FALSE; }
}
extern "C" JNIEXPORT jboolean JNICALL TEST_NAME(nativeReleaseBorrowGate)(JNIEnv *, jclass, jint kind) {
    try {
        auto *gate=gate_for(kind);if(!gate)return JNI_FALSE;
        std::lock_guard<std::mutex> lock(gate->mutex);
        gate->released=true;gate->changed.notify_all();return gate->entered&&!gate->timed_out?JNI_TRUE:JNI_FALSE;
    } catch (...) { return JNI_FALSE; }
}
