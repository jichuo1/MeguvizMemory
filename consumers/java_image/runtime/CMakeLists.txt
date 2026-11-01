cmake_minimum_required(VERSION 3.25)
project(MeguvizMemoryJavaImageHostRuntime LANGUAGES CXX)

# A host JVM build of the existing JNI adapter, linked to one installed shared
# Memory runtime. This is a separate host artifact, not an Android AAR binary.
find_package(MeguvizMemory CONFIG REQUIRED)
find_package(JNI REQUIRED)
find_package(Threads REQUIRED)
get_target_property(memory_kind MeguvizMemory::mgmem TYPE)
if(NOT memory_kind STREQUAL "SHARED_LIBRARY")
  message(FATAL_ERROR "The Java image consumer requires one installed shared Memory runtime")
endif()
get_filename_component(ROOT "${CMAKE_CURRENT_LIST_DIR}/../../.." ABSOLUTE)
add_library(mgmem_pressure_jni SHARED
  "${ROOT}/adapters/android/native/pressure_jni.cpp"
  "${ROOT}/adapters/android/native/borrow_jni.cpp"
  "${ROOT}/adapters/android/native/workspace_jni.cpp")
target_compile_features(mgmem_pressure_jni PRIVATE cxx_std_17)
target_include_directories(mgmem_pressure_jni PRIVATE ${JNI_INCLUDE_DIRS})
target_link_libraries(mgmem_pressure_jni PRIVATE MeguvizMemory::pressure MeguvizMemory::mgmem Threads::Threads)
if(WIN32)
  target_compile_definitions(mgmem_pressure_jni PRIVATE MGMEM_PRESSURE_SHARED MGMEM_PRESSURE_BUILDING)
endif()
set_target_properties(mgmem_pressure_jni PROPERTIES CXX_EXTENSIONS OFF CXX_VISIBILITY_PRESET hidden VISIBILITY_INLINES_HIDDEN ON)
install(TARGETS mgmem_pressure_jni RUNTIME DESTINATION bin LIBRARY DESTINATION lib ARCHIVE DESTINATION lib)
