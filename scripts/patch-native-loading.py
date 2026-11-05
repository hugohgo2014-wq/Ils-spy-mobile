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
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Intent;
import android.database.Cursor;
import android.net.Uri;
import android.os.Bundle;
import android.provider.OpenableColumns;
import android.text.Editable;
import android.text.SpannableString;
import android.text.TextWatcher;
import android.text.style.ForegroundColorSpan;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.HorizontalScrollView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Mobile adaptation of ILSpy's assembly-browser + decompiled-source workflow.
 * The NativeAOT engine and JNI bridge are intentionally kept unchanged.
 */
public final class WorkbenchActivity extends Activity {
    private static final int PICK_ASSEMBLY = 4102;
    private static final int TAB_EXPLORER = 0, TAB_CODE = 1, TAB_METADATA = 2;

    // Desktop ILSpy-inspired palette: dark explorer chrome + warm code editor.
    private final int chrome = Color.rgb(34, 38, 44);
    private final int chrome2 = Color.rgb(47, 52, 60);
    private final int border = Color.rgb(76, 84, 94);
    private final int accent = Color.rgb(35, 132, 190);
    private final int editorBg = Color.rgb(250, 249, 239);
    private final int editorFg = Color.rgb(39, 43, 47);
    private final int muted = Color.rgb(165, 176, 189);
    private final int white = Color.rgb(241, 245, 249);

    private LinearLayout root, body, tabBar;
    private TextView status, assemblyName;
    private EditText search;
    private File currentFile;
    private String displayName = "";
    private String inspectResult = "";
    private String decompiled = "";
    private String selectedType = "";
    private int activeTab = TAB_EXPLORER;
    private final ArrayList<TypeEntry> types = new ArrayList<>();

