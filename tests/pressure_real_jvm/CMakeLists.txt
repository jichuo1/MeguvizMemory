cmake_minimum_required(VERSION 3.25)
project(PressureRealJvmHost LANGUAGES CXX)
find_package(JNI REQUIRED)
find_package(Java REQUIRED COMPONENTS Runtime Development)
find_package(Threads REQUIRED)
get_filename_component(ROOT "${CMAKE_CURRENT_LIST_DIR}/../.." ABSOLUTE)

# Host-only composition: one shared library contains the real core, pressure
# controller, and JNI code. It is not an Android AAR or loader-closure test.
add_library(mgmem_pressure_jni SHARED "${ROOT}/src/mgmem.cpp"
  "${ROOT}/adapters/android/native/pressure.cpp"
  "${ROOT}/adapters/android/native/pressure_jni.cpp")
target_compile_features(mgmem_pressure_jni PRIVATE cxx_std_17)
target_include_directories(mgmem_pressure_jni PRIVATE "${ROOT}/include" ${JNI_INCLUDE_DIRS})
target_link_libraries(mgmem_pressure_jni PRIVATE Threads::Threads)

set(JAVA_ROOT "${ROOT}/adapters/android/java/io/github/jichuo1/meguviz/memory/android")
set(STUB_ROOT "${ROOT}/tests/managed_host/android_stub")
set(CLASSES "${CMAKE_CURRENT_BINARY_DIR}/classes")
set(SOURCES "${JAVA_ROOT}/NativePressureBackend.java" "${JAVA_ROOT}/MemoryPressureAdapter.java"
  "${CMAKE_CURRENT_LIST_DIR}/RealPressureCloseJvmTest.java"
  "${STUB_ROOT}/android/content/ComponentCallbacks2.java" "${STUB_ROOT}/android/content/Context.java"
  "${STUB_ROOT}/android/content/res/Configuration.java"
  "${ROOT}/tests/android_borrow/host_stub/android/os/Looper.java")
add_custom_command(OUTPUT "${CLASSES}/RealPressureCloseJvmTest.class"
  COMMAND ${CMAKE_COMMAND} -E make_directory "${CLASSES}"
  COMMAND ${Java_JAVAC_EXECUTABLE} --release 8 -d "${CLASSES}" ${SOURCES}
  DEPENDS ${SOURCES} VERBATIM)
add_custom_target(real_pressure_jvm_classes ALL DEPENDS "${CLASSES}/RealPressureCloseJvmTest.class")
enable_testing()
add_test(NAME pressure_real_jvm_close COMMAND ${Java_JAVA_EXECUTABLE}
  "-Djava.library.path=$<TARGET_FILE_DIR:mgmem_pressure_jni>" -cp "${CLASSES}" RealPressureCloseJvmTest)
set_tests_properties(pressure_real_jvm_close PROPERTIES TIMEOUT 60)
