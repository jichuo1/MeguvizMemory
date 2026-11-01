cmake_minimum_required(VERSION 3.25)
project(PressureBackendCloseHost LANGUAGES CXX)
find_package(JNI REQUIRED)
find_package(Java REQUIRED COMPONENTS Runtime Development)
get_filename_component(ROOT "${CMAKE_CURRENT_LIST_DIR}/../.." ABSOLUTE)
add_library(mgmem_pressure_jni SHARED fake_pressure_jni.cpp)
target_include_directories(mgmem_pressure_jni PRIVATE ${JNI_INCLUDE_DIRS})
set(JAVA_ROOT "${ROOT}/adapters/android/java/io/github/jichuo1/meguviz/memory/android")
set(STUB_ROOT "${ROOT}/tests/managed_host/android_stub")
set(CLASSES "${CMAKE_CURRENT_BINARY_DIR}/classes")
set(SOURCES "${JAVA_ROOT}/NativePressureBackend.java" "${JAVA_ROOT}/MemoryPressureAdapter.java"
  "${ROOT}/tests/android_pressure_close/NativeBackendConcurrencyTest.java"
  "${STUB_ROOT}/android/content/ComponentCallbacks2.java" "${STUB_ROOT}/android/content/Context.java"
  "${STUB_ROOT}/android/content/res/Configuration.java"
  "${ROOT}/tests/android_borrow/host_stub/android/os/Looper.java")
add_custom_command(OUTPUT "${CLASSES}/NativeBackendConcurrencyTest.class"
  COMMAND ${CMAKE_COMMAND} -E make_directory "${CLASSES}"
  COMMAND ${Java_JAVAC_EXECUTABLE} --release 8 -d "${CLASSES}" ${SOURCES}
  DEPENDS ${SOURCES} VERBATIM)
add_custom_target(pressure_backend_classes ALL DEPENDS "${CLASSES}/NativeBackendConcurrencyTest.class")
enable_testing()
add_test(NAME pressure_native_backend_close COMMAND ${Java_JAVA_EXECUTABLE}
  "-Djava.library.path=$<TARGET_FILE_DIR:mgmem_pressure_jni>" -cp "${CLASSES}" NativeBackendConcurrencyTest)
set_tests_properties(pressure_native_backend_close PROPERTIES TIMEOUT 30)
