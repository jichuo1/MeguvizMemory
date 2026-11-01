cmake_minimum_required(VERSION 3.25)
project(MeguvizMemoryBorrowHostTests LANGUAGES CXX)
find_package(JNI REQUIRED)
find_package(Java REQUIRED COMPONENTS Runtime Development)
find_package(Threads REQUIRED)
get_filename_component(ROOT "${CMAKE_CURRENT_LIST_DIR}/../.." ABSOLUTE)
add_executable(mgmem_borrow_jni_host_tests
  "${ROOT}/src/mgmem.cpp" "${ROOT}/adapters/android/native/borrow_jni.cpp"
  "${ROOT}/tests/borrow_jni_host_tests.cpp")
target_compile_features(mgmem_borrow_jni_host_tests PRIVATE cxx_std_17)
target_compile_definitions(mgmem_borrow_jni_host_tests PRIVATE MGMEM_BORROW_TESTING)
target_include_directories(mgmem_borrow_jni_host_tests PRIVATE "${ROOT}/include" ${JNI_INCLUDE_DIRS})
target_link_libraries(mgmem_borrow_jni_host_tests PRIVATE Threads::Threads)
if(MSVC)
  target_compile_options(mgmem_borrow_jni_host_tests PRIVATE /W4 /permissive- /utf-8)
else()
  target_compile_options(mgmem_borrow_jni_host_tests PRIVATE -Wall -Wextra -Wpedantic)
endif()
enable_testing()
add_test(NAME borrow_jni_host COMMAND mgmem_borrow_jni_host_tests)
set_tests_properties(borrow_jni_host PROPERTIES TIMEOUT 30)

add_library(mgmem_pressure_jni SHARED "${ROOT}/src/mgmem.cpp"
  "${ROOT}/adapters/android/native/borrow_jni.cpp" "${ROOT}/tests/borrow_jni_java_fixture.cpp")
target_compile_features(mgmem_pressure_jni PRIVATE cxx_std_17)
target_include_directories(mgmem_pressure_jni PRIVATE "${ROOT}/include" ${JNI_INCLUDE_DIRS})
target_link_libraries(mgmem_pressure_jni PRIVATE Threads::Threads)
set(JAVA_CLASSES "${CMAKE_CURRENT_BINARY_DIR}/classes")
add_custom_command(OUTPUT "${JAVA_CLASSES}/io/github/jichuo1/meguviz/memory/android/NativeMemoryLeaseJvmTest.class"
  COMMAND ${CMAKE_COMMAND} -E make_directory "${JAVA_CLASSES}"
  COMMAND ${Java_JAVAC_EXECUTABLE} -d "${JAVA_CLASSES}"
    "${ROOT}/adapters/android/java/io/github/jichuo1/meguviz/memory/android/NativeMemoryLease.java"
    "${ROOT}/tests/NativeMemoryLeaseJvmTest.java"
  DEPENDS "${ROOT}/adapters/android/java/io/github/jichuo1/meguviz/memory/android/NativeMemoryLease.java"
          "${ROOT}/tests/NativeMemoryLeaseJvmTest.java")
add_custom_target(borrow_jni_java_classes ALL DEPENDS
  "${JAVA_CLASSES}/io/github/jichuo1/meguviz/memory/android/NativeMemoryLeaseJvmTest.class")
add_test(NAME borrow_jni_real_jvm COMMAND ${Java_JAVA_EXECUTABLE}
  "-Djava.library.path=$<TARGET_FILE_DIR:mgmem_pressure_jni>" -cp "${JAVA_CLASSES}"
  io.github.jichuo1.meguviz.memory.android.NativeMemoryLeaseJvmTest)
set_tests_properties(borrow_jni_real_jvm PROPERTIES TIMEOUT 30)
