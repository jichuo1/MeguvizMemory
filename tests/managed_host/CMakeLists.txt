cmake_minimum_required(VERSION 3.25)
project(MeguvizMemoryManagedHostDevelopment LANGUAGES CXX)
find_package(JNI REQUIRED)
find_package(Java REQUIRED COMPONENTS Runtime Development)
find_package(Threads REQUIRED)
get_filename_component(ROOT "${CMAKE_CURRENT_LIST_DIR}/../.." ABSOLUTE)

# Test-only single DLL. Production must link the existing shared core instead.
add_library(mgmem_pressure_jni SHARED
  "${ROOT}/src/mgmem.cpp" "${ROOT}/tests/managed_host/borrow_test_wrapped.cpp"
  "${ROOT}/tests/managed_host/borrow_release_wrap.cpp"
  "${ROOT}/tests/managed_host/workspace_test_wrapped.cpp")
target_compile_features(mgmem_pressure_jni PRIVATE cxx_std_17)
target_compile_definitions(mgmem_pressure_jni PRIVATE MGMEM_MANAGED_TESTING)
target_include_directories(mgmem_pressure_jni PRIVATE "${ROOT}/include" ${JNI_INCLUDE_DIRS})
target_link_libraries(mgmem_pressure_jni PRIVATE Threads::Threads)
if(MSVC)
  target_compile_options(mgmem_pressure_jni PRIVATE /W4 /permissive- /utf-8)
else()
  target_compile_options(mgmem_pressure_jni PRIVATE -Wall -Wextra -Wpedantic)
endif()

set(JAVA_CLASSES "${CMAKE_CURRENT_BINARY_DIR}/classes")
set(JAVA_SOURCE "${ROOT}/adapters/android/java/io/github/jichuo1/meguviz/memory/android")
set(TEST_JAVA "${ROOT}/tests/managed_host/ManagedMemoryDomainJvmTest.java")
add_custom_command(OUTPUT "${JAVA_CLASSES}/io/github/jichuo1/meguviz/memory/android/ManagedMemoryDomainJvmTest.class"
  COMMAND ${CMAKE_COMMAND} -E make_directory "${JAVA_CLASSES}"
  COMMAND ${Java_JAVAC_EXECUTABLE} --release 8 -d "${JAVA_CLASSES}"
    "${JAVA_SOURCE}/NativeMemoryLease.java" "${JAVA_SOURCE}/ManagedMemoryDomain.java" "${TEST_JAVA}"
  DEPENDS "${JAVA_SOURCE}/NativeMemoryLease.java" "${JAVA_SOURCE}/ManagedMemoryDomain.java" "${TEST_JAVA}"
  VERBATIM)
add_custom_target(managed_java_classes ALL DEPENDS
  "${JAVA_CLASSES}/io/github/jichuo1/meguviz/memory/android/ManagedMemoryDomainJvmTest.class")
enable_testing()
foreach(case normal arena worker-retry race-allocate race-published race-borrow race-pin forget-state forget-failure
             forget-close drop-retry drop-exception unknown-create unknown-allocate unknown-borrow
             unknown-registration lease-release-failure quarantine-jni-failure java-only-quarantine checked-create checked-claim
             checked-allocate checked-borrow checked-release checked-quarantine checked-lease-close
             guard-admission-retry guard-enter-unknown guard-exit-delay guard-exit-failure guard-underflow
             java-close-tail java-close-tail-error observed-closed observed-stale-state owner-tail owner-tail-error allocate-tail allocate-tail-error
             adapter-close-tail adapter-close-tail-error adapter-cancel-tail adapter-cancel-tail-error
             destroy-unknown worker-cap unforgotten-exit)
  add_test(NAME managed_${case} COMMAND ${Java_JAVA_EXECUTABLE}
    "-Djava.library.path=$<TARGET_FILE_DIR:mgmem_pressure_jni>" -cp "${JAVA_CLASSES}"
    io.github.jichuo1.meguviz.memory.android.ManagedMemoryDomainJvmTest ${case})
  set_tests_properties(managed_${case} PROPERTIES TIMEOUT 30)
endforeach()

# Separate host JNI build with no fault hooks. Production still links its shared core.
add_library(mgmem_managed_plain SHARED "${ROOT}/src/mgmem.cpp"
  "${ROOT}/adapters/android/native/borrow_jni.cpp" "${ROOT}/adapters/android/native/workspace_jni.cpp")
target_compile_features(mgmem_managed_plain PRIVATE cxx_std_17)
target_include_directories(mgmem_managed_plain PRIVATE "${ROOT}/include" ${JNI_INCLUDE_DIRS})
target_link_libraries(mgmem_managed_plain PRIVATE Threads::Threads)
set_target_properties(mgmem_managed_plain PROPERTIES OUTPUT_NAME mgmem_pressure_jni
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/plain" LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/plain" ARCHIVE_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/plain")
set(PLAIN_JAVA "${ROOT}/tests/managed_host/ManagedMemoryDomainPlainJvmTest.java")
add_custom_command(OUTPUT "${JAVA_CLASSES}/io/github/jichuo1/meguviz/memory/android/ManagedMemoryDomainPlainJvmTest.class"
  COMMAND ${Java_JAVAC_EXECUTABLE} --release 8 -cp "${JAVA_CLASSES}" -d "${JAVA_CLASSES}" "${PLAIN_JAVA}"
  DEPENDS managed_java_classes "${PLAIN_JAVA}" VERBATIM)
add_custom_target(managed_plain_classes ALL DEPENDS "${JAVA_CLASSES}/io/github/jichuo1/meguviz/memory/android/ManagedMemoryDomainPlainJvmTest.class")
add_test(NAME managed_plain COMMAND ${Java_JAVA_EXECUTABLE}
  "-Djava.library.path=$<TARGET_FILE_DIR:mgmem_managed_plain>" -cp "${JAVA_CLASSES}"
  io.github.jichuo1.meguviz.memory.android.ManagedMemoryDomainPlainJvmTest)
set_tests_properties(managed_plain PROPERTIES TIMEOUT 30)
