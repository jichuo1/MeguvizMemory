# A separate build directory per mode prevents target/ODR collisions. Neither
# the shared Dex tree nor the production paired target is changed.
set(DEX_TRACE_STAGE "" CACHE PATH "Verified private Dex source preparation")
set(PAIRED_TRACE_STAGE "" CACHE PATH "Verified private paired source preparation")
set(DEX_TRACE_MODE "" CACHE STRING "baseline or traced")
if(NOT DEX_TRACE_MODE MATCHES "^(baseline|traced)$" OR
   NOT EXISTS "${DEX_TRACE_STAGE}/manifest.json" OR
   NOT EXISTS "${PAIRED_TRACE_STAGE}/manifest.json" OR
   NOT EXISTS "${PAIRED_TRACE_STAGE}/paired_diagnostic.cpp")
  message(FATAL_ERROR "Supply both prepared private stages and an explicit baseline/traced mode")
endif()
find_package(MeguvizMemory CONFIG REQUIRED)
set(DMG_BUILD_TESTS OFF CACHE BOOL "No external parser tests" FORCE)
set(DMG_WITH_SQLITE OFF CACHE BOOL "No optional SQL bridge needed" FORCE)
add_subdirectory("${DEX_TRACE_STAGE}/${DEX_TRACE_MODE}" "${CMAKE_CURRENT_BINARY_DIR}/dexmeguviz" EXCLUDE_FROM_ALL)
add_executable(mgmem_dex_reader_trace "${PAIRED_TRACE_STAGE}/paired_diagnostic.cpp"
  "${CMAKE_CURRENT_LIST_DIR}/consumer_trace.cpp")
target_compile_features(mgmem_dex_reader_trace PRIVATE cxx_std_20)
target_include_directories(mgmem_dex_reader_trace PRIVATE "${CMAKE_CURRENT_LIST_DIR}" "${CMAKE_CURRENT_LIST_DIR}/../image")
target_link_libraries(mgmem_dex_reader_trace PRIVATE MeguvizMemory::mgmem dexmeguviz_core ${CMAKE_DL_LIBS})
if(DEX_TRACE_MODE STREQUAL "traced")
  target_compile_definitions(mgmem_dex_reader_trace PRIVATE MGMEM_DEX_CONSUMER_TRACE=1)
  target_include_directories(dexmeguviz_core PRIVATE "${CMAKE_CURRENT_LIST_DIR}")
endif()
if(MSVC)
  target_compile_options(mgmem_dex_reader_trace PRIVATE /W4 /permissive- /utf-8)
else()
  target_compile_options(mgmem_dex_reader_trace PRIVATE -Wall -Wextra -Wpedantic)
endif()
