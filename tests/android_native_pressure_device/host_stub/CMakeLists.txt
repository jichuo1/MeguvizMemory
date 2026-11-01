cmake_minimum_required(VERSION 3.25)
project(NativePressureDeviceHostProbe LANGUAGES CXX)
find_package(JNI REQUIRED)
find_package(Java REQUIRED COMPONENTS Runtime Development)
find_package(Threads REQUIRED)
get_filename_component(ROOT "${CMAKE_CURRENT_LIST_DIR}/../../.." ABSOLUTE)
# Host-only single-DSO composition; not Android runtime or package qualification.
add_library(mgmem_pressure_jni SHARED "${ROOT}/src/mgmem.cpp"
  "${ROOT}/adapters/android/native/pressure.cpp" "${ROOT}/adapters/android/native/pressure_jni.cpp")
target_compile_features(mgmem_pressure_jni PRIVATE cxx_std_17)
target_include_directories(mgmem_pressure_jni PRIVATE "${ROOT}/include" ${JNI_INCLUDE_DIRS})
target_link_libraries(mgmem_pressure_jni PRIVATE Threads::Threads)
# A separate process loads only this fake JNI library for an incomplete path.
add_library(mgmem_pressure_jni_failure SHARED "${ROOT}/tests/android_pressure_close/fake_pressure_jni.cpp")
target_include_directories(mgmem_pressure_jni_failure PRIVATE ${JNI_INCLUDE_DIRS})
set_target_properties(mgmem_pressure_jni_failure PROPERTIES OUTPUT_NAME mgmem_pressure_jni
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/fake"
  LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/fake"
  ARCHIVE_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/fake")
set(JAVA_ROOT "${ROOT}/adapters/android/java/io/github/jichuo1/meguviz/memory/android")
set(STUB "${ROOT}/tests/managed_host/android_stub")
set(BORROW_STUB "${ROOT}/tests/android_borrow/host_stub")
set(CLASSES "${CMAKE_CURRENT_BINARY_DIR}/classes")
set(SOURCES "${CMAKE_CURRENT_LIST_DIR}/android/app/Instrumentation.java"
  "${BORROW_STUB}/android/os/Bundle.java" "${BORROW_STUB}/android/os/Looper.java"
  "${BORROW_STUB}/org/json/JSONObject.java" "${STUB}/android/content/Context.java"
  "${STUB}/android/content/ComponentCallbacks2.java" "${STUB}/android/content/res/Configuration.java"
  "${JAVA_ROOT}/NativePressureBackend.java" "${JAVA_ROOT}/MemoryPressureAdapter.java"
  "${ROOT}/tests/android_native_pressure_device/NativePressureCloseInstrumentation.java"
  "${CMAKE_CURRENT_LIST_DIR}/HostProbe.java" "${CMAKE_CURRENT_LIST_DIR}/HostIncompleteProbe.java")
add_custom_command(OUTPUT "${CLASSES}/HostProbe.class" "${CLASSES}/HostIncompleteProbe.class"
  COMMAND ${CMAKE_COMMAND} -E make_directory "${CLASSES}"
  COMMAND ${Java_JAVAC_EXECUTABLE} --release 8 -d "${CLASSES}" ${SOURCES}
  DEPENDS ${SOURCES} VERBATIM)
add_custom_target(native_pressure_probe_classes ALL DEPENDS "${CLASSES}/HostProbe.class" "${CLASSES}/HostIncompleteProbe.class")
enable_testing()
add_test(NAME android_native_pressure_probe_host COMMAND ${Java_JAVA_EXECUTABLE}
  "-Djava.library.path=$<TARGET_FILE_DIR:mgmem_pressure_jni>" -cp "${CLASSES}" HostProbe)
add_test(NAME android_native_pressure_probe_incomplete_host COMMAND ${Java_JAVA_EXECUTABLE}
  "-Djava.library.path=$<TARGET_FILE_DIR:mgmem_pressure_jni_failure>" -cp "${CLASSES}" HostIncompleteProbe)
set_tests_properties(android_native_pressure_probe_host android_native_pressure_probe_incomplete_host PROPERTIES TIMEOUT 60)
