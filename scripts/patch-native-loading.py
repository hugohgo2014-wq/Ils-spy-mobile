#!/usr/bin/env python3
from pathlib import Path
import re
import sys

root = Path(sys.argv[1]).resolve()

# The ILSpy NativeAOT library is bundled as an APK asset and loaded by absolute
# private-app path via dlopen. It is deliberately NOT a DT_NEEDED dependency of
# libilspy_bridge.so: Android's class-loader namespace cannot resolve the
# runner's build path, and JNI lib merging does not reliably package an
# imported CMake target.
cmake = root / "app/src/main/cpp/CMakeLists.txt"
cmake.write_text("""cmake_minimum_required(VERSION 3.22.1)
project(ilspy_mobile_bridge LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_library(ilspy_bridge SHARED native-lib.cpp)
target_compile_options(ilspy_bridge PRIVATE -Wall -Wextra -Werror=return-type)
find_library(android-log log)
find_library(android-dl dl)
target_link_libraries(ilspy_bridge PRIVATE \${android-log} \${android-dl})
""", encoding="utf-8")

# Replace direct unresolved engine references with an explicit runtime-loaded C ABI.
cpp = root / "app/src/main/cpp/native-lib.cpp"
source = cpp.read_text(encoding="utf-8")
source = source.replace("#include <fstream>\n#include <string>", "#include <fstream>\n#include <string>\n#include <mutex>\n#include <dlfcn.h>")
start = source.find('extern "C" {')
end = source.find('} // namespace', start)
# The first namespace close follows the old extern declarations and namespace block.
if start < 0:
    raise SystemExit("JNI source does not contain expected engine declarations")
old = """extern "C" {
char* ilspy_engine_version();
char* ilspy_inspect_file(const char* path);
char* ilspy_decompile_file(const char* path);
char* ilspy_decompile_type(const char* path, const char* type_name);
void ilspy_engine_free_string(char* value);
}
"""
if old not in source:
    raise SystemExit("Expected direct ILSpy engine declarations were not found")
source = source.replace(old, """namespace {
struct EngineApi {
    void* handle = nullptr;
    char* (*version)() = nullptr;
    char* (*inspect)(const char*) = nullptr;
    char* (*decompile)(const char*) = nullptr;
    char* (*decompileType)(const char*, const char*) = nullptr;
    void (*freeString)(char*) = nullptr;
    std::string error = "ILSpy engine has not been initialized.";
    std::mutex mutex;
    bool loaded = false;
};
EngineApi g_engine;

template <typename Function>
bool bindEngineSymbol(Function& function, const char* name) {
    dlerror();
    void* address = dlsym(g_engine.handle, name);
    const char* error = dlerror();
    if (error != nullptr || address == nullptr) {
        g_engine.error = std::string("Missing NativeAOT export ") + name +
            (error != nullptr ? std::string(": ") + error : std::string());
        return false;
    }
    function = reinterpret_cast<Function>(address);
    return true;
}
} // namespace
""")
# Native string ownership now calls the dynamically-resolved free export.
source = source.replace("ilspy_engine_free_string(value)", "g_engine.freeString(value)")
source = source.replace("ilspy_engine_version()", "g_engine.version()")
source = source.replace("ilspy_inspect_file(path.c_str())", "g_engine.inspect(path.c_str())")
source = source.replace("ilspy_decompile_file(path.c_str())", "g_engine.decompile(path.c_str())")
source = source.replace("ilspy_decompile_type(path.c_str(), type.c_str())", "g_engine.decompileType(path.c_str(), type.c_str())")
marker = 'extern "C" JNIEXPORT jstring JNICALL\nJava_com_ilspy_mobile_NativeBridge_nativeEngineVersion'
if marker not in source:
    raise SystemExit("Could not locate nativeEngineVersion JNI entry point")
