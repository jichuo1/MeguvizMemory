#include <mgmem/android_borrow_jni.h>
#include <jni.h>

#include <atomic>
#include <cstdint>
#include <cstring>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <new>
#include <string>
#if defined(_WIN32)
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#else
#include <dlfcn.h>
#endif

namespace {
using u64 = uint64_t;
constexpr u64 max_token = static_cast<u64>(std::numeric_limits<jlong>::max());
constexpr u64 max_capacity = 24u * 1024u * 1024u;
constexpr u64 max_budget = 64u * 1024u * 1024u;

struct Session {
    std::mutex mutex;
    std::atomic<unsigned> pins{1};
    mgmem_domain *domain = nullptr;
    mgmem_handle_v1 owner{};
    u64 bytes = 0;
    bool owner_live = false;
    bool registration_issued = false;
    bool retired = false;
    bool quarantined = false;
};

std::mutex registry_mutex;
std::map<u64, std::shared_ptr<Session>> registry;
u64 next_id = 1;

std::shared_ptr<Session> lookup(jlong id) {
    if (id <= 0) return nullptr;
    std::lock_guard<std::mutex> lock(registry_mutex);
    const auto found = registry.find(static_cast<u64>(id));
    return found == registry.end() ? nullptr : found->second;
}
void erase(u64 id, const std::shared_ptr<Session> &s) {
    std::lock_guard<std::mutex> lock(registry_mutex);
    const auto found = registry.find(id);
    if (found != registry.end() && found->second == s) registry.erase(found);
}
void retain(void *context) noexcept { static_cast<Session *>(context)->pins.fetch_add(1, std::memory_order_relaxed); }
void release(void *context) noexcept { static_cast<Session *>(context)->pins.fetch_sub(1, std::memory_order_release); }

// Caller holds Session::mutex. Never retry a potentially consumed owner or
// force destruction when a lease, failed release, or pending operation exists.
mgmem_status finish(Session &s, u64 &peak) {
    if (!s.domain) return MGMEM_STALE_HANDLE;
    if (s.quarantined) return MGMEM_PROVIDER_ERROR;
    if (s.pins.load(std::memory_order_acquire) != 1) return MGMEM_BUSY;
    if (s.owner_live) {
        s.owner_live = false;
        const auto status = mgmem_release_v1(s.domain, s.owner);
        s.owner = {};
        if (status != MGMEM_OK) { s.quarantined = true; return status; }
    }
    auto status = mgmem_domain_begin_close_v1(s.domain);
    if (status != MGMEM_OK) { s.quarantined = true; return status; }
    status = mgmem_domain_collect_v1(s.domain);
    if (status != MGMEM_OK) return status;
    status = mgmem_domain_drain_v1(s.domain, 0);
    if (status != MGMEM_OK) return status;
    mgmem_snapshot_v1 snapshot{};
    status = mgmem_domain_snapshot_v1(s.domain, &snapshot, sizeof(snapshot));
    if (status != MGMEM_OK) return status;
    if (snapshot.state != MGMEM_CLOSED || snapshot.backing_bytes || snapshot.reserved_bytes ||
        snapshot.live_bytes || snapshot.retiring_bytes || snapshot.active_allocations || snapshot.active_leases)
        return MGMEM_PROVIDER_ERROR;
    peak = snapshot.peak_backing_bytes;
    status = mgmem_domain_destroy_v1(s.domain);
    if (status == MGMEM_OK) s.domain = nullptr;
    else if (status != MGMEM_BUSY) { s.domain = nullptr; s.quarantined = true; }
    return status;
}

jlong failure(mgmem_status status) { return -static_cast<jlong>(status ? status : MGMEM_PROVIDER_ERROR); }
}

#define JNI_NAME(method) Java_io_github_jichuo1_meguviz_memory_image_JavaImage_##method

