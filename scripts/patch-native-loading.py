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

# Remove every restrictive MIME allow-list and force wildcard on all Android
# document/content intents, including builders whose type was set later.
picker_source = re.sub(
    r'\.setType(?:AndNormalize)?\s*\([^;]*?\)',
    '.setType("*/*")',
    picker_source,
)
picker_source = re.sub(
    r'\.putExtra\s*\(\s*Intent\.EXTRA_MIME_TYPES\s*,\s*(?:new\s+String\s*\[\s*\]\s*\{[^}]*\}|[^;]*?)\s*\)',
    '',
    picker_source,
)
picker_source = re.sub(
    r'\.putExtra\s*\(\s*["\']android\.intent\.extra\.MIME_TYPES["\']\s*,[^;]*?\)',
    '',
    picker_source,
)

# Normalize picker creation after MIME filtering. The previous generic
# substitution could leave a bare "intent;" expression in Java; remove it
# and set the wildcard directly on each document-picker Intent.
picker_source = re.sub(r'(?m)^[ \t]*intent;[ \t]*$', '', picker_source)
picker_source = re.sub(
    r'(Intent\s+intent\s*=\s*new\s+Intent\s*\(\s*Intent\.(?:ACTION_OPEN_DOCUMENT|ACTION_GET_CONTENT)\s*\)\s*;)',
    r'\1\n        intent.setType("*/*");',
    picker_source,
)
activity.write_text(picker_source, encoding="utf-8")



