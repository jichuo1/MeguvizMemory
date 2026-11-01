#include <mgmem/android_borrow_jni.h>
#include <jni.h>
#include <cstdint>
#include <string>
#if defined(_WIN32)
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#else
#include <dlfcn.h>
#endif

// Optional OS module identity probe only; no domain, owner or lease state.
#define JNI_NAME(method) Java_io_github_jichuo1_meguviz_memory_image_JavaImage_##method

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
