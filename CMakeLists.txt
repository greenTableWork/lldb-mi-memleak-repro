cmake_minimum_required(VERSION 3.20)
project(lldb-mi-memleak-repro NONE)

include(CTest)
find_package(Python3 REQUIRED COMPONENTS Interpreter)

set(LLDB_MI_EXECUTABLE "" CACHE FILEPATH
  "Path to the lldb-mi executable under test")
set(LLDB_MI_MEMORY_TEST_MAX_MIB 512 CACHE STRING
  "RSS/physical footprint watchdog limit (MiB); use 1024 for sanitizer builds")

if (NOT LLDB_MI_EXECUTABLE)
  message(FATAL_ERROR "Set -DLLDB_MI_EXECUTABLE=/path/to/lldb-mi")
endif()

foreach(mode contracts update lifecycle)
  add_test(NAME varobj-${mode}
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/repro.py
      --lldb-mi ${LLDB_MI_EXECUTABLE}
      --mode ${mode} --count 256 --cycles 100 --expect-correct
      --max-mib ${LLDB_MI_MEMORY_TEST_MAX_MIB})
  set_tests_properties(varobj-${mode} PROPERTIES
    TIMEOUT 120
    RUN_SERIAL TRUE
    LABELS "integration;memory")
endforeach()