initializer = r'''extern "C" JNIEXPORT jstring JNICALL
Java_com_ilspy_mobile_NativeBridge_nativeInitialize(JNIEnv* env, jclass, jstring enginePath) {
    std::lock_guard<std::mutex> guard(g_engine.mutex);
    if (g_engine.loaded) return env->NewStringUTF("OK");

    const std::string path = javaStringToUtf8(env, enginePath);
    if (path.empty()) return env->NewStringUTF("ILSPY_ERROR: Empty engine path.");

    g_engine.handle = dlopen(path.c_str(), RTLD_NOW | RTLD_LOCAL);
    if (g_engine.handle == nullptr) {
        const char* error = dlerror();
        g_engine.error = error != nullptr ? error : "dlopen failed without diagnostic.";
        const std::string message = "ILSPY_ERROR: " + g_engine.error;
        return env->NewStringUTF(message.c_str());
    }

    const bool ok =
        bindEngineSymbol(g_engine.version, "ilspy_engine_version") &&
        bindEngineSymbol(g_engine.inspect, "ilspy_inspect_file") &&
        bindEngineSymbol(g_engine.decompile, "ilspy_decompile_file") &&
        bindEngineSymbol(g_engine.decompileType, "ilspy_decompile_type") &&
        bindEngineSymbol(g_engine.freeString, "ilspy_engine_free_string");

    if (!ok) {
        dlclose(g_engine.handle);
        g_engine.handle = nullptr;
        g_engine.version = nullptr;
        g_engine.inspect = nullptr;
        g_engine.decompile = nullptr;
        g_engine.decompileType = nullptr;
        g_engine.freeString = nullptr;
        const std::string message = "ILSPY_ERROR: " + g_engine.error;
        return env->NewStringUTF(message.c_str());
    }

    g_engine.loaded = true;
    g_engine.error.clear();
    return env->NewStringUTF("OK");
}

'''
source = source.replace(marker, initializer + marker)
# Every exported JNI method is called only after Java's initialize() success.
source = source.replace("""Java_com_ilspy_mobile_NativeBridge_nativeEngineVersion(JNIEnv* env, jclass) {
    return consumeResult(env, g_engine.version());
}""", """Java_com_ilspy_mobile_NativeBridge_nativeEngineVersion(JNIEnv* env, jclass) {
    if (!g_engine.loaded) return env->NewStringUTF("ILSPY_ERROR: Engine not initialized.");
    return consumeResult(env, g_engine.version());
}""")
source = source.replace("""    const std::string path = javaStringToUtf8(env, filePath);
    const bool valid = hasPortableExecutableHeader(path.c_str());""", """    if (!g_engine.loaded) return env->NewStringUTF("ILSPY_ERROR: Engine not initialized.");
    const std::string path = javaStringToUtf8(env, filePath);
    const bool valid = hasPortableExecutableHeader(path.c_str());""")
cpp.write_text(source, encoding="utf-8")

