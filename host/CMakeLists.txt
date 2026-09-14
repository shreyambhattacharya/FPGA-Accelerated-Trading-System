cmake_minimum_required(VERSION 3.16)
project(fpga_trading_host LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_library(trading_host
    src/protocol.cpp
    src/spi_transport.cpp
    src/latency_tracker.cpp
    src/market_event.cpp
    src/symbol_registry.cpp
    src/sequence_manager.cpp
    src/synthetic_market_data_source.cpp
    src/fpga_client.cpp
    src/trading_engine.cpp
)
target_include_directories(trading_host PUBLIC include)
target_compile_options(trading_host PRIVATE -Wall -Wextra -Wpedantic)

add_executable(loopback_test tests/loopback_test.cpp)
target_link_libraries(loopback_test PRIVATE trading_host)
target_compile_options(loopback_test PRIVATE -Wall -Wextra -Wpedantic)

add_executable(protocol_unit_test tests/protocol_unit_test.cpp)
target_link_libraries(protocol_unit_test PRIVATE trading_host)
target_compile_options(protocol_unit_test PRIVATE -Wall -Wextra -Wpedantic)

add_executable(host_runtime_test tests/host_runtime_test.cpp)
target_link_libraries(host_runtime_test PRIVATE trading_host)
target_compile_options(host_runtime_test PRIVATE -Wall -Wextra -Wpedantic)

add_executable(trading_engine src/main.cpp)
target_link_libraries(trading_engine PRIVATE trading_host)
target_compile_options(trading_engine PRIVATE -Wall -Wextra -Wpedantic)
