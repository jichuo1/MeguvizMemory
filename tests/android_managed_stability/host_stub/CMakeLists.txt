cmake_minimum_required(VERSION 3.25)
project(MeguvizManagedStabilityHost LANGUAGES CXX)
find_package(JNI REQUIRED)
find_package(Java REQUIRED COMPONENTS Runtime Development)
find_package(Threads REQUIRED)
get_filename_component(ROOT "${CMAKE_CURRENT_LIST_DIR}/../../.." ABSOLUTE)
# Host-only test DLL; it is not the Android AAR or its runtime closure.
add_library(mgmem_pressure_jni SHARED "${ROOT}/src/mgmem.cpp"
  "${ROOT}/adapters/android/native/pressure.cpp" "${ROOT}/adapters/android/native/borrow_jni.cpp"
  "${ROOT}/adapters/android/native/workspace_jni.cpp")
target_compile_features(mgmem_pressure_jni PRIVATE cxx_std_17)
target_include_directories(mgmem_pressure_jni PRIVATE "${ROOT}/include" ${JNI_INCLUDE_DIRS})
target_link_libraries(mgmem_pressure_jni PRIVATE Threads::Threads)
set(JAVA_ROOT "${ROOT}/adapters/android/java/io/github/jichuo1/meguviz/memory/android")
set(CLASSES "${CMAKE_CURRENT_BINARY_DIR}/classes")
set(SOURCES "${JAVA_ROOT}/NativeMemoryLease.java" "${JAVA_ROOT}/ManagedMemoryDomain.java"
  "${ROOT}/tests/android_managed_stability/StabilityEngine.java"
  "${ROOT}/tests/android_managed_stability/StabilityTiming.java"
  "${ROOT}/tests/android_managed_stability/ManagedStabilityInstrumentation.java"
  "${CMAKE_CURRENT_LIST_DIR}/EngineHostTest.java" "${CMAKE_CURRENT_LIST_DIR}/ForegroundGateHostTest.java"
  "${CMAKE_CURRENT_LIST_DIR}/PhaseDeadlineHostTest.java"
  "${CMAKE_CURRENT_LIST_DIR}/ForegroundFinishHostTest.java"
  "${CMAKE_CURRENT_LIST_DIR}/ManifestHostTest.java"
  "${CMAKE_CURRENT_LIST_DIR}/PressureR8ForegroundActivity.java"
  "${CMAKE_CURRENT_LIST_DIR}/android/app/Instrumentation.java" "${CMAKE_CURRENT_LIST_DIR}/android/content/Context.java"
  "${CMAKE_CURRENT_LIST_DIR}/android/os/Bundle.java" "${CMAKE_CURRENT_LIST_DIR}/android/os/Process.java"
  "${CMAKE_CURRENT_LIST_DIR}/android/util/AtomicFile.java"
  "${ROOT}/tests/android_borrow/host_stub/android/os/Looper.java"
  "${ROOT}/tests/android_borrow/host_stub/org/json/JSONObject.java")
add_custom_command(OUTPUT "${CLASSES}/io/github/jichuo1/meguviz/memory/android/EngineHostTest.class"
  COMMAND ${CMAKE_COMMAND} -E make_directory "${CLASSES}"
  COMMAND ${Java_JAVAC_EXECUTABLE} --release 8 -d "${CLASSES}" ${SOURCES}
  DEPENDS ${SOURCES} VERBATIM)
add_custom_target(stability_classes ALL DEPENDS "${CLASSES}/io/github/jichuo1/meguviz/memory/android/EngineHostTest.class")
enable_testing()
foreach(case normal unknown owner_timeout lease_timeout gate deadline manifest foreground_finish)
  if(case STREQUAL "gate")
    set(test_class io.github.jichuo1.meguviz.memory.android.ForegroundGateHostTest)
  elseif(case STREQUAL "deadline")
    set(test_class io.github.jichuo1.meguviz.memory.android.PhaseDeadlineHostTest)
  elseif(case STREQUAL "manifest")
    set(test_class io.github.jichuo1.meguviz.memory.android.ManifestHostTest)
  elseif(case STREQUAL "foreground_finish")
    set(test_class io.github.jichuo1.meguviz.memory.android.ForegroundFinishHostTest)
  else()
    set(test_class io.github.jichuo1.meguviz.memory.android.EngineHostTest)
  endif()
  add_test(NAME stability_host_${case} COMMAND ${Java_JAVA_EXECUTABLE}
    "-Djava.library.path=$<TARGET_FILE_DIR:mgmem_pressure_jni>" "-Dtest.stability.cache=${CMAKE_CURRENT_BINARY_DIR}"
    "-Dtest.stability.manifest=${ROOT}/tests/android_managed_stability/AndroidManifest.xml" -cp "${CLASSES}" ${test_class} ${case})
  set_tests_properties(stability_host_${case} PROPERTIES TIMEOUT 30)
endforeach()
