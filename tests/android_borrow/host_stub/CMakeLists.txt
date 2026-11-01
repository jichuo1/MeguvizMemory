cmake_minimum_required(VERSION 3.25)
project(MeguvizMemoryAndroidBorrowHostProbe LANGUAGES CXX)

# Test-only host fixture: one DLL holds the real core, adapter and test JNI.
# A name-only companion DLL satisfies System.loadLibrary in the Java class.
# It is not the Android AAR, a production runtime closure or device evidence.
find_package(JNI REQUIRED)
find_package(Java REQUIRED COMPONENTS Runtime Development)
find_package(Python3 REQUIRED COMPONENTS Interpreter)
find_package(Threads REQUIRED)
get_filename_component(ROOT "${CMAKE_CURRENT_LIST_DIR}/../../.." ABSOLUTE)

add_library(mgmem_borrow_fixture SHARED
  "${ROOT}/src/mgmem.cpp" "${ROOT}/adapters/android/native/borrow_jni.cpp"
  "${ROOT}/tests/android_borrow/borrow_fixture.cpp")
target_compile_features(mgmem_borrow_fixture PRIVATE cxx_std_17)
target_include_directories(mgmem_borrow_fixture PRIVATE "${ROOT}/include" ${JNI_INCLUDE_DIRS})
target_link_libraries(mgmem_borrow_fixture PRIVATE Threads::Threads)
add_library(mgmem_pressure_jni SHARED stub.cpp)

set(JAVA_CLASSES "${CMAKE_CURRENT_BINARY_DIR}/classes")
set(JAVA_SOURCES
  "${CMAKE_CURRENT_SOURCE_DIR}/android/os/Bundle.java"
  "${CMAKE_CURRENT_SOURCE_DIR}/android/os/Looper.java"
  "${CMAKE_CURRENT_SOURCE_DIR}/android/app/Instrumentation.java"
  "${CMAKE_CURRENT_SOURCE_DIR}/org/json/JSONObject.java"
  "${CMAKE_CURRENT_SOURCE_DIR}/HostProbe.java"
  "${ROOT}/adapters/android/java/io/github/jichuo1/meguviz/memory/android/NativeMemoryLease.java"
  "${ROOT}/tests/android_borrow/NativeBorrowInstrumentation.java")
add_custom_command(OUTPUT "${JAVA_CLASSES}/HostProbe.class"
  COMMAND ${CMAKE_COMMAND} -E make_directory "${JAVA_CLASSES}"
  COMMAND ${Java_JAVAC_EXECUTABLE} --release 8 -d "${JAVA_CLASSES}" ${JAVA_SOURCES}
  DEPENDS ${JAVA_SOURCES} VERBATIM)
add_custom_target(borrow_probe_java_classes ALL DEPENDS "${JAVA_CLASSES}/HostProbe.class")

enable_testing()
add_test(NAME android_borrow_v2_host_stub COMMAND ${Python3_EXECUTABLE}
  "${CMAKE_CURRENT_SOURCE_DIR}/run_host_probe.py" --java "${Java_JAVA_EXECUTABLE}"
  --classes "${JAVA_CLASSES}" --libraries "$<TARGET_FILE_DIR:mgmem_borrow_fixture>")
set_tests_properties(android_borrow_v2_host_stub PROPERTIES TIMEOUT 30)
