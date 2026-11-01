#include <jni.h>

#define BACKEND(method) Java_io_github_jichuo1_meguviz_memory_android_NativePressureBackend_##method

extern "C" JNIEXPORT jlong JNICALL BACKEND(nativeCreate)(JNIEnv *, jclass, jlong, jlong, jlong, jlong, jobject, jlongArray) {
    return 1;
}
extern "C" JNIEXPORT jint JNICALL BACKEND(nativeBeginClose)(JNIEnv *env, jclass, jlong) {
    jclass test = env->FindClass("NativeBackendConcurrencyTest");
    if (!test) return 9;
    jmethodID callback = env->GetStaticMethodID(test, "onNativeBeginClose", "()I");
    if (!callback) return 9;
    return env->CallStaticIntMethod(test, callback);
}
extern "C" JNIEXPORT void JNICALL BACKEND(nativePollCompletion)(JNIEnv *, jclass, jlong) {}
extern "C" JNIEXPORT jint JNICALL BACKEND(nativeNotify)(JNIEnv *, jclass, jlong, jint) { return 0; }