extern "C" JNIEXPORT jlong JNICALL JNI_NAME(nativeOpen)(JNIEnv *, jclass, jlong count, jlong budget, jboolean system) {
    if (count <= 0 || static_cast<u64>(count) > max_capacity || budget <= 0 || static_cast<u64>(budget) > max_budget)
        return failure(MGMEM_INVALID_ARGUMENT);
    try {
        auto s = std::make_shared<Session>();
        u64 id = 0;
        {
            std::lock_guard<std::mutex> lock(registry_mutex);
            if (next_id > max_token) return failure(MGMEM_OVERFLOW);
            id = next_id++;
            registry.emplace(id, s);
        }
        auto discard = [&](mgmem_status status) -> jlong {
            u64 ignored = 0;
            if (!s->domain || finish(*s, ignored) == MGMEM_OK) erase(id, s);
            return failure(status);
        };
        const mgmem_domain_options_v1 options{sizeof(options), MGMEM_ABI_MAJOR, MGMEM_ABI_MINOR, 0,
            system ? MGMEM_SYSTEM : MGMEM_ARENA, 0, static_cast<u64>(budget), 65536, nullptr};
        auto status = mgmem_domain_create_v1(&options, &s->domain);
        if (status != MGMEM_OK) return discard(status);
        s->bytes = static_cast<u64>(count);
        status = mgmem_allocate_v1(s->domain, s->bytes, 64, &s->owner);
        if (status != MGMEM_OK) return discard(status);
        s->owner_live = true;
        mgmem_borrow_v1 init{};
        status = mgmem_borrow_acquire_v1(s->domain, s->owner, 0, s->bytes, MGMEM_WRITE, &init);
        if (status != MGMEM_OK) return discard(status);
        std::memset(init.data, 0, static_cast<size_t>(s->bytes));
        status = mgmem_borrow_release_v1(s->domain, init.lease);
        if (status != MGMEM_OK) { s->quarantined = true; return failure(status); }
        return static_cast<jlong>(id);
    } catch (const std::bad_alloc &) { return failure(MGMEM_OUT_OF_MEMORY); }
    catch (...) { return failure(MGMEM_PROVIDER_ERROR); }
}

extern "C" JNIEXPORT jlong JNICALL JNI_NAME(nativeRegister)(JNIEnv *, jclass, jlong id) {
    try {
        auto s = lookup(id);
        if (!s) return failure(MGMEM_STALE_HANDLE);
        std::lock_guard<std::mutex> lock(s->mutex);
        if (!s->domain || !s->owner_live || s->registration_issued || s->quarantined)
            return failure(MGMEM_INVALID_ARGUMENT);
        const mgmem_domain_lifetime_v1 life{sizeof(life), 1, s.get(), retain, release};
        u64 token = 0;
        const auto status = mgmem_android_borrow_register_v1(s->domain, &life, s->owner, 0, s->bytes,
            MGMEM_WRITE, MGMEM_ANDROID_BORROW_INITIALIZED, &token);
        if (status != MGMEM_OK) return failure(status);
        s->registration_issued = true;
        return static_cast<jlong>(token);
    } catch (...) { return failure(MGMEM_PROVIDER_ERROR); }
}

extern "C" JNIEXPORT jint JNICALL JNI_NAME(nativeRetire)(JNIEnv *, jclass, jlong id) {
    try {
        auto s = lookup(id);
        if (!s) return MGMEM_STALE_HANDLE;
        std::lock_guard<std::mutex> lock(s->mutex);
        if (!s->domain || !s->owner_live || !s->registration_issued || s->retired || s->quarantined)
            return MGMEM_INVALID_ARGUMENT;
        s->owner_live = false;
        auto status = mgmem_release_v1(s->domain, s->owner);
        s->owner = {};
        if (status != MGMEM_OK) { s->quarantined = true; return status; }
        status = mgmem_domain_begin_close_v1(s->domain);
        if (status != MGMEM_OK) { s->quarantined = true; return status; }
        s->retired = true;
        mgmem_snapshot_v1 snapshot{};
        status = mgmem_domain_snapshot_v1(s->domain, &snapshot, sizeof(snapshot));
        if (status != MGMEM_OK) return status;
        if (snapshot.state != MGMEM_CLOSING || snapshot.retiring_bytes != s->bytes ||
            snapshot.active_allocations != 1 || snapshot.active_leases != 1 ||
            mgmem_domain_collect_v1(s->domain) != MGMEM_BUSY ||
            mgmem_domain_drain_v1(s->domain, 0) != MGMEM_TIMEOUT)
            return MGMEM_PROVIDER_ERROR;
        return MGMEM_OK;
    } catch (...) { return MGMEM_PROVIDER_ERROR; }
}