# Replace the generic landing screen with an ILSpy-style mobile workbench.
# Keep the existing NativeAOT engine and JNI bridge untouched.
workbench = root / "app/src/main/java/com/ilspy/mobile/WorkbenchActivity.java"
workbench.write_text(r'''package com.ilspy.mobile;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.HorizontalScrollView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.graphics.Typeface;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;

public final class WorkbenchActivity extends Activity {
    private static final int PICK_ASSEMBLY = 4102;
    private int bg = Color.rgb(18, 21, 28), panel = Color.rgb(29, 34, 44);
    private int border = Color.rgb(55, 65, 81), accent = Color.rgb(35, 155, 219);
    private int text = Color.rgb(226, 232, 240), muted = Color.rgb(148, 163, 184);
    private LinearLayout root, toolbar, content, bottom;
    private TextView title, status, code;
    private File currentFile;
    private String inspectResult = "", decompiled = "";

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().setStatusBarColor(bg);
        getWindow().setNavigationBarColor(bg);
        getWindow().getDecorView().setSystemUiVisibility(0);
        buildShell();
        boolean ready = NativeBridge.initialize(getApplicationContext());
        status.setText(ready ? "ILSpy engine · " + NativeBridge.nativeEngineVersion()
                : "Engine unavailable · " + NativeBridge.loadError());
        status.setTextColor(ready ? Color.rgb(52, 211, 153) : Color.rgb(248, 113, 113));
    }

    private GradientDrawable shape(int color, int stroke) {
        GradientDrawable d = new GradientDrawable();
        d.setColor(color); d.setCornerRadius(dp(12));
        if (stroke != 0) d.setStroke(dp(1), stroke);
        return d;
    }
    private int dp(float v) { return (int)(v * getResources().getDisplayMetrics().density + .5f); }
    private TextView label(String value, float size, int color, boolean bold) {
        TextView t = new TextView(this); t.setText(value); t.setTextSize(size);
        t.setTextColor(color); t.setGravity(Gravity.CENTER_VERTICAL);
        if (bold) t.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        return t;
    }
    private Button button(String value, boolean primary, Runnable action) {
        Button b = new Button(this); b.setText(value); b.setAllCaps(false);
        b.setTextSize(13); b.setTextColor(primary ? Color.WHITE : text);
        b.setBackground(shape(primary ? accent : panel, primary ? 0 : border));
        b.setOnClickListener(v -> action.run());
        return b;
    }
    private void buildShell() {
        root = new LinearLayout(this); root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(16), dp(8), dp(16), dp(8)); root.setBackgroundColor(bg);
        setContentView(root);
        toolbar = new LinearLayout(this); toolbar.setGravity(Gravity.CENTER_VERTICAL);
        toolbar.setOrientation(LinearLayout.HORIZONTAL);
        TextView brand = label("ILSpy", 23, text, true);
        toolbar.addView(brand, new LinearLayout.LayoutParams(0, dp(52), 1));
        toolbar.addView(button("＋ Abrir assembly", true, this::chooseFile));
        root.addView(toolbar);
        status = label("Inicializando motor…", 12, muted, false);
        status.setPadding(dp(2), 0, 0, dp(12)); root.addView(status);
        LinearLayout filebar = new LinearLayout(this); filebar.setOrientation(1);
        filebar.setPadding(dp(14), dp(10), dp(14), dp(10)); filebar.setBackground(shape(panel, border));
        title = label("Nenhum assembly aberto", 15, text, true);
        filebar.addView(title);
        TextView hint = label("Assembly Explorer   ›   C# Code", 11, muted, false);
        filebar.addView(hint); root.addView(filebar);
        LinearLayout tabs = new LinearLayout(this); tabs.setOrientation(0);
        tabs.setPadding(0, dp(10), 0, dp(10));
        tabs.addView(button("Explorer", false, () -> showResult(inspectResult.isEmpty() ? "Abra uma DLL ou EXE .NET para explorar seus assemblies." : inspectResult)));
        tabs.addView(button("C# Code", true, () -> showResult(decompiled.isEmpty() ? "Selecione um assembly para descompilar." : decompiled)));
        tabs.addView(button("Info", false, () -> showResult(currentFile == null ? "Nenhum arquivo selecionado." : currentFile.getName() + "\n" + currentFile.length() + " bytes\nCaminho: " + currentFile.getAbsolutePath())));
        root.addView(tabs);
        ScrollView scroll = new ScrollView(this);
        content = new LinearLayout(this); content.setOrientation(1);
        content.setPadding(dp(12), dp(12), dp(12), dp(12)); content.setBackground(shape(Color.rgb(248, 249, 251), border));
        code = label("Bem-vindo ao ILSpy Mobile\n\nAbra um assembly .NET para navegar pelos namespaces, tipos e métodos e visualizar o código C# descompilado.", 13, Color.rgb(35, 42, 52), false);
        code.setTypeface(Typeface.MONOSPACE); code.setGravity(Gravity.TOP | Gravity.START);
        code.setTextIsSelectable(true); content.addView(code);
        scroll.addView(content); root.addView(scroll, new LinearLayout.LayoutParams(-1, 0, 1));
        bottom = new LinearLayout(this); bottom.setGravity(Gravity.CENTER_VERTICAL);
        bottom.addView(label("ICSharpCode.Decompiler · ILSpy engine", 11, muted, false), new LinearLayout.LayoutParams(0, dp(42), 1));
        bottom.addView(button("Copiar código", false, () -> {
            android.content.ClipboardManager cm = (android.content.ClipboardManager)getSystemService(CLIPBOARD_SERVICE);
            cm.setPrimaryClip(android.content.ClipData.newPlainText("C# code", decompiled));
        }));
        root.addView(bottom);
    }
    private void showResult(String value) { code.setText(value == null ? "" : value); }
    private void chooseFile() {
        Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        i.setType("*/*"); i.addCategory(Intent.CATEGORY_OPENABLE);
        startActivityForResult(i, PICK_ASSEMBLY);
    }
    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request != PICK_ASSEMBLY || result != RESULT_OK || data == null || data.getData() == null) return;
        Uri uri = data.getData();
        try {
            File target = new File(getCacheDir(), "imported-" + System.currentTimeMillis() + ".bin");
            try (InputStream in = getContentResolver().openInputStream(uri);
                 FileOutputStream out = new FileOutputStream(target)) {
                if (in == null) throw new IllegalStateException("Não foi possível ler o arquivo selecionado.");
                byte[] buf = new byte[65536]; int n;
                while ((n = in.read(buf)) != -1) out.write(buf, 0, n);
            }
            currentFile = target;
            title.setText(uri.getLastPathSegment() == null ? target.getName() : uri.getLastPathSegment());
            status.setText("Analisando assembly localmente…");
            inspectResult = NativeBridge.nativeInspect(target.getAbsolutePath());
            decompiled = NativeBridge.nativeDecompile(target.getAbsolutePath());
            showResult(decompiled);
            status.setText("Assembly carregado · descompilação concluída");
        } catch (Throwable e) {
            status.setText("Falha ao abrir arquivo");
            showResult(e.getClass().getSimpleName() + ": " + String.valueOf(e.getMessage()));
        }
    }
}
''', encoding="utf-8")

manifest = root / "app/src/main/AndroidManifest.xml"
xml = manifest.read_text(encoding="utf-8")
if "com.ilspy.mobile.MainActivity" in xml:
    xml = xml.replace("com.ilspy.mobile.MainActivity", "com.ilspy.mobile.WorkbenchActivity")
elif ".MainActivity" in xml:
    xml = xml.replace(".MainActivity", ".WorkbenchActivity")
else:
    raise SystemExit("Could not locate launcher MainActivity in AndroidManifest.xml")
manifest.write_text(xml, encoding="utf-8")

print("Patched to bundle engine as ABI-specific APK assets and load via dlopen; bridge no longer DT_NEEDED-links engine.")
