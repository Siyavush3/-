add_library(arm112 SHARED
  src/version.c
  src/platform/mmap_file.c
  src/platform/cpu.c
  src/ekp/ekp.c
)
target_include_directories(arm112
  PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include
  PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src
)
target_compile_definitions(arm112 PRIVATE
  A112_BUILDING
  A112_VERSION_STRING="${PROJECT_VERSION}"
)
set_target_properties(arm112 PROPERTIES C_VISIBILITY_PRESET hidden)
if(WIN32)
  # arm112.dll и у MSVC, и у MinGW/zig — без префикса lib.
  set_target_properties(arm112 PROPERTIES PREFIX "")
endif()

function(a112_warnings target)
  if(MSVC)
    target_compile_options(${target} PRIVATE /W4 /utf-8)
    target_compile_definitions(${target} PRIVATE _CRT_SECURE_NO_WARNINGS)
  else()
    target_compile_options(${target} PRIVATE -Wall -Wextra -Wpedantic -Wshadow)
  endif()
endfunction()
a112_warnings(arm112)

# Таблица маршрутизации classifier.ekp собирается из JSON в data/classifier.
find_package(Python3 COMPONENTS Interpreter)
if(Python3_Interpreter_FOUND)
  set(A112_EKP_TABLE ${CMAKE_BINARY_DIR}/data/classifier.ekp)
  add_custom_command(
    OUTPUT ${A112_EKP_TABLE}
    COMMAND ${CMAKE_COMMAND} -E make_directory ${CMAKE_BINARY_DIR}/data
    COMMAND ${Python3_EXECUTABLE} ${PROJECT_SOURCE_DIR}/tools/compile_ekp.py
            ${PROJECT_SOURCE_DIR}/data/classifier/classifier.json
            ${PROJECT_SOURCE_DIR}/data/classifier/ui_services.json
            ${A112_EKP_TABLE}
    DEPENDS ${PROJECT_SOURCE_DIR}/tools/compile_ekp.py
            ${PROJECT_SOURCE_DIR}/data/classifier/classifier.json
            ${PROJECT_SOURCE_DIR}/data/classifier/ui_services.json
    COMMENT "compile_ekp -> classifier.ekp"
    VERBATIM
  )
  add_custom_target(ekp_table ALL DEPENDS ${A112_EKP_TABLE})
else()
  message(WARNING "Python 3 не найден: classifier.ekp не собирается, тесты маршрутизации ядра пропускаются")
endif()

if(A112_BUILD_TESTS)
  add_executable(test_core tests/test_core.c)
  target_link_libraries(test_core PRIVATE arm112)
  if(WIN32)
    target_link_libraries(test_core PRIVATE shell32) # CommandLineToArgvW
  endif()
  a112_warnings(test_core)
  if(Python3_Interpreter_FOUND)
    add_test(NAME core COMMAND test_core ${A112_EKP_TABLE})
  else()
    add_test(NAME core COMMAND test_core)
  endif()
endif()