extern "C" JNIEXPORT jint JNICALL JNI_NAME(nativeCancel)(JNIEnv *, jclass, jlong token) {
    try { return token > 0 ? mgmem_android_borrow_cancel_v1(static_cast<u64>(token)) : MGMEM_INVALID_ARGUMENT; }
    catch (...) { return MGMEM_PROVIDER_ERROR; }
}

extern "C" JNIEXPORT jlong JNICALL JNI_NAME(nativeFinish)(JNIEnv *, jclass, jlong id) {
    try {
        auto s = lookup(id);
        if (!s) return failure(MGMEM_STALE_HANDLE);
        u64 peak = 0;
        mgmem_status status;
        { std::lock_guard<std::mutex> lock(s->mutex); status = finish(*s, peak); }
        if (status != MGMEM_OK) return failure(status);
        erase(static_cast<u64>(id), s);
        if (peak > max_token) return failure(MGMEM_OVERFLOW);
        return static_cast<jlong>(peak);
    } catch (...) { return failure(MGMEM_PROVIDER_ERROR); }
}

extern "C" JNIEXPORT jint JNICALL JNI_NAME(nativeActiveSessions)(JNIEnv *, jclass) {
    try {
        std::lock_guard<std::mutex> lock(registry_mutex);
        return registry.size() > static_cast<size_t>(std::numeric_limits<jint>::max())
            ? -1 : static_cast<jint>(registry.size());
    } catch (...) { return -1; }
}

extern "C" JNIEXPORT jobjectArray JNICALL JNI_NAME(nativeModulePaths)(JNIEnv *env, jclass) {
    try {
#if defined(_WIN32)
        auto path_for = [](uintptr_t address, std::wstring &out) {
            HMODULE module = nullptr;
            if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                                    reinterpret_cast<LPCWSTR>(address), &module)) return false;
            wchar_t buffer[32768];
            const DWORD count = GetModuleFileNameW(module, buffer, static_cast<DWORD>(sizeof(buffer)/sizeof(buffer[0])));
            if (!count || count >= sizeof(buffer)/sizeof(buffer[0])) return false;
            out.assign(buffer, count); return true;
        };
        std::wstring core, jni;
        if (!path_for(reinterpret_cast<uintptr_t>(&mgmem_abi_version_v1), core) ||
            !path_for(reinterpret_cast<uintptr_t>(&mgmem_android_borrow_register_v1), jni)) return nullptr;
        jclass string_class = env->FindClass("java/lang/String");
        if (!string_class || env->ExceptionCheck()) return nullptr;
        jobjectArray result = env->NewObjectArray(2, string_class, nullptr);
        env->DeleteLocalRef(string_class);
        if (!result || env->ExceptionCheck()) return nullptr;
        const std::wstring *paths[] = {&core, &jni};
        for (jsize i = 0; i < 2; ++i) {
            jstring value = env->NewString(reinterpret_cast<const jchar *>(paths[i]->data()), static_cast<jsize>(paths[i]->size()));
            if (!value || env->ExceptionCheck()) return nullptr;
            env->SetObjectArrayElement(result, i, value);
            env->DeleteLocalRef(value);
            if (env->ExceptionCheck()) return nullptr;
        }
#else
        Dl_info core{}, jni{};
        if (!dladdr(reinterpret_cast<const void *>(&mgmem_abi_version_v1), &core) || !core.dli_fname ||
            !dladdr(reinterpret_cast<const void *>(&mgmem_android_borrow_register_v1), &jni) || !jni.dli_fname) return nullptr;
        jclass string_class = env->FindClass("java/lang/String");
        if (!string_class || env->ExceptionCheck()) return nullptr;
        jobjectArray result = env->NewObjectArray(2, string_class, nullptr);
        env->DeleteLocalRef(string_class);
        if (!result || env->ExceptionCheck()) return nullptr;
        const char *paths[] = {core.dli_fname, jni.dli_fname};
        for (jsize i = 0; i < 2; ++i) {
            jstring value = env->NewStringUTF(paths[i]);
            if (!value || env->ExceptionCheck()) return nullptr;
            env->SetObjectArrayElement(result, i, value);
            env->DeleteLocalRef(value);
            if (env->ExceptionCheck()) return nullptr;
        }
#endif
        return result;
    } catch (...) { return nullptr; }
}
