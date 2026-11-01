// Test-only build of the real borrowing adapter with one intercepted C call.
// Production builds compile the unmodified adapter directly.
#define mgmem_borrow_release_v1 managed_test_borrow_release_v1
#define Java_io_github_jichuo1_meguviz_memory_android_NativeMemoryLease_nativeClaim managed_test_real_nativeClaim
#define Java_io_github_jichuo1_meguviz_memory_android_NativeMemoryLease_nativeClose managed_test_real_nativeClose
#include "../../adapters/android/native/borrow_jni.cpp"