    private static final class TypeEntry {
        final String name;
        final String fullName;
        TypeEntry(String name, String fullName) {
            this.name = name;
            this.fullName = fullName;
        }
    }

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().setStatusBarColor(chrome);
        getWindow().setNavigationBarColor(chrome);
        buildShell();
        boolean ready = NativeBridge.initialize(getApplicationContext());
        status.setText(ready
                ? "Motor ILSpy pronto · " + NativeBridge.nativeEngineVersion()
                : "Motor indisponível · " + NativeBridge.loadError());
        status.setTextColor(ready ? Color.rgb(77, 210, 164) : Color.rgb(248, 113, 113));
    }

    private int dp(float value) {
        return (int)(value * getResources().getDisplayMetrics().density + .5f);
    }

    private GradientDrawable rounded(int fill, int stroke) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(fill);
        drawable.setCornerRadius(dp(8));
        if (stroke != 0) drawable.setStroke(dp(1), stroke);
        return drawable;
    }

    private TextView text(String value, float size, int color, boolean bold) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(size);
        view.setTextColor(color);
        view.setGravity(Gravity.CENTER_VERTICAL);
        if (bold) view.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        return view;
    }

    private Button actionButton(String caption, boolean primary, Runnable action) {
        Button button = new Button(this);
        button.setText(caption);
        button.setTextSize(12);
        button.setAllCaps(false);
        button.setMinHeight(dp(42));
        button.setTextColor(primary ? Color.WHITE : white);
        button.setBackground(rounded(primary ? accent : chrome2, primary ? 0 : border));
        button.setOnClickListener(v -> action.run());
        return button;
    }

    private void buildShell() {
        root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(12), dp(5), dp(12), dp(5));
        root.setBackgroundColor(chrome);
        setContentView(root);

        // ILSpy-style menu/tool bar.
        LinearLayout toolbar = new LinearLayout(this);
        toolbar.setOrientation(LinearLayout.HORIZONTAL);
        toolbar.setGravity(Gravity.CENTER_VERTICAL);
        TextView brand = text("ILSpy", 23, white, true);
        toolbar.addView(brand, new LinearLayout.LayoutParams(0, dp(50), 1));
        toolbar.addView(actionButton("Abrir", true, this::chooseFile));
        toolbar.addView(actionButton("Copiar", false, this::copyCode));
        root.addView(toolbar);
        Button rebuildEntry = actionButton("↻  RECOMPILAR DLL + SALVAR", true, this::recompileOrOpenEditor);
        LinearLayout.LayoutParams rebuildParams = new LinearLayout.LayoutParams(-1, dp(46));
        rebuildParams.topMargin = dp(5);
        root.addView(rebuildEntry, rebuildParams);
        Button saveSourceEntry = actionButton("↓  SALVAR CÓDIGO C# (.cs)", false, this::saveCSharpSource);
        LinearLayout.LayoutParams saveSourceParams = new LinearLayout.LayoutParams(-1, dp(42));
        saveSourceParams.topMargin = dp(4);
        root.addView(saveSourceEntry, saveSourceParams);

        assemblyName = text("Assemblies  ·  nenhum assembly aberto", 13, white, true);
        assemblyName.setPadding(dp(10), dp(8), dp(10), dp(8));
        assemblyName.setBackground(rounded(chrome2, border));
        root.addView(assemblyName);

        status = text("Inicializando motor…", 11, muted, false);
        status.setPadding(dp(4), dp(5), dp(4), dp(5));
        root.addView(status);

        search = new EditText(this);
        search.setSingleLine(true);
        search.setTextSize(13);
        search.setHint("Pesquisar tipo, classe ou método…");
        search.setHintTextColor(muted);
        search.setTextColor(white);
        search.setPadding(dp(12), 0, dp(12), 0);
        search.setBackground(rounded(chrome2, border));
        root.addView(search, new LinearLayout.LayoutParams(-1, dp(44)));
        search.addTextChangedListener(new TextWatcher() {
            @Override public void beforeTextChanged(CharSequence s, int st, int count, int after) { }
            @Override public void onTextChanged(CharSequence s, int st, int before, int count) {
                if (activeTab == TAB_EXPLORER) renderExplorer();
            }
            @Override public void afterTextChanged(Editable e) { }
        });

        tabBar = new LinearLayout(this);
        tabBar.setOrientation(LinearLayout.HORIZONTAL);
        tabBar.setPadding(0, dp(7), 0, dp(7));
        root.addView(tabBar);

        ScrollView bodyScroll = new ScrollView(this);
        body = new LinearLayout(this);
        body.setOrientation(LinearLayout.VERTICAL);
        body.setPadding(dp(9), dp(9), dp(9), dp(9));
        body.setBackground(rounded(chrome2, border));
        bodyScroll.addView(body);
        root.addView(bodyScroll, new LinearLayout.LayoutParams(-1, 0, 1));

        TextView footer = text("ICSharpCode.Decompiler  ·  ILSpy", 10, muted, false);
        footer.setGravity(Gravity.CENTER);
        footer.setPadding(0, dp(7), 0, dp(4));
        root.addView(footer);

        updateTabs();
        renderWelcome();
    }

    private void updateTabs() {
        tabBar.removeAllViews();
        addTab("Assemblies", TAB_EXPLORER);
        addTab("C# Code", TAB_CODE);
        addTab("Metadata", TAB_METADATA);
    }

    private void addTab(String caption, int tab) {
        Button button = new Button(this);
        button.setText(caption);
        button.setAllCaps(false);
        button.setTextSize(12);
        button.setTextColor(activeTab == tab ? Color.WHITE : muted);
        button.setBackground(rounded(activeTab == tab ? accent : chrome, 0));
        button.setOnClickListener(v -> {
            activeTab = tab;
            updateTabs();
            renderActiveTab();
        });
        tabBar.addView(button, new LinearLayout.LayoutParams(0, dp(42), 1));
    }

    private void renderWelcome() {
        body.removeAllViews();
        addSection("Assembly Explorer");
        addInfoRow("▸  Assemblies", "Abra uma DLL, EXE .NET ou WINMD para começar.");
        addInfoRow("▸  References", "As dependências do assembly aparecem na análise.");
        addInfoRow("▸  Namespaces", "Classes, interfaces, structs e enums são organizados por namespace.");
        activeTab = TAB_EXPLORER;
        updateTabs();
    }

    private void renderActiveTab() {
        if (activeTab == TAB_EXPLORER) renderExplorer();
        else if (activeTab == TAB_CODE) renderCode();
        else renderMetadata();
    }

    private void addSection(String caption) {
        TextView heading = text(caption.toUpperCase(Locale.ROOT), 10, Color.rgb(114, 190, 225), true);
        heading.setPadding(dp(5), dp(9), dp(5), dp(5));
        body.addView(heading);
    }

    private void addInfoRow(String caption, String description) {
        TextView row = text(caption + "\n" + description, 12, white, false);
        row.setPadding(dp(10), dp(9), dp(10), dp(9));
        row.setBackground(rounded(chrome, 0));
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2);
        params.bottomMargin = dp(5);
        body.addView(row, params);
    }

    private void renderExplorer() {
        body.removeAllViews();
        if (currentFile == null) {
            renderWelcome();
            return;
        }
        addSection("Assembly Explorer");
        addInfoRow("▾  " + displayName, "Assembly carregado · toque em um tipo para abrir o C#");
        addInfoRow("▸  Metadata", "Informações e cabeçalho do assembly");
        addSection("Namespaces and Types");
        String filter = search.getText() == null ? "" : search.getText().toString().trim().toLowerCase(Locale.ROOT);
        boolean any = false;
        for (TypeEntry entry : types) {
            if (!filter.isEmpty() && !entry.fullName.toLowerCase(Locale.ROOT).contains(filter)) continue;
            any = true;
            TextView row = text("  ◇  " + entry.name, 13, white, false);
            row.setSingleLine(true);
            row.setEllipsize(android.text.TextUtils.TruncateAt.END);
            row.setPadding(dp(12), dp(10), dp(8), dp(10));
            row.setBackground(rounded(chrome, 0));
            row.setOnClickListener(v -> openType(entry));
            LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2);
            params.leftMargin = dp(9);
            params.bottomMargin = dp(3);
            body.addView(row, params);
            TextView sub = text(entry.fullName, 10, muted, false);
            sub.setPadding(dp(36), 0, dp(5), dp(5));
            body.addView(sub);
        }
        if (!any) addInfoRow("Nenhum tipo encontrado", "Tente outra pesquisa.");
        addSection("References");
        addInfoRow("▸  Referências e dependências", "Consulte a aba Metadata para ver a inspeção retornada pelo motor.");
    }

    private void openType(TypeEntry entry) {
        try {
            selectedType = entry.fullName;
            String result = NativeBridge.nativeDecompileType(currentFile.getAbsolutePath(), selectedType);
            if (result == null || result.startsWith("ILSPY_ERROR:")) {
                status.setText(result == null ? "O motor não retornou código." : result);
                return;
            }
            decompiled = result;
            activeTab = TAB_CODE;
            updateTabs();
            renderCode();
            status.setText("Tipo selecionado · " + selectedType);
        } catch (Throwable error) {
            status.setText("Falha ao descompilar tipo: " + error.getMessage());
        }
    }

    private void renderCode() {
        final String preservedSource = sourceEditor == null ? decompiled : sourceEditor.getText().toString();
        body.removeAllViews();
        addSection("C# Decompiler");
        addInfoRow(selectedType.isEmpty() ? displayName : selectedType, "C# · código descompilado");
        if (decompiled.isEmpty()) {
            addInfoRow("Editor C# pronto", "Selecione um tipo no Explorer ou cole código C# abaixo.");
        }
        TextView editor = new TextView(this);
        editor.setText(highlightCSharp(decompiled));
        editor.setTextSize(12);
        editor.setTypeface(Typeface.MONOSPACE);
        editor.setTextIsSelectable(true);
        editor.setTextColor(editorFg);
        editor.setGravity(Gravity.TOP | Gravity.START);
        editor.setPadding(dp(12), dp(12), dp(12), dp(12));
        editor.setBackgroundColor(editorBg);
        editor.setHorizontallyScrolling(true);
        body.addView(editor, new LinearLayout.LayoutParams(-1, -2));
    }

    private SpannableString highlightCSharp(String source) {
        SpannableString styled = new SpannableString(source);
        colorMatches(styled, "\\b(using|namespace|public|private|protected|internal|static|class|interface|struct|enum|record|void|int|string|bool|byte|long|short|float|double|decimal|var|new|return|if|else|for|foreach|while|do|switch|case|break|continue|throw|try|catch|finally|async|await|override|virtual|abstract|sealed|readonly|const|this|base|null|true|false)\\b",
                Color.rgb(0, 82, 150));
        colorMatches(styled, "\"(?:\\\\.|[^\"\\\\])*\"", Color.rgb(163, 75, 35));
        colorMatches(styled, "'(?:\\\\.|[^'\\\\])'", Color.rgb(163, 75, 35));
        colorMatches(styled, "//[^\\n]*", Color.rgb(110, 130, 105));
        colorMatches(styled, "/\\*[\\s\\S]*?\\*/", Color.rgb(110, 130, 105));
        colorMatches(styled, "\\b[0-9]+\\b", Color.rgb(120, 70, 160));
        return styled;
    }

    private void colorMatches(SpannableString text, String regex, int color) {
        Matcher matcher = Pattern.compile(regex).matcher(text);
        while (matcher.find()) {
            text.setSpan(new ForegroundColorSpan(color), matcher.start(), matcher.end(), 0);
        }
    }

    private void renderMetadata() {
        body.removeAllViews();
        addSection("Assembly Metadata");
        if (currentFile == null) {
            addInfoRow("Nenhum assembly aberto", "Abra uma DLL, EXE .NET ou WINMD.");
            return;
        }
        addInfoRow("File", displayName);
        addInfoRow("Size", currentFile.length() + " bytes");
        addInfoRow("Location", currentFile.getAbsolutePath());
        addSection("ILSpy Inspection");
        TextView details = text(inspectResult.isEmpty() ? "Nenhum dado de inspeção." : inspectResult,
                11, white, false);
        details.setTypeface(Typeface.MONOSPACE);
        details.setTextIsSelectable(true);
        details.setGravity(Gravity.TOP | Gravity.START);
        details.setPadding(dp(10), dp(10), dp(10), dp(10));
        details.setBackground(rounded(chrome, 0));
        body.addView(details, new LinearLayout.LayoutParams(-1, -2));
    }

    private void chooseFile() {
        Intent picker = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        picker.setType("*/*");
        picker.addCategory(Intent.CATEGORY_OPENABLE);
        startActivityForResult(picker, PICK_ASSEMBLY);
    }

    private String queryDisplayName(Uri uri) {
        try (Cursor cursor = getContentResolver().query(uri, null, null, null, null)) {
            if (cursor != null && cursor.moveToFirst()) {
                int index = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME);
                if (index >= 0) return cursor.getString(index);
            }
        } catch (Exception ignored) { }
        String segment = uri.getLastPathSegment();
        return segment == null ? "assembly" : segment;
    }

    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request != PICK_ASSEMBLY || result != RESULT_OK || data == null || data.getData() == null) return;
        Uri uri = data.getData();
        displayName = queryDisplayName(uri);
        String lower = displayName.toLowerCase(Locale.ROOT);
        if (lower.endsWith(".zip") || lower.endsWith(".7z") || lower.endsWith(".rar")) {
            status.setText("Arquivo compactado selecionado");
            body.removeAllViews();
            addInfoRow("Este arquivo é um pacote, não um assembly.",
                    "Extraia o ZIP e selecione diretamente uma DLL, EXE gerenciado ou WINMD.");
            return;
        }
        if (!(lower.endsWith(".dll") || lower.endsWith(".exe") || lower.endsWith(".winmd"))) {
            status.setText("Formato não suportado");
            body.removeAllViews();
            addInfoRow("Selecione DLL, EXE ou WINMD",
                    "O ILSpy descompila assemblies .NET; arquivos nativos não contêm IL/C# para recuperar.");
            return;
        }
        try {
            File target = new File(getCacheDir(), "assembly-" + System.currentTimeMillis() + "-" + displayName.replaceAll("[^A-Za-z0-9._-]", "_"));
            try (InputStream input = getContentResolver().openInputStream(uri);
                 FileOutputStream output = new FileOutputStream(target)) {
                if (input == null) throw new IllegalStateException("Não foi possível ler o arquivo.");
                byte[] buffer = new byte[65536];
                int count;
                while ((count = input.read(buffer)) != -1) output.write(buffer, 0, count);
            }
            currentFile = target;
            sourceEditor = null;
            selectedType = "";
            status.setText("Analisando " + displayName + "…");
            assemblyName.setText("Assemblies  ·  " + displayName);
            inspectResult = NativeBridge.nativeInspect(target.getAbsolutePath());
            decompiled = NativeBridge.nativeDecompile(target.getAbsolutePath());
            if (decompiled == null || decompiled.startsWith("ILSPY_ERROR:")) {
                status.setText(decompiled == null ? "O motor não retornou resultado." : decompiled);
                types.clear();
                activeTab = TAB_METADATA;
                updateTabs();
                renderMetadata();
                return;
            }
            collectTypes(decompiled);
            status.setText("Assembly carregado · " + types.size() + " tipo(s)");
            activeTab = TAB_EXPLORER;
            updateTabs();
            renderExplorer();
        } catch (Throwable error) {
            status.setText("Falha ao importar: " + error.getClass().getSimpleName());
            body.removeAllViews();
            addInfoRow("Não foi possível abrir o assembly", String.valueOf(error.getMessage()));
        }
    }

    private void collectTypes(String source) {
        types.clear();
        Matcher nsMatcher = Pattern.compile("(?m)^\\s*namespace\\s+([A-Za-z_][A-Za-z0-9_.]*)").matcher(source);
        String namespace = nsMatcher.find() ? nsMatcher.group(1) : "";
        Pattern pattern = Pattern.compile("(?m)^\\s*(?:(?:public|private|protected|internal|static|abstract|sealed|partial|readonly|new|unsafe)\\s+)*(?:class|interface|struct|enum|record)\\s+([A-Za-z_][A-Za-z0-9_]*)");
        Matcher matcher = pattern.matcher(source);
        while (matcher.find()) {
            String name = matcher.group(1);
            String full = namespace.isEmpty() ? name : namespace + "." + name;
            boolean exists = false;
            for (TypeEntry type : types) if (type.fullName.equals(full)) exists = true;
            if (!exists) types.add(new TypeEntry(name, full));
        }
    }

    private void copyCode() {
        if (decompiled.isEmpty()) {
            status.setText("Não há código para copiar.");
            return;
        }
        ClipboardManager manager = (ClipboardManager)getSystemService(CLIPBOARD_SERVICE);
        manager.setPrimaryClip(ClipData.newPlainText("C# decompiled source", decompiled));
        status.setText("Código C# copiado.");
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



# ---------------------------------------------------------------------------
# On-device Roslyn recompilation. This is a separate feature; the existing
# ICSharpCode.Decompiler entry points are not replaced or modified.
# ---------------------------------------------------------------------------
engine_project = root / "native/ILSpyEngine/ILSpyEngine.csproj"
project_text = engine_project.read_text(encoding="utf-8")
if "Microsoft.CodeAnalysis.CSharp" not in project_text:
    package_block = """
  <ItemGroup>
    <PackageReference Include="Microsoft.CodeAnalysis.CSharp" Version="4.14.0" />
  </ItemGroup>
  <ItemGroup>
    <TrimmerRootAssembly Include="Microsoft.CodeAnalysis" />
    <TrimmerRootAssembly Include="Microsoft.CodeAnalysis.CSharp" />
  </ItemGroup>
"""
    project_text = project_text.replace("</Project>", package_block + "\n</Project>")
engine_project.write_text(project_text, encoding="utf-8")

compiler = root / "native/ILSpyEngine/RecompilerExports.cs"
compiler.write_text(r'''using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.Emit;

namespace ILSpy.Mobile.Engine;

public static class RecompilerExports
{
    private static IntPtr Utf8(string value) => Marshal.StringToCoTaskMemUTF8(value);

    [UnmanagedCallersOnly(EntryPoint = "ilspy_recompile_csharp")]
    public static IntPtr Recompile(
        IntPtr sourcePathPtr,
        IntPtr outputPathPtr,
        IntPtr referenceDirectoriesPtr,
        IntPtr outputKindPtr,
        IntPtr assemblyNamePtr)
    {
        try
        {
            string sourcePath = Marshal.PtrToStringUTF8(sourcePathPtr) ?? "";
            string outputPath = Marshal.PtrToStringUTF8(outputPathPtr) ?? "";
            string referenceDirectories = Marshal.PtrToStringUTF8(referenceDirectoriesPtr) ?? "";
            string outputKindText = Marshal.PtrToStringUTF8(outputKindPtr) ?? "dll";
            string assemblyName = Marshal.PtrToStringUTF8(assemblyNamePtr) ?? "RebuiltAssembly";

            if (!File.Exists(sourcePath))
                return Utf8("COMPILE_ERROR: O arquivo C# não existe.");
            if (string.IsNullOrWhiteSpace(outputPath))
                return Utf8("COMPILE_ERROR: Caminho de saída vazio.");

            OutputKind outputKind = outputKindText.ToLowerInvariant() switch
            {
                "exe" => OutputKind.ConsoleApplication,
                "winexe" => OutputKind.WindowsApplication,
                "winmdobj" => OutputKind.WindowsRuntimeMetadata,
                _ => OutputKind.DynamicallyLinkedLibrary
            };

            var references = new List<MetadataReference>();
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (string directory in referenceDirectories.Split(';', StringSplitOptions.RemoveEmptyEntries))
            {
                if (!Directory.Exists(directory))
                    continue;
                foreach (string file in Directory.EnumerateFiles(directory, "*", SearchOption.TopDirectoryOnly))
                {
                    string extension = Path.GetExtension(file);
                    if (!extension.Equals(".dll", StringComparison.OrdinalIgnoreCase) &&
                        !extension.Equals(".winmd", StringComparison.OrdinalIgnoreCase))
                        continue;
                    string fullPath = Path.GetFullPath(file);
                    if (seen.Add(fullPath))
                        references.Add(MetadataReference.CreateFromFile(fullPath));
                }
            }

            if (references.Count == 0)
                return Utf8("COMPILE_ERROR: Referências do .NET não foram encontradas no APK.");

            var parseOptions = new CSharpParseOptions(LanguageVersion.Preview);
            var tree = CSharpSyntaxTree.ParseText(File.ReadAllText(sourcePath), parseOptions, sourcePath);

            var options = new CSharpCompilationOptions(
                outputKind,
                optimizationLevel: OptimizationLevel.Release,
                allowUnsafe: true,
                deterministic: true,
                nullableContextOptions: NullableContextOptions.Enable);

            string safeName = new string(assemblyName
                .Select(ch => char.IsLetterOrDigit(ch) || ch == '_' ? ch : '_')
                .ToArray());
            if (string.IsNullOrWhiteSpace(safeName))
                safeName = "RebuiltAssembly";

            var compilation = CSharpCompilation.Create(
                safeName, new[] { tree }, references, options);

            string? outputDirectory = Path.GetDirectoryName(outputPath);
            if (!string.IsNullOrEmpty(outputDirectory))
                Directory.CreateDirectory(outputDirectory);

            EmitResult emitResult;
            using (var output = new FileStream(outputPath, FileMode.Create, FileAccess.Write, FileShare.None))
                emitResult = compilation.Emit(output);

            if (!emitResult.Success)
            {
                try { if (File.Exists(outputPath)) File.Delete(outputPath); } catch { }
                var diagnostics = emitResult.Diagnostics
                    .Where(d => d.Severity == DiagnosticSeverity.Error || d.IsWarningAsError)
                    .Take(80)
                    .Select(d =>
                    {
                        var line = d.Location.GetLineSpan().StartLinePosition;
                        return $"{d.Id} ({line.Line + 1},{line.Character + 1}): {d.GetMessage()}";
                    });
                return Utf8("COMPILE_ERROR\n" + string.Join("\n", diagnostics));
            }

            long bytes = new FileInfo(outputPath).Length;
            return Utf8($"COMPILE_OK\n{outputPath}\n{bytes} bytes\nNovo assembly gerado. Assinatura, recursos e identidade binária original não são preservados automaticamente.");
        }
        catch (Exception error)
        {
            return Utf8("COMPILE_ERROR: " + error.GetType().Name + ": " + error.Message);
        }
    }

    [UnmanagedCallersOnly(EntryPoint = "ilspy_recompiler_free_string")]
    public static void FreeString(IntPtr value)
    {
        if (value != IntPtr.Zero)
            Marshal.FreeCoTaskMem(value);
    }
}
''', encoding="utf-8")

# Bundle .NET reference assemblies as compile-time metadata for Roslyn.
engine_builder = root / "scripts/build-engine.sh"
builder_text = engine_builder.read_text(encoding="utf-8")
if "ILSPY_COMPILER_REFERENCE_ASSETS" not in builder_text:
    builder_text += r'''

# Bundle the .NET 10 reference pack for offline on-device C# compilation.
ILSPY_COMPILER_REFERENCE_ASSETS="$ROOT/app/src/main/assets/compiler-refs"
mkdir -p "$ILSPY_COMPILER_REFERENCE_ASSETS"
DOTNET_ROOT_VALUE="$(printenv DOTNET_ROOT || printf '/usr/share/dotnet')"
DOTNET_PACK_ROOT="$DOTNET_ROOT_VALUE/packs/Microsoft.NETCore.App.Ref"
ILSPY_REF_DIR="$(find "$DOTNET_PACK_ROOT" -type d -path '*/ref/net10.0' | sort -V | tail -n 1)"
if [ -z "$ILSPY_REF_DIR" ] || [ ! -d "$ILSPY_REF_DIR" ]; then
  echo "Could not find the .NET 10 reference assembly pack at $DOTNET_PACK_ROOT" >&2
  exit 1
fi
cp "$ILSPY_REF_DIR"/*.dll "$ILSPY_COMPILER_REFERENCE_ASSETS"/
echo "Bundled $(find "$ILSPY_COMPILER_REFERENCE_ASSETS" -maxdepth 1 -name '*.dll' | wc -l) .NET compiler references."
'''
engine_builder.write_text(builder_text, encoding="utf-8")

# Add native function pointers for Roslyn compilation and its separately freed result.
cpp = root / "app/src/main/cpp/native-lib.cpp"
cpp_source = cpp.read_text(encoding="utf-8")
cpp_source = cpp_source.replace(
    'void (*freeString)(char*) = nullptr;\n    std::string error',
    'void (*freeString)(char*) = nullptr;\n    char* (*recompile)(const char*, const char*, const char*, const char*, const char*) = nullptr;\n    void (*freeRecompilerString)(char*) = nullptr;\n    std::string error'
)
cpp_source = cpp_source.replace(
    'bindEngineSymbol(g_engine.freeString, "ilspy_engine_free_string");',
    'bindEngineSymbol(g_engine.freeString, "ilspy_engine_free_string");'
)
cpp_source = cpp_source.replace(
    'g_engine.freeString = nullptr;\n        const std::string message',
    'g_engine.freeString = nullptr;\n        g_engine.recompile = nullptr;\n        g_engine.freeRecompilerString = nullptr;\n        const std::string message'
)
if "char* (*recompile)(" not in cpp_source:
    raise SystemExit("Could not add compiler exports to the native engine API")
if "Java_com_ilspy_mobile_NativeBridge_nativeRecompile" not in cpp_source:
    cpp_source += r'''

extern "C" JNIEXPORT jstring JNICALL
Java_com_ilspy_mobile_NativeBridge_nativeRecompile(
        JNIEnv* env, jclass,
        jstring sourcePath, jstring outputPath, jstring referenceDirectories,
        jstring outputKind, jstring assemblyName) {
    if (!g_engine.loaded)
        return env->NewStringUTF("COMPILE_ERROR: ILSpy engine not initialized.");
    const std::string source = javaStringToUtf8(env, sourcePath);
    const std::string output = javaStringToUtf8(env, outputPath);
    const std::string references = javaStringToUtf8(env, referenceDirectories);
    const std::string kind = javaStringToUtf8(env, outputKind);
    const std::string name = javaStringToUtf8(env, assemblyName);
    char* raw = g_engine.recompile(source.c_str(), output.c_str(), references.c_str(),
                                   kind.c_str(), name.c_str());
    if (raw == nullptr)
        return env->NewStringUTF("COMPILE_ERROR: Native compiler returned an empty result.");
    std::string message(raw);
    g_engine.freeRecompilerString(raw);
    return env->NewStringUTF(message.c_str());
}
'''
cpp_source = cpp_source.replace(
    '    g_engine.loaded = true;\n    g_engine.error.clear();',
    """    // Optional Roslyn exports must never block the original ILSpy decompiler.
    dlerror();
    g_engine.recompile = reinterpret_cast<char* (*)(const char*, const char*, const char*, const char*, const char*)>(
        dlsym(g_engine.handle, "ilspy_recompile_csharp"));
    dlerror();
    g_engine.freeRecompilerString = reinterpret_cast<void (*)(char*)>(
        dlsym(g_engine.handle, "ilspy_recompiler_free_string"));
    g_engine.loaded = true;
    g_engine.error.clear();"""
)
cpp_source = cpp_source.replace(
    '    const std::string source = javaStringToUtf8(env, sourcePath);',
    '    if (g_engine.recompile == nullptr || g_engine.freeRecompilerString == nullptr)\n' +
    '        return env->NewStringUTF("COMPILE_ERROR: Optional Roslyn compiler exports are not available.");\n' +
    '    const std::string source = javaStringToUtf8(env, sourcePath);'
)
cpp.write_text(cpp_source, encoding="utf-8")

bridge = root / "app/src/main/java/com/ilspy/mobile/NativeBridge.java"
bridge_source = bridge.read_text(encoding="utf-8")
if "nativeRecompile(" not in bridge_source:
    bridge_source = bridge_source.replace(
        "static native String nativeDecompileType(String absolutePath, String fullTypeName);",
        "static native String nativeDecompileType(String absolutePath, String fullTypeName);\n" +
        "    static native String nativeRecompile(String sourcePath, String outputPath, String referenceDirectories, String outputKind, String assemblyName);"
    )
bridge.write_text(bridge_source, encoding="utf-8")

# Upgrade the workbench: editable C#, target selection, dependency import and
# Android's Save As document picker for the rebuilt output.
workbench = root / "app/src/main/java/com/ilspy/mobile/WorkbenchActivity.java"
ui = workbench.read_text(encoding="utf-8")
ui = ui.replace(
    "private static final int PICK_ASSEMBLY = 4102;",
    "private static final int PICK_ASSEMBLY = 4102;\n    private static final int PICK_OUTPUT = 4103;\n    private static final int PICK_REFERENCE = 4104;"
)
ui = ui.replace(
    "private EditText search;\n    private File currentFile;",
    "private EditText search, sourceEditor;\n    private File currentFile, pendingExport;\n    private String pendingExportName = \"\";\n    private boolean buildRunning;"
)
ui = ui.replace(
    "drawable.setColor(fill);\n        drawable.setCornerRadius(dp(8));",
    """int top = Color.rgb(Math.min(255, Color.red(fill) + 13),
                Math.min(255, Color.green(fill) + 13), Math.min(255, Color.blue(fill) + 13));
        int bottom = Color.rgb(Math.max(0, Color.red(fill) - 15),
                Math.max(0, Color.green(fill) - 15), Math.max(0, Color.blue(fill) - 15));
        drawable.setColors(new int[] { top, fill, bottom });
        drawable.setOrientation(GradientDrawable.Orientation.TOP_BOTTOM);
        drawable.setCornerRadius(dp(8));"""
)
ui = ui.replace(
    "button.setBackground(rounded(primary ? accent : chrome2, primary ? 0 : border));\n        button.setOnClickListener",
    "button.setBackground(rounded(primary ? accent : chrome2, primary ? Color.rgb(116, 190, 232) : border));\n        button.setElevation(dp(3));\n        button.setOnClickListener"
)
ui = ui.replace(
    'toolbar.addView(actionButton("Abrir", true, this::chooseFile));\n        toolbar.addView(actionButton("Copiar", false, this::copyCode));',
    'toolbar.addView(actionButton("▣ Abrir", true, this::chooseFile));\n        toolbar.addView(actionButton("⚒ Build", false, this::openCodeEditor));\n        toolbar.addView(actionButton("＋ Ref", false, this::chooseReference));'
)

old_render = """TextView editor = new TextView(this);
        editor.setText(highlightCSharp(decompiled));
        editor.setTextSize(12);
        editor.setTypeface(Typeface.MONOSPACE);
        editor.setTextIsSelectable(true);
        editor.setTextColor(editorFg);
        editor.setGravity(Gravity.TOP | Gravity.START);
        editor.setPadding(dp(12), dp(12), dp(12), dp(12));
        editor.setBackgroundColor(editorBg);
        editor.setHorizontallyScrolling(true);
        body.addView(editor, new LinearLayout.LayoutParams(-1, -2));"""
new_render = """LinearLayout buildBar = new LinearLayout(this);
        buildBar.setOrientation(LinearLayout.HORIZONTAL);
        buildBar.addView(actionButton("DLL", true, () -> recompileSource("dll")),
                new LinearLayout.LayoutParams(0, dp(44), 1));
        buildBar.addView(actionButton("EXE", false, () -> recompileSource("exe")),
                new LinearLayout.LayoutParams(0, dp(44), 1));
        buildBar.addView(actionButton("WINMDOBJ*", false, () -> recompileSource("winmdobj")),
                new LinearLayout.LayoutParams(0, dp(44), 1));
        body.addView(buildBar);
        TextView editHint = text("Editor C# · altere o código e escolha o formato para recompilar",
                10, muted, false);
        editHint.setPadding(dp(4), dp(7), dp(4), dp(5));
        body.addView(editHint);
        sourceEditor = new EditText(this);
        sourceEditor.setText(highlightCSharp(preservedSource));
        sourceEditor.setTextSize(12);
        sourceEditor.setTypeface(Typeface.MONOSPACE);
        sourceEditor.setTextColor(editorFg);
        sourceEditor.setHintTextColor(Color.rgb(125, 130, 135));
        sourceEditor.setGravity(Gravity.TOP | Gravity.START);
        sourceEditor.setPadding(dp(12), dp(12), dp(12), dp(12));
        sourceEditor.setBackground(rounded(editorBg, Color.rgb(199, 195, 177)));
        sourceEditor.setMinHeight(dp(440));
        sourceEditor.setHorizontallyScrolling(true);
        sourceEditor.setInputType(android.text.InputType.TYPE_CLASS_TEXT |
                android.text.InputType.TYPE_TEXT_FLAG_MULTI_LINE |
                android.text.InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        sourceEditor.setSingleLine(false);
        sourceEditor.setTextIsSelectable(false);
        sourceEditor.setFocusableInTouchMode(true);
        sourceEditor.setFocusable(true);
        sourceEditor.setClickable(false);
        sourceEditor.setElevation(dp(2));
        body.addView(sourceEditor, new LinearLayout.LayoutParams(-1, -2));
        TextView refHint = text("Dependências adicionais: use ＋ Ref na barra superior. " +
                "WINMDOBJ* é intermediário: gerar um .winmd final exige WinMDExp no Windows. " +
                "A saída é um assembly novo; recursos, strong-name e identidade original não são preservados automaticamente.",
                10, muted, false);
        refHint.setPadding(dp(4), dp(8), dp(4), dp(8));
        body.addView(refHint);"""
if old_render not in ui:
    raise SystemExit("Could not locate the old read-only code viewer")
ui = ui.replace(old_render, new_render)

ui = ui.replace(
    'if (!(lower.endsWith(".dll") || lower.endsWith(".exe") || lower.endsWith(".winmd"))) {',
    'if (!(lower.endsWith(".dll") || lower.endsWith(".exe") || lower.endsWith(".winmd") || lower.endsWith(".netmodule") || lower.endsWith(".winmdobj"))) {'
)

old_start = """super.onActivityResult(request, result, data);
        if (request != PICK_ASSEMBLY || result != RESULT_OK || data == null || data.getData() == null) return;
        Uri uri = data.getData();"""
new_start = """super.onActivityResult(request, result, data);
        if (request == PICK_OUTPUT) {
            if (result != RESULT_OK || data == null || data.getData() == null) {
                status.setText("Salvamento cancelado. Compile novamente para abrir o seletor de destino.");
                pendingExport = null;
                pendingExportName = "";
                return;
            }
            if (pendingExport == null || !pendingExport.isFile() || pendingExport.length() == 0) {
                status.setText("Não há arquivo compilado válido para exportar.");
                return;
            }
            Uri outputUri = data.getData();
            try (InputStream input = new java.io.FileInputStream(pendingExport);
                 java.io.OutputStream output = getContentResolver().openOutputStream(outputUri, "w")) {
                if (output == null) throw new IllegalStateException("O Android não abriu o destino de gravação.");
                byte[] buffer = new byte[65536];
                int count;
                while ((count = input.read(buffer)) != -1) output.write(buffer, 0, count);
                output.flush();
                status.setText(pendingExportName.toLowerCase(Locale.ROOT).endsWith(".cs")
                        ? "Código C# salvo: " + pendingExportName
                        : "Arquivo recompilado salvo: " + pendingExportName);
                pendingExport = null;
                pendingExportName = "";
            } catch (Throwable error) {
                status.setText("Falha ao exportar: " + error.getClass().getSimpleName() + ": " + error.getMessage());
            }
            return;
        }
        if (result != RESULT_OK || data == null || data.getData() == null) return;
        Uri uri = data.getData();
        if (request == PICK_REFERENCE) {
            importReference(uri);
            return;
        }
        if (request != PICK_ASSEMBLY) return;"""
if old_start not in ui:
    raise SystemExit("Could not locate onActivityResult start")
ui = ui.replace(old_start, new_start)

helper_marker = "    private void chooseFile() {"
helpers = r'''    private File compilerReferencesDirectory() throws Exception {
        File directory = new File(getFilesDir(), "ilspy-compiler-refs");
        if (!directory.exists() && !directory.mkdirs())
            throw new IllegalStateException("Não foi possível criar a pasta de referências.");
        String[] assets = getAssets().list("compiler-refs");
        if (assets == null || assets.length == 0)
            throw new IllegalStateException("O APK não contém as referências do .NET para recompilar.");
        File sample = new File(directory, assets[0]);
        if (!sample.exists()) {
            for (String name : assets) {
                if (!name.endsWith(".dll")) continue;
                try (InputStream input = getAssets().open("compiler-refs/" + name);
                     FileOutputStream output = new FileOutputStream(new File(directory, name))) {
                    byte[] buffer = new byte[65536];
                    int count;
                    while ((count = input.read(buffer)) != -1) output.write(buffer, 0, count);
                }
            }
        }
        return directory;
    }

    private File userReferencesDirectory() throws Exception {
        File directory = new File(getFilesDir(), "ilspy-user-refs");
        if (!directory.exists() && !directory.mkdirs())
            throw new IllegalStateException("Não foi possível criar a pasta de dependências.");
        return directory;
    }

    private void chooseReference() {
        Intent picker = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        picker.setType("*/*");
        picker.addCategory(Intent.CATEGORY_OPENABLE);
        startActivityForResult(picker, PICK_REFERENCE);
    }

    private void importReference(Uri uri) {
        String name = queryDisplayName(uri);
        String lower = name.toLowerCase(Locale.ROOT);
        if (!(lower.endsWith(".dll") || lower.endsWith(".winmd"))) {
            status.setText("Dependência ignorada: escolha DLL ou WINMD.");
            return;
        }
        try {
            File target = new File(userReferencesDirectory(), name.replaceAll("[^A-Za-z0-9._-]", "_"));
            try (InputStream input = getContentResolver().openInputStream(uri);
                 FileOutputStream output = new FileOutputStream(target)) {
                if (input == null) throw new IllegalStateException("Não foi possível ler a dependência.");
                byte[] buffer = new byte[65536];
                int count;
                while ((count = input.read(buffer)) != -1) output.write(buffer, 0, count);
            }
            status.setText("Referência adicionada: " + target.getName());
        } catch (Throwable error) {
            status.setText("Falha ao adicionar referência: " + error.getMessage());
        }
    }

    private void saveCSharpSource() {
        String source = sourceEditor != null ? sourceEditor.getText().toString() : decompiled;
        if (source == null || source.trim().isEmpty()) {
            status.setText("Não há código C# descompilado para salvar.");
            return;
        }
        try {
            String base = selectedType == null || selectedType.trim().isEmpty()
                    ? displayName.replaceFirst("(?i)\\.(dll|exe|winmd|netmodule|winmdobj)$", "")
                    : selectedType.substring(selectedType.lastIndexOf('.') + 1);
            base = base.replaceAll("[^A-Za-z0-9._-]", "_");
            if (base.isEmpty()) base = "DecompiledSource";
            File folder = new File(getCacheDir(), "ilspy-csharp-export");
            if (!folder.exists() && !folder.mkdirs())
                throw new IllegalStateException("Não foi possível criar a pasta de exportação.");
            File cs = new File(folder, base + ".cs");
            try (FileOutputStream output = new FileOutputStream(cs, false)) {
                output.write(source.getBytes(java.nio.charset.StandardCharsets.UTF_8));
                output.flush();
            }
            pendingExport = cs;
            pendingExportName = cs.getName();
            Intent create = new Intent(Intent.ACTION_CREATE_DOCUMENT);
            create.addCategory(Intent.CATEGORY_OPENABLE);
            create.setType("text/plain");
            create.putExtra(Intent.EXTRA_TITLE, cs.getName());
            status.setText("Código C# preparado · escolha onde salvar o .cs");
            startActivityForResult(create, PICK_OUTPUT);
        } catch (Throwable error) {
            status.setText("Falha ao salvar C#: " + error.getMessage());
        }
    }

    private void openCodeEditor() {
        if (currentFile == null) {
            status.setText("Abra primeiro uma DLL, EXE .NET ou WINMD.");
            return;
        }
        activeTab = TAB_CODE;
        updateTabs();
        renderCode();
        if (sourceEditor != null) sourceEditor.requestFocus();
    }

    private void recompileOrOpenEditor() {
        if (currentFile == null) {
            status.setText("Primeiro abra uma DLL, EXE .NET ou WINMD.");
            return;
        }
        if (activeTab != TAB_CODE || sourceEditor == null) {
            activeTab = TAB_CODE;
            updateTabs();
            renderCode();
        }
        if (sourceEditor == null) {
            status.setText("Não foi possível carregar o editor C#.");
            return;
        }
        recompileSource("dll");
    }

    private void recompileSource(String kind) {
        if (buildRunning) {
            status.setText("Uma compilação já está em andamento.");
            return;
        }
        if (currentFile == null || sourceEditor == null) {
            status.setText("Abra um assembly e entre na aba C# Code antes de recompilar.");
            return;
        }
        final String source = sourceEditor.getText().toString();
        if (source.trim().isEmpty()) {
            status.setText("O editor C# está vazio.");
            return;
        }

        final String baseName;
        final File outputFile;
        try {
            String base = displayName.replaceFirst("(?i)\\\\.(dll|exe|winmd|netmodule|winmdobj)$", "");
            if (base.isEmpty()) base = "RebuiltAssembly";
            baseName = base;
            String extension = kind.equals("winmdobj") ? ".winmdobj" : "." + kind;
            File work = new File(getCacheDir(), "ilspy-recompile");
            if (!work.exists() && !work.mkdirs())
                throw new IllegalStateException("Não foi possível criar a pasta de compilação.");
            outputFile = new File(work, baseName + "-rebuilt-" +
                    System.currentTimeMillis() + extension);
        } catch (Throwable error) {
            status.setText("Falha ao preparar compilação: " + error.getMessage());
            return;
        }

        buildRunning = true;
        status.setText("Roslyn compilando " + kind.toUpperCase(Locale.ROOT) + " em segundo plano…");
        Thread worker = new Thread(() -> {
            try {
                File sourceFile = new File(outputFile.getParentFile(),
                        "EditedAssembly-" + System.currentTimeMillis() + ".cs");
                try (FileOutputStream stream = new FileOutputStream(sourceFile, false)) {
                    stream.write(source.getBytes(java.nio.charset.StandardCharsets.UTF_8));
                    stream.flush();
                    stream.getFD().sync();
                }

                File frameworkRefs = compilerReferencesDirectory();
                File customRefs = userReferencesDirectory();
                String references = frameworkRefs.getAbsolutePath() + ";" +
                        customRefs.getAbsolutePath();
                String result = NativeBridge.nativeRecompile(
                        sourceFile.getAbsolutePath(), outputFile.getAbsolutePath(),
                        references, kind, baseName);

                if (result == null || !result.startsWith("COMPILE_OK")) {
                    final String diagnostic = result == null
                            ? "O compilador não retornou resultado." : result;
                    runOnUiThread(() -> {
                        buildRunning = false;
                        status.setText("Falha na recompilação · consulte Build Output");
                        showBuildDiagnostics(diagnostic);
                    });
                    return;
                }
                if (!outputFile.isFile() || outputFile.length() == 0) {
                    runOnUiThread(() -> {
                        buildRunning = false;
                        status.setText("O compilador informou sucesso, mas não criou o arquivo.");
                        showBuildDiagnostics(result);
                    });
                    return;
                }

                runOnUiThread(() -> {
                    buildRunning = false;
                    if (isFinishing() || isDestroyed()) return;
                    pendingExport = outputFile;
                    pendingExportName = outputFile.getName();
                    status.setText("Compilação concluída · " + outputFile.length() +
                            " bytes · abrindo seletor para salvar…");
                    try {
                        Intent create = new Intent(Intent.ACTION_CREATE_DOCUMENT);
                        create.addCategory(Intent.CATEGORY_OPENABLE);
                        create.setType("application/octet-stream");
                        create.putExtra(Intent.EXTRA_TITLE, outputFile.getName());
                        startActivityForResult(create, PICK_OUTPUT);
                    } catch (Throwable error) {
                        status.setText("Compilado, mas o seletor não abriu: " + error.getMessage());
                        showBuildDiagnostics("O arquivo continua no cache privado do app:\\n" +
                                outputFile.getAbsolutePath() + "\\n\\n" + error);
                    }
                });
            } catch (Throwable error) {
                runOnUiThread(() -> {
                    buildRunning = false;
                    status.setText("Falha no recompilador: " + error.getClass().getSimpleName() +
                            ": " + error.getMessage());
                    showBuildDiagnostics(status.getText().toString());
                });
            }
        }, "ilspy-recompiler");
        worker.setPriority(Thread.NORM_PRIORITY);
        worker.start();
    }

    private void showBuildDiagnostics(String message) {
        body.removeAllViews();
        addSection("Build Output");
        TextView details = text(message, 11, white, false);
        details.setTypeface(Typeface.MONOSPACE);
        details.setGravity(Gravity.TOP | Gravity.START);
        details.setTextIsSelectable(true);
        details.setPadding(dp(10), dp(10), dp(10), dp(10));
        details.setBackground(rounded(chrome, border));
        body.addView(details, new LinearLayout.LayoutParams(-1, -2));
        body.addView(actionButton("Voltar ao código", true, this::renderCode));
    }

'''
if helper_marker not in ui:
    raise SystemExit("Could not locate chooseFile insertion point")
ui = ui.replace(helper_marker, helpers + helper_marker)

workbench.write_text(ui, encoding="utf-8")

print("Added Roslyn editor/recompiler for managed DLL/EXE and WINMDOBJ intermediate; ILSpy decompiler code untouched.")

print("Patched to bundle engine as ABI-specific APK assets and load via dlopen; bridge no longer DT_NEEDED-links engine.")
