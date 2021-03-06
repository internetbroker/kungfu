PROJECT(kfext_ctp)

if(WIN32)
    SET(CMAKE_WINDOWS_EXPORT_ALL_SYMBOLS OFF)
endif()

AUX_SOURCE_DIRECTORY(src SOURCE_FILES)

SET(KF_EXTENSION_BUILD_DIR ${KF_EXTENSIONS_BUILD_DIR}/ctp)

IF(UNIX)
    SET(CTP_API_VERSION v6.3.15_20190220_api_tradeapi_se_linux64)
ENDIF()
IF(WIN32)
    SET(CTP_API_VERSION 6.3.15_20190220_tradeapi64_se_windows)
ENDIF()

INCLUDE_DIRECTORIES(api/${CTP_API_VERSION}/include)
LINK_DIRECTORIES(api/${CTP_API_VERSION}/lib)

CONFIGURE_FILE(__init__.py "${KF_EXTENSION_BUILD_DIR}/__init__.py")
CONFIGURE_FILE(package.json "${KF_EXTENSION_BUILD_DIR}/package.json")

PYBIND11_ADD_MODULE(${PROJECT_NAME} SHARED ${SOURCE_FILES})
SET_TARGET_PROPERTIES(${PROJECT_NAME} PROPERTIES LIBRARY_OUTPUT_DIRECTORY ${KF_EXTENSION_BUILD_DIR})
IF(WIN32)
    SET_TARGET_PROPERTIES(${PROJECT_NAME} PROPERTIES RUNTIME_OUTPUT_DIRECTORY_RELEASE ${KF_EXTENSION_BUILD_DIR})
    SET_TARGET_PROPERTIES(${PROJECT_NAME} PROPERTIES RUNTIME_OUTPUT_DIRECTORY_DEBUG ${KF_EXTENSION_BUILD_DIR})
ENDIF()

IF(UNIX)
    TARGET_LINK_LIBRARIES(${PROJECT_NAME} PRIVATE ctpthostmduserapi ctpthosttraderapi wingchun)
    FILE(COPY api/${CTP_API_VERSION}/lib/libctpthostmduserapi.so DESTINATION ${KF_EXTENSION_BUILD_DIR})
    FILE(COPY api/${CTP_API_VERSION}/lib/libctpthosttraderapi.so DESTINATION ${KF_EXTENSION_BUILD_DIR})
ENDIF()

IF(WIN32)
    TARGET_LINK_LIBRARIES(${PROJECT_NAME} PRIVATE thosttraderapi_se thostmduserapi_se wingchun)
    FILE(COPY api/${CTP_API_VERSION}/lib/thosttraderapi_se.dll DESTINATION ${KF_EXTENSION_BUILD_DIR})
    FILE(COPY api/${CTP_API_VERSION}/lib/thostmduserapi_se.dll DESTINATION ${KF_EXTENSION_BUILD_DIR})
    FILE(COPY api/${CTP_API_VERSION}/lib/thosttraderapi_se.lib DESTINATION ${KF_EXTENSION_BUILD_DIR})
    FILE(COPY api/${CTP_API_VERSION}/lib/thostmduserapi_se.lib DESTINATION ${KF_EXTENSION_BUILD_DIR})
ENDIF()

ADD_CUSTOM_COMMAND(OUTPUT package_json
        COMMAND ${CMAKE_COMMAND} -E copy ${PROJECT_SOURCE_DIR}/package.json ${KF_EXTENSION_BUILD_DIR})
ADD_CUSTOM_TARGET(kfext_ctp_package_json ALL DEPENDS package_json)