bridge = root / "app/src/main/java/com/ilspy/mobile/NativeBridge.java"
bridge.write_text("""package com.ilspy.mobile;

import android.content.Context;
import android.os.Build;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;

/** Extracts the bundled NativeAOT engine and loads it without a DT_NEEDED dependency. */
final class NativeBridge {
    private static boolean bridgeLoaded;
    private static boolean engineLoaded;
    private static String loadError = "Engine not initialized.";

    private NativeBridge() { }

    static synchronized boolean initialize(Context context) {
        if (engineLoaded) return true;
        try {
            String abi = null;
            for (String supported : Build.SUPPORTED_ABIS) {
                if ("arm64-v8a".equals(supported) || "x86_64".equals(supported)) {
                    abi = supported;
                    break;
                }
            }
            if (abi == null) throw new IllegalStateException("This device ABI is not supported: "
                    + java.util.Arrays.toString(Build.SUPPORTED_ABIS));

            File directory = new File(context.getCodeCacheDir(), "ilspy-engine/" + abi);
            if (!directory.exists() && !directory.mkdirs())
                throw new IllegalStateException("Cannot create private engine directory.");
            File engineFile = new File(directory, "libilspy_engine.so");
            File temporary = new File(directory, "libilspy_engine.so.tmp");
            try (InputStream input = context.getAssets().open("native/" + abi + "/libilspy_engine.so");
                 FileOutputStream output = new FileOutputStream(temporary, false)) {
                byte[] buffer = new byte[65536];
                int count;
                while ((count = input.read(buffer)) != -1) output.write(buffer, 0, count);
                output.getFD().sync();
            }
            if (temporary.length() < 1024 * 1024)
                throw new IllegalStateException("Bundled ILSpy engine is incomplete (" + temporary.length() + " bytes).");
            if (engineFile.exists() && !engineFile.delete())
                throw new IllegalStateException("Cannot replace previous native engine.");
            if (!temporary.renameTo(engineFile))
                throw new IllegalStateException("Cannot finalize extracted native engine.");
            engineFile.setReadable(true, true);

            if (!bridgeLoaded) {
                System.loadLibrary("ilspy_bridge");
                bridgeLoaded = true;
            }
            String result = nativeInitialize(engineFile.getAbsolutePath());
            engineLoaded = "OK".equals(result);
            loadError = engineLoaded ? "" : String.valueOf(result);
            return engineLoaded;
        } catch (Throwable error) {
            engineLoaded = false;
            loadError = error.getClass().getSimpleName() + ": " + String.valueOf(error.getMessage());
            android.util.Log.e("ILSpyMobile", "Native ILSpy engine initialization failed", error);
            return false;
        }
    }

    static boolean isLoaded() { return engineLoaded; }
    static String loadError() { return loadError; }

    private static native String nativeInitialize(String absoluteEnginePath);
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
            engineAvailable = NativeBridge.initialize(getApplicationContext());
            if (engineAvailable) {
                String version = NativeBridge.nativeEngineVersion();
                engineAvailable = version != null && !version.startsWith("ILSPY_ERROR:");
                engineStatus.setText(engineAvailable ? getString(R.string.engine_ready) + "  ·  " + version
                        : getString(R.string.engine_unavailable) + " · " + String.valueOf(version));
            } else {
                engineStatus.setText(getString(R.string.engine_unavailable) + " · " + NativeBridge.loadError());
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
    raise SystemExit("Could not find expected MainActivity.checkEngine implementation")
activity.write_text(text.replace(old, new), encoding="utf-8")

# Ensure both ABI engines are included under assets/, not merely left in jniLibs/.
builder = root / "scripts/build-engine.sh"
build_text = builder.read_text(encoding="utf-8")
copy_line = '  cp "$candidate" "$destination"\n'
if copy_line not in build_text:
    raise SystemExit("Could not find engine copy line in build-engine.sh")
replacement = """  cp "$candidate" "$destination"
  local asset_destination="$ROOT/app/src/main/assets/native/$abi/libilspy_engine.so"
  mkdir -p "$(dirname "$asset_destination")"
  cp "$candidate" "$asset_destination"
  echo "Bundled asset $(du -h "$asset_destination" | cut -f1) -> $asset_destination"
"""
builder.write_text(build_text.replace(copy_line, replacement), encoding="utf-8")


# Android's document providers commonly classify .dll/.exe/.winmd as
# application/octet-stream or unknown. A narrow MIME filter hides them from
# ACTION_OPEN_DOCUMENT before the app ever receives a URI. Use the SAF wildcard
# picker; the app's PE/CLI validation remains responsible for file contents.
picker_source = activity.read_text(encoding="utf-8")
picker_source = re.sub(
    r"(\b[A-Za-z_$][\w$]*\.setType\()\s*[^;]*?(\);)",
    r'\1"*/*"\2',
    picker_source,
)
picker_pattern = re.compile(
    r"(Intent\s+([A-Za-z_$][\w$]*)\s*=\s*new\s+Intent\s*\(\s*Intent\.(?:ACTION_OPEN_DOCUMENT|ACTION_GET_CONTENT)\s*\)\s*;)"
)
def add_wildcard_picker(match):
    statement, variable = match.group(1), match.group(2)
    return statement + '\n        ' + variable + '.setType("*/*");'
picker_source = picker_pattern.sub(add_wildcard_picker, picker_source)
if "Intent.ACTION_OPEN_DOCUMENT" in picker_source or "Intent.ACTION_GET_CONTENT" in picker_source:
    if not re.search(r"\.setType\(\s*[\"']\*/\*[\"']\s*\)", picker_source):
        raise SystemExit("File picker still has no wildcard MIME type; refusing to build a filtered APK")
activity.write_text(picker_source, encoding="utf-8")

print("Patched to bundle engine as ABI-specific APK assets and load via dlopen; bridge no longer DT_NEEDED-links engine.")
