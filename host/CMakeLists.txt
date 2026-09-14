cmake_minimum_required(VERSION 3.16)
project(fpga_trading_host LANGUAGES CXX)

option(FPGA_TRADER_ENABLE_NETWORKING
       "Build the Alpaca Boost.Beast/OpenSSL/nlohmann-json market-data receiver"
       OFF)

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

if(FPGA_TRADER_ENABLE_NETWORKING)
    find_package(Boost 1.70 REQUIRED)
    find_package(OpenSSL REQUIRED)
    find_path(NLOHMANN_JSON_INCLUDE_DIR nlohmann/json.hpp)
    if(NOT NLOHMANN_JSON_INCLUDE_DIR)
        message(FATAL_ERROR "FPGA_TRADER_ENABLE_NETWORKING requires nlohmann/json.hpp")
    endif()

    target_sources(trading_host PRIVATE
        src/alpaca_message_parser.cpp
        src/alpaca_market_data_source.cpp
        src/websocket_client.cpp
    )
    target_compile_definitions(trading_host PUBLIC
        FPGA_TRADER_ENABLE_NETWORKING=1
        BOOST_ERROR_CODE_HEADER_ONLY
    )
    target_include_directories(trading_host PUBLIC ${Boost_INCLUDE_DIRS} ${NLOHMANN_JSON_INCLUDE_DIR})
    target_link_libraries(trading_host PUBLIC OpenSSL::SSL OpenSSL::Crypto)
else()
    target_compile_definitions(trading_host PUBLIC FPGA_TRADER_ENABLE_NETWORKING=0)
endif()

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

if(FPGA_TRADER_ENABLE_NETWORKING)
    add_executable(alpaca_networking_test tests/alpaca_networking_test.cpp)
    target_link_libraries(alpaca_networking_test PRIVATE trading_host)
    target_compile_options(alpaca_networking_test PRIVATE -Wall -Wextra -Wpedantic)
endif()
