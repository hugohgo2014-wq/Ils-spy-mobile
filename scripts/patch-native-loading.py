#!/usr/bin/env python3
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
cmake = root / "app/src/main/cpp/CMakeLists.txt"
cmake.write_text("""cmake_minimum_required(VERSION 3.22.1)
project(ilspy_mobile_bridge LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

set(ENGINE_DIR "${CMAKE_CURRENT_LIST_DIR}/../jniLibs/${ANDROID_ABI}")
set(ENGINE_SO "${ENGINE_DIR}/libilspy_engine.so")
if(NOT EXISTS "${ENGINE_SO}")
    message(FATAL_ERROR "ILSpy NativeAOT engine is missing for ${ANDROID_ABI}. Run the build-managed-engine workflow step first.")
endif()

add_library(ilspy_bridge SHARED native-lib.cpp)
target_compile_options(ilspy_bridge PRIVATE -Wall -Wextra -Werror=return-type)
find_library(android-log log)

# Do not link the imported .so by its absolute build-machine path.
# Android's dynamic linker must see only the packaged soname.
target_link_options(ilspy_bridge PRIVATE "-L${ENGINE_DIR}" "-Wl,--no-undefined")
target_link_libraries(ilspy_bridge PRIVATE "-lilspy_engine" ${android-log})
""", encoding="utf-8")

bridge = root / "app/src/main/java/com/ilspy/mobile/NativeBridge.java"
bridge.write_text("""package com.ilspy.mobile;

/** Loads the NativeAOT engine before the JNI bridge and preserves loader diagnostics. */
final class NativeBridge {
    private static boolean loaded;
    private static String loadError = "Unknown native loading error";

    static {
        try {
            // Load the dependency explicitly first. Android's linker must resolve
            // libilspy_engine.so before libilspy_bridge.so is initialized.
            System.loadLibrary("ilspy_engine");
            System.loadLibrary("ilspy_bridge");
            loaded = true;
            loadError = "";
        } catch (Throwable error) {
            loaded = false;
            loadError = error.getClass().getSimpleName() + ": " + String.valueOf(error.getMessage());
            android.util.Log.e("ILSpyMobile", "Unable to load ILSpy native engine", error);
        }
    }

    private NativeBridge() { }

    static boolean isLoaded() { return loaded; }
    static String loadError() { return loadError; }

    static native String nativeEngineVersion();
    static native String nativeInspect(String absolutePath);
    static native String nativeDecompile(String absolutePath);
    static native String nativeDecompileType(String absolutePath, String fullTypeName);
}
""", encoding="utf-8")

activity = root / "app/src/main/java/com/ilspy/mobile/MainActivity.java"
text = activity.read_text(encoding="utf-8")
old = """    private void checkEngine() {
        try {
            String version = NativeBridge.nativeEngineVersion();
            engineAvailable = version != null && !version.startsWith("ILSPY_ERROR:");
            engineStatus.setText(engineAvailable ? getString(R.string.engine_ready) + "  ·  " + version
                    : getString(R.string.engine_unavailable));
        } catch (Throwable error) {
            engineAvailable = false;
            engineStatus.setText(getString(R.string.engine_unavailable));
        }
        openButton.setEnabled(engineAvailable);
    }"""
new = """    private void checkEngine() {
        try {
            if (!NativeBridge.isLoaded()) {
                engineAvailable = false;
                engineStatus.setText(getString(R.string.engine_unavailable) + " · " + NativeBridge.loadError());
            } else {
                String version = NativeBridge.nativeEngineVersion();
                engineAvailable = version != null && !version.startsWith("ILSPY_ERROR:");
                engineStatus.setText(engineAvailable ? getString(R.string.engine_ready) + "  ·  " + version
                        : getString(R.string.engine_unavailable) + " · " + String.valueOf(version));
            }
        } catch (Throwable error) {
            engineAvailable = false;
            engineStatus.setText(getString(R.string.engine_unavailable) + " · "
                    + error.getClass().getSimpleName() + ": " + String.valueOf(error.getMessage()));
            android.util.Log.e("ILSpyMobile", "Native engine self-test failed", error);
        }
        openButton.setEnabled(engineAvailable);
    }"""
if old not in text:
    raise SystemExit("Could not find exact checkEngine block; refusing to patch unexpected source")
activity.write_text(text.replace(old, new), encoding="utf-8")
print("Patched CMake soname linking, NativeBridge loading order, and engine diagnostics.")
