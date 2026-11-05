#!/usr/bin/env python3
"""Adds native ELF analysis, DEX round-trip editing, and ARSC XML inspection."""
from pathlib import Path
import re
import sys

root = Path(sys.argv[1]).resolve()
java_dir = root / "app/src/main/java/com/ilspy/mobile"
cpp = root / "app/src/main/cpp/native-lib.cpp"
cmake = root / "app/src/main/cpp/CMakeLists.txt"
bridge = java_dir / "NativeBridge.java"
workbench = java_dir / "WorkbenchActivity.java"
manifest = root / "app/src/main/AndroidManifest.xml"
gradle = root / "app/build.gradle"

for required in (cpp, cmake, bridge, workbench, manifest, gradle):
    if not required.is_file():
        raise SystemExit("Required Android source file is missing: " + str(required))

# Resolve libraries from Maven Central and JitPack (Apktool's Smali fork).
settings_candidates = [root / "settings.gradle", root / "settings.gradle.kts"]
settings = next((p for p in settings_candidates if p.is_file()), None)
if settings:
    text = settings.read_text(encoding="utf-8")
    if "dependencyResolutionManagement" in text:
        start = text.index("dependencyResolutionManagement")
        block = text[start:]
        match = re.search(r"repositories\s*\{", block)
        if not match:
            raise SystemExit("Could not locate dependencyResolutionManagement.repositories in settings file")
        pos = start + match.end()
        additions = ""
        if "mavenCentral()" not in block:
            additions += "\n        mavenCentral()"
        if "jitpack.io" not in block:
            if settings.suffix == ".kts":
                additions += '\n        maven { url = uri("https://jitpack.io") }'
            else:
                additions += "\n        maven { url 'https://jitpack.io' }"
        if additions:
            text = text[:pos] + additions + text[pos:]
            settings.write_text(text, encoding="utf-8")
    elif "mavenCentral()" not in text:
        raise SystemExit("settings file has no dependencyResolutionManagement block; refusing a risky repository rewrite")
else:
    raise SystemExit("settings.gradle(.kts) was not found")

g = gradle.read_text(encoding="utf-8")
dependency = "    implementation 'org.apktool:apktool-lib:3.0.3'\n    implementation 'com.github.iBotPeaches.smali:smali:b6365a84f4'"
if "org.apktool:apktool-lib" not in g:
    match = re.search(r"(?m)^dependencies\s*\{", g)
    if match:
        pos = match.end()
        g = g[:pos] + "\n" + dependency + g[pos:]
    else:
        g += "\n\ndependencies {\n" + dependency + "\n}\n"
    gradle.write_text(g, encoding="utf-8")

# CMake builds Capstone as a static library for the app's ARM64 and x86_64 ABIs.
c = cmake.read_text(encoding="utf-8")
if "FetchContent_Declare(capstone" not in c:
    c += r'''

include(FetchContent)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
set(BUILD_STATIC_LIBS ON CACHE BOOL "" FORCE)
set(CAPSTONE_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(CAPSTONE_BUILD_CSTOOL OFF CACHE BOOL "" FORCE)
set(CAPSTONE_ARCHITECTURE_DEFAULT OFF CACHE BOOL "" FORCE)
set(CAPSTONE_ARM_SUPPORT ON CACHE BOOL "" FORCE)
set(CAPSTONE_ARM64_SUPPORT ON CACHE BOOL "" FORCE)
set(CAPSTONE_X86_SUPPORT ON CACHE BOOL "" FORCE)
set(CAPSTONE_X86_REDUCE OFF CACHE BOOL "" FORCE)
FetchContent_Declare(capstone
    GIT_REPOSITORY https://github.com/capstone-engine/capstone.git
    GIT_TAG 5.0.6
)
FetchContent_MakeAvailable(capstone)
target_link_libraries(ilspy_bridge PRIVATE capstone_static)
'''
    cmake.write_text(c, encoding="utf-8")

# JNI exposes bounded instruction decoding. It never loads or executes the .so being inspected.
native = cpp.read_text(encoding="utf-8")
if "Java_com_ilspy_mobile_NativeBridge_nativeDisassembleElf" not in native:
    include_anchor = "#include <dlfcn.h>"
    if include_anchor not in native:
        raise SystemExit("Could not find dlfcn include anchor in native bridge")
    native = native.replace(
        include_anchor,
        include_anchor + "\n#include <capstone/capstone.h>\n#include <sstream>\n#include <iomanip>\n#include <vector>\n#include <algorithm>",
        1
    )
    native += r'''

extern "C" JNIEXPORT jstring JNICALL
Java_com_ilspy_mobile_NativeBridge_nativeDisassembleElf(
        JNIEnv* env, jclass, jstring pathValue, jlong offsetValue,
        jint lengthValue, jint architectureValue) {
    if (offsetValue < 0 || lengthValue <= 0 || lengthValue > 131072)
        return env->NewStringUTF("ELF_ERROR: Invalid code range (limit: 128 KiB).");

    const std::string path = javaStringToUtf8(env, pathValue);
    std::ifstream input(path, std::ios::binary);
    if (!input) return env->NewStringUTF("ELF_ERROR: Cannot open shared object.");

    input.seekg(static_cast<std::streamoff>(offsetValue), std::ios::beg);
    if (!input) return env->NewStringUTF("ELF_ERROR: Cannot seek to executable segment.");

    std::vector<uint8_t> code(static_cast<size_t>(lengthValue));
    input.read(reinterpret_cast<char*>(code.data()), static_cast<std::streamsize>(code.size()));
    code.resize(static_cast<size_t>(input.gcount()));
    if (code.empty()) return env->NewStringUTF("ELF_ERROR: Executable segment is empty.");

    cs_arch arch;
    cs_mode mode;
    switch (architectureValue) {
        case 1: arch = CS_ARCH_ARM64; mode = CS_MODE_ARM; break;
        case 2: arch = CS_ARCH_ARM; mode = CS_MODE_ARM; break;
        case 3: arch = CS_ARCH_X86; mode = CS_MODE_32; break;
        case 4: arch = CS_ARCH_X86; mode = CS_MODE_64; break;
        default: return env->NewStringUTF("ELF_ERROR: Unsupported ELF machine architecture.");
    }

    csh handle = 0;
    if (cs_open(arch, mode, &handle) != CS_ERR_OK)
        return env->NewStringUTF("ELF_ERROR: Capstone could not initialize this architecture.");

    cs_option(handle, CS_OPT_DETAIL, CS_OPT_OFF);
    cs_insn* instructions = nullptr;
    const size_t count = cs_disasm(handle, code.data(), code.size(),
            static_cast<uint64_t>(offsetValue), 600, &instructions);

    std::ostringstream output;
    output << "Capstone 5.0.6 · file offsets · " << count << " decoded instructions\n";
    for (size_t i = 0; i < count; ++i) {
        output << "0x" << std::hex << instructions[i].address << "  "
               << std::left << std::setw(10) << instructions[i].mnemonic
               << instructions[i].op_str << "\n";
    }
    if (count == 0) output << "No valid instruction was decoded in this segment.\n";

    if (instructions != nullptr) cs_free(instructions, count);
    cs_close(&handle);

    const std::string result = output.str();
    return env->NewStringUTF(result.c_str());
}
'''
    cpp.write_text(native, encoding="utf-8")

# Add the JNI declaration without rewriting the existing bridge.
b = bridge.read_text(encoding="utf-8")
decl = "    static native String nativeDisassembleElf(String absolutePath, long offset, int length, int architecture);"
if "nativeDisassembleElf(" not in b:
    idx = b.rfind("}")
    if idx < 0:
        raise SystemExit("NativeBridge class closing brace was not found")
    b = b[:idx] + decl + "\n" + b[idx:]
    bridge.write_text(b, encoding="utf-8")

# Add a launcher action to the existing workbench after its generated toolbar is finalized.
w = workbench.read_text(encoding="utf-8")
if "BinaryWorkbenchActivity.class" not in w:
    anchor = "root.addView(toolbar);"
    if anchor not in w:
        raise SystemExit("Could not find the finalized workbench toolbar container")
    w = w.replace(anchor, anchor + '\n        Button binaryLabButton = actionButton("▣  BINARY LAB · SO / DEX / ARSC", false, () ->\n                startActivity(new Intent(this, BinaryWorkbenchActivity.class)));\n        root.addView(binaryLabButton, new LinearLayout.LayoutParams(-1, dp(42)));', 1)
    workbench.write_text(w, encoding="utf-8")

# Register the additional non-exported screen.
m = manifest.read_text(encoding="utf-8")
if "BinaryWorkbenchActivity" not in m:
    m = m.replace(
        "</application>",
        '        <activity android:name=".BinaryWorkbenchActivity" android:exported="false" />\n    </application>'
    )
    manifest.write_text(m, encoding="utf-8")

activity = r'''package com.ilspy.mobile;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import brut.androlib.ApkDecoder;
import brut.androlib.Config;
import brut.androlib.smali.SmaliDecoder;
import com.android.tools.smali.smali.Smali;
import com.android.tools.smali.smali.SmaliOptions;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.RandomAccessFile;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Locale;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;

/** Binary analysis tools. Files are copied into private cache before parsing. */
public final class BinaryWorkbenchActivity extends Activity {
    private static final int PICK_FILE = 8201;
    private static final int SAVE_FILE = 8202;
    private final int bg = Color.rgb(25, 30, 38);
    private final int panel = Color.rgb(37, 45, 56);
    private final int accent = Color.rgb(38, 132, 190);
    private final int fg = Color.rgb(238, 243, 248);
    private final int dim = Color.rgb(164, 177, 192);

    private LinearLayout root, content, classList;
    private TextView status, output;
    private EditText editor, patchOffset, patchBytes;
    private File opened, workspace, selectedSmali, pendingSave;
    private String pendingSaveName = "";
    private int currentKind;

    private static final int KIND_NONE = 0, KIND_SO = 1, KIND_DEX = 2, KIND_ARSC = 3;
    private static final class ElfInfo {
        int bits;
        int machine;
        int architecture;
        long executableOffset;
        long executableSize;
        long virtualAddress;
        String architectureName;
    }

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().setStatusBarColor(bg);
        getWindow().setNavigationBarColor(bg);
        buildUi();
    }

    private int dp(float n) {
        return (int)(n * getResources().getDisplayMetrics().density + .5f);
    }

    private TextView label(String value, int size, int color, boolean bold) {
        TextView v = new TextView(this);
        v.setText(value);
        v.setTextSize(size);
        v.setTextColor(color);
        v.setGravity(Gravity.CENTER_VERTICAL);
        if (bold) v.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        return v;
    }

    private Button button(String value, boolean primary, Runnable task) {
        Button b = new Button(this);
        b.setText(value);
        b.setAllCaps(false);
        b.setTextSize(12);
        b.setTextColor(fg);
        b.setMinHeight(dp(44));
        b.setBackgroundColor(primary ? accent : panel);
        b.setOnClickListener(v -> task.run());
        return b;
    }

    private void buildUi() {
        root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(12), dp(8), dp(12), dp(8));
        root.setBackgroundColor(bg);
        setContentView(root);

        LinearLayout top = new LinearLayout(this);
        top.setOrientation(LinearLayout.HORIZONTAL);
        TextView title = label("ILSpy  /  BINARY LAB", 18, fg, true);
        top.addView(title, new LinearLayout.LayoutParams(0, dp(48), 1));
        top.addView(button("Abrir arquivo", true, this::chooseFile));
        root.addView(top);
        status = label("Suporte: .so · .dex · .arsc", 11, dim, false);
        status.setPadding(dp(4), dp(6), dp(4), dp(8));
        root.addView(status);

        ScrollView scroll = new ScrollView(this);
        content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setPadding(dp(8), dp(8), dp(8), dp(12));
        content.setBackgroundColor(panel);
        scroll.addView(content);
        root.addView(scroll, new LinearLayout.LayoutParams(-1, 0, 1));

        output = label("Abra um .so, .dex ou resources.arsc.\n\n" +
                "ELF: cabeçalho e desmontagem de instruções.\n" +
                "DEX: classes Smali editáveis e montagem de volta para DEX.\n" +
                "ARSC: decodificação da tabela para XML editável/exportável.", 12, fg, false);
        output.setTypeface(Typeface.MONOSPACE);
        output.setGravity(Gravity.TOP | Gravity.START);
        output.setTextIsSelectable(true);
        output.setPadding(dp(10), dp(10), dp(10), dp(10));
        content.addView(output, new LinearLayout.LayoutParams(-1, -2));
    }

    private void chooseFile() {
        Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        i.setType("*/*");
        i.addCategory(Intent.CATEGORY_OPENABLE);
        startActivityForResult(i, PICK_FILE);
    }

    private String displayName(Uri uri) {
        try (android.database.Cursor c = getContentResolver().query(uri, null, null, null, null)) {
            if (c != null && c.moveToFirst()) {
                int col = c.getColumnIndex(android.provider.OpenableColumns.DISPLAY_NAME);
                if (col >= 0) return c.getString(col);
            }
        } catch (Throwable ignored) { }
        return "binary-" + System.currentTimeMillis();
    }

    private File copyUri(Uri uri, String name) throws Exception {
        String safe = name.replaceAll("[^A-Za-z0-9._-]", "_");
        if (safe.isEmpty()) safe = "input.bin";
        File target = new File(getCacheDir(), "binary-lab-" + safe);
        try (java.io.InputStream in = getContentResolver().openInputStream(uri);
             FileOutputStream out = new FileOutputStream(target, false)) {
            if (in == null) throw new IllegalStateException("O provedor não abriu o arquivo.");
            byte[] buffer = new byte[65536];
            int count;
            while ((count = in.read(buffer)) != -1) out.write(buffer, 0, count);
        }
        return target;
    }

    @Override protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request == SAVE_FILE) {
            if (result != RESULT_OK || data == null || data.getData() == null) {
                status.setText("Salvamento cancelado.");
                pendingSave = null;
                pendingSaveName = "";
                return;
            }
            if (pendingSave == null || !pendingSave.isFile() || pendingSave.length() == 0) {
                status.setText("Arquivo temporário para salvar não existe ou está vazio.");
                return;
            }
        } else if (result != RESULT_OK || data == null || data.getData() == null) {
            return;
        }
        Uri uri = data.getData();
        if (request == PICK_FILE) {
            String name = displayName(uri);
            status.setText("Lendo " + name + "…");
            new Thread(() -> {
                try {
                    File f = copyUri(uri, name);
                    runOnUiThread(() -> openBinary(f));
                } catch (Throwable error) {
                    runOnUiThread(() -> status.setText("Falha ao abrir: " + error.getMessage()));
                }
            }, "binary-import").start();
        } else if (request == SAVE_FILE && pendingSave != null) {
            try (FileInputStream in = new FileInputStream(pendingSave);
                 java.io.OutputStream out = getContentResolver().openOutputStream(uri, "w")) {
                if (out == null) throw new IllegalStateException("Não foi possível gravar no destino.");
                byte[] buffer = new byte[65536];
                int count;
                while ((count = in.read(buffer)) != -1) out.write(buffer, 0, count);
                out.flush();
                status.setText("Salvo: " + pendingSaveName);
                pendingSave = null;
                pendingSaveName = "";
            } catch (Throwable error) {
                status.setText("Erro ao salvar: " + error.getMessage());
            }
        }
    }

    private void openBinary(File file) {
        opened = file;
        content.removeAllViews();
        editor = null;
        currentKind = KIND_NONE;
        String name = file.getName().toLowerCase(Locale.ROOT);
        try {
            if (name.endsWith(".so")) {
                currentKind = KIND_SO;
                openElf(file);
            } else if (name.endsWith(".dex")) {
                currentKind = KIND_DEX;
                openDex(file);
            } else if (name.endsWith(".arsc")) {
                currentKind = KIND_ARSC;
                openArsc(file);
            } else {
                output.setText("Formato não suportado por este módulo.\n\nEscolha .so, .dex ou resources.arsc.");
                content.addView(output);
            }
        } catch (Throwable error) {
            output.setText("Falha na análise:\n" + error.getClass().getSimpleName() + ": " + error.getMessage());
            content.addView(output);
            status.setText("Falha ao analisar " + file.getName());
        }
    }

    private static long u32(ByteBuffer b, int p) {
        return Integer.toUnsignedLong(b.getInt(p));
    }

    private ElfInfo readElf(File file) throws Exception {
        try (RandomAccessFile raf = new RandomAccessFile(file, "r")) {
            if (raf.length() < 16) throw new IllegalArgumentException("ELF pequeno demais.");
            byte[] ident = new byte[16];
            raf.readFully(ident);
            if (ident[0] != 0x7f || ident[1] != 'E' || ident[2] != 'L' || ident[3] != 'F')
                throw new IllegalArgumentException("Assinatura ELF inválida.");
            int elfClass = ident[4] & 255;
            int headerSize = elfClass == 2 ? 64 : elfClass == 1 ? 52 : 0;
            if (headerSize == 0) throw new IllegalArgumentException("Classe ELF inválida.");
            if (raf.length() < headerSize) throw new IllegalArgumentException("Cabeçalho ELF truncado.");
            byte[] h = new byte[headerSize];
            System.arraycopy(ident, 0, h, 0, ident.length);
            raf.readFully(h, ident.length, headerSize - ident.length);
            if ((h[5] & 255) != 1) throw new IllegalArgumentException("ELF big-endian não é suportado.");
            ByteBuffer b = ByteBuffer.wrap(h).order(ByteOrder.LITTLE_ENDIAN);
            ElfInfo info = new ElfInfo();
            info.bits = elfClass == 2 ? 64 : elfClass == 1 ? 32 : 0;
            if (info.bits == 0) throw new IllegalArgumentException("Classe ELF inválida.");
            info.machine = Short.toUnsignedInt(b.getShort(18));
            switch (info.machine) {
                case 183: info.architecture = 1; info.architectureName = "AArch64"; break;
                case 40: info.architecture = 2; info.architectureName = "ARM"; break;
                case 3: info.architecture = 3; info.architectureName = "x86"; break;
                case 62: info.architecture = 4; info.architectureName = "x86_64"; break;
                default: info.architectureName = "machine " + info.machine; break;
            }
            long phoff = info.bits == 64 ? b.getLong(32) : u32(b, 28);
            int phentsize = Short.toUnsignedInt(b.getShort(info.bits == 64 ? 54 : 42));
            int phnum = Short.toUnsignedInt(b.getShort(info.bits == 64 ? 56 : 44));
            int minEntry = info.bits == 64 ? 56 : 32;
            if (phentsize < minEntry || phnum > 1024 || phoff < 0 ||
                    phoff + (long)phentsize * phnum > raf.length())
                throw new IllegalArgumentException("Tabela de program headers inválida.");
            byte[] entry = new byte[phentsize];
            for (int i = 0; i < phnum; i++) {
                raf.seek(phoff + (long)i * phentsize);
                raf.readFully(entry);
                ByteBuffer p = ByteBuffer.wrap(entry).order(ByteOrder.LITTLE_ENDIAN);
                long type = u32(p, 0);
                long flags, offset, vaddr, size;
                if (info.bits == 64) {
                    flags = u32(p, 4); offset = p.getLong(8); vaddr = p.getLong(16); size = p.getLong(32);
                } else {
                    offset = u32(p, 4); vaddr = u32(p, 8); size = u32(p, 16); flags = u32(p, 24);
                }
                if (type == 1 && (flags & 1) != 0 && size > 0 &&
                        offset >= 0 && size <= raf.length() && offset + size <= raf.length()) {
                    info.executableOffset = offset;
                    info.executableSize = size;
                    info.virtualAddress = vaddr;
                    break;
                }
            }
            if (info.executableSize <= 0) throw new IllegalArgumentException("Nenhum segmento PT_LOAD executável encontrado.");
            return info;
        }
    }

    private void openElf(File file) throws Exception {
        ElfInfo info = readElf(file);
        if (info.architecture == 0) throw new IllegalArgumentException("Arquitetura ainda não suportada pelo Capstone.");
        long amount = Math.min(info.executableSize, 65536L);
        String disassembly = NativeBridge.nativeDisassembleElf(file.getAbsolutePath(),
                info.executableOffset, (int)amount, info.architecture);
        String header = "ELF " + info.bits + "-bit · " + info.architectureName +
                "\nMachine: " + info.machine +
                "\nExecutable PT_LOAD: file offset 0x" + Long.toHexString(info.executableOffset) +
                "\nVirtual address: 0x" + Long.toHexString(info.virtualAddress) +
                "\nSegment size: " + info.executableSize + " bytes" +
                "\nShowing first " + amount + " bytes (maximum 64 KiB)." +
                "\n\n" + disassembly;
        output.setText(header);
        content.addView(output, new LinearLayout.LayoutParams(-1, -2));

        TextView note = label("Patch binário controlado · substitui bytes no segmento executável; não recompila C/C++ nem reconstrói o ELF.", 11, dim, false);
        note.setPadding(0, dp(12), 0, dp(5));
        content.addView(note);
        patchOffset = field("Offset do arquivo (ex.: 0x" + Long.toHexString(info.executableOffset) + ")");
        patchOffset.setText("0x" + Long.toHexString(info.executableOffset));
        content.addView(patchOffset);
        patchBytes = field("Bytes hexadecimais (ex.: 1F 20 03 D5)");
        content.addView(patchBytes);
        content.addView(button("Criar cópia .so com patch", true, () -> patchElf(info)));
    }

    private EditText field(String hint) {
        EditText e = new EditText(this);
        e.setSingleLine(true);
        e.setTextSize(13);
        e.setTextColor(fg);
        e.setHintTextColor(dim);
        e.setHint(hint);
        e.setPadding(dp(10), dp(5), dp(10), dp(5));
        e.setBackgroundColor(bg);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(-1, dp(46));
        lp.topMargin = dp(5);
        lp.bottomMargin = dp(5);
        e.setLayoutParams(lp);
        return e;
    }

    private void patchElf(ElfInfo info) {
        try {
            String raw = patchOffset.getText().toString().trim().toLowerCase(Locale.ROOT);
            long offset = raw.startsWith("0x") ? Long.parseLong(raw.substring(2), 16) : Long.parseLong(raw);
            String hex = patchBytes.getText().toString().replaceAll("[^0-9A-Fa-f]", "");
            if (hex.isEmpty() || (hex.length() & 1) != 0) throw new IllegalArgumentException("Bytes hex inválidos.");
            if (hex.length() / 2 > 4096) throw new IllegalArgumentException("Limite de patch: 4096 bytes.");
            byte[] replacement = new byte[hex.length() / 2];
            for (int i = 0; i < replacement.length; i++)
                replacement[i] = (byte)Integer.parseInt(hex.substring(i * 2, i * 2 + 2), 16);
            if (offset < info.executableOffset ||
                    offset + replacement.length > info.executableOffset + info.executableSize)
                throw new IllegalArgumentException("O patch precisa ficar dentro do segmento executável identificado.");
            File out = new File(getCacheDir(), opened.getName() + ".patched.so");
            try (FileInputStream in = new FileInputStream(opened);
                 FileOutputStream stream = new FileOutputStream(out, false)) {
                byte[] buffer = new byte[65536]; int n;
                while ((n = in.read(buffer)) != -1) stream.write(buffer, 0, n);
            }
            try (RandomAccessFile raf = new RandomAccessFile(out, "rw")) {
                raf.seek(offset);
                raf.write(replacement);
                raf.getFD().sync();
            }
            requestSave(out, out.getName());
        } catch (Throwable error) {
            status.setText("Patch recusado: " + error.getMessage());
        }
    }

    private File wrapDex(File dex) throws Exception {
        File zip = new File(getCacheDir(), "binary-lab-dex-container.apk");
        try (ZipOutputStream out = new ZipOutputStream(new FileOutputStream(zip));
             FileInputStream in = new FileInputStream(dex)) {
            out.putNextEntry(new ZipEntry("classes.dex"));
            byte[] buffer = new byte[65536]; int count;
            while ((count = in.read(buffer)) != -1) out.write(buffer, 0, count);
            out.closeEntry();
        }
        return zip;
    }

    private void openDex(File dex) throws Exception {
        workspace = new File(getFilesDir(), "binary-lab-smali");
        deleteTree(workspace);
        if (!workspace.mkdirs()) throw new IllegalStateException("Não foi possível criar a pasta Smali.");
        File container = wrapDex(dex);
        status.setText("Baksmali descompilando DEX…");
        new SmaliDecoder(container, true).decode("classes.dex", workspace);
        List<File> files = new ArrayList<>();
        try (java.util.stream.Stream<java.nio.file.Path> stream = Files.walk(workspace.toPath())) {
            stream.filter(p -> p.toString().endsWith(".smali")).forEach(p -> files.add(p.toFile()));
        }
        files.sort(Comparator.comparing(File::getAbsolutePath));
        if (files.isEmpty()) throw new IllegalStateException("Nenhuma classe Smali foi extraída.");
        status.setText("DEX · " + files.size() + " classes Smali · edite e monte novamente");
        output.setText("DEX analisado: " + dex.getName() + "\nClasses extraídas: " + files.size() +
                "\nAs alterações no editor são salvas na pasta de trabalho antes da montagem.");
        content.addView(output);
        classList = new LinearLayout(this);
        classList.setOrientation(LinearLayout.VERTICAL);
        content.addView(label("CLASSES (toque para editar)", 10, dim, true));
        for (int i = 0; i < Math.min(files.size(), 300); i++) {
            File f = files.get(i);
            Button row = button(f.getName(), false, () -> loadSmali(f));
            classList.addView(row);
        }
        content.addView(classList);
        editor = new EditText(this);
        editor.setTextSize(11);
        editor.setTypeface(Typeface.MONOSPACE);
        editor.setTextColor(Color.rgb(34, 39, 44));
        editor.setGravity(Gravity.TOP | Gravity.START);
        editor.setMinHeight(dp(360));
        editor.setPadding(dp(10), dp(10), dp(10), dp(10));
        editor.setBackgroundColor(Color.rgb(248, 248, 241));
        content.addView(editor, new LinearLayout.LayoutParams(-1, -2));
        content.addView(button("Montar DEX editado", true, this::assembleDex));
        loadSmali(files.get(0));
    }

    private void loadSmali(File file) {
        try {
            if (selectedSmali != null && editor != null && selectedSmali.isFile()) {
                Files.write(selectedSmali.toPath(), editor.getText().toString().getBytes(java.nio.charset.StandardCharsets.UTF_8));
            }
            selectedSmali = file;
            editor.setText(new String(Files.readAllBytes(file.toPath()), java.nio.charset.StandardCharsets.UTF_8));
            status.setText("Editando: " + workspace.toPath().relativize(file.toPath()));
        } catch (Throwable error) {
            status.setText("Erro ao abrir classe: " + error.getMessage());
        }
    }

    private void assembleDex() {
        try {
            if (selectedSmali != null) {
                Files.write(selectedSmali.toPath(), editor.getText().toString().getBytes(java.nio.charset.StandardCharsets.UTF_8));
            }
            File out = new File(getCacheDir(), opened.getName().replaceAll("(?i)\\.dex$", "") + "-rebuilt.dex");
            SmaliOptions options = new SmaliOptions();
            options.apiLevel = 35;
            options.jobs = Math.max(1, Math.min(2, Runtime.getRuntime().availableProcessors()));
            options.outputDexFile = out.getAbsolutePath();
            status.setText("Smali montando novo DEX…");
            boolean ok = Smali.assemble(options, workspace.getAbsolutePath());
            if (!ok || !out.isFile() || out.length() == 0)
                throw new IllegalStateException("O assembler Smali não conseguiu gerar o DEX.");
            requestSave(out, out.getName());
        } catch (Throwable error) {
            status.setText("Falha ao recompilar DEX: " + error.getMessage());
        }
    }

    private void openArsc(File arsc) throws Exception {
        status.setText("Apktool decodificando resources.arsc…");
        File zip = new File(getCacheDir(), "binary-lab-arsc-container.apk");
        try (ZipOutputStream out = new ZipOutputStream(new FileOutputStream(zip));
             FileInputStream in = new FileInputStream(arsc)) {
            out.putNextEntry(new ZipEntry("resources.arsc"));
            byte[] buffer = new byte[65536]; int count;
            while ((count = in.read(buffer)) != -1) out.write(buffer, 0, count);
            out.closeEntry();
        }
        File decoded = new File(getFilesDir(), "binary-lab-arsc");
        deleteTree(decoded);
        if (!decoded.mkdirs()) throw new IllegalStateException("Não foi possível criar a pasta XML.");
        Config config = new Config("3.0.3");
        config.setJobs(1);
        config.setForced(true); // ApkDecoder clears/recreates its output directory.
        config.setKeepBrokenResources(true);
        config.setIgnoreRawValues(true);
        // Apktool 3.0.3 exposes File-based ApkDecoder APIs. Decode the tiny
        // container APK so the binary resources.arsc is processed with its
        // expected APK context; no synthetic ApkFile/ResDecoder API is needed.
        new ApkDecoder(zip, config).decode(decoded);
        List<File> xml = new ArrayList<>();
        try (java.util.stream.Stream<java.nio.file.Path> stream = Files.walk(decoded.toPath())) {
            stream.filter(p -> p.toString().endsWith(".xml")).forEach(p -> xml.add(p.toFile()));
        }
        xml.sort(Comparator.comparing(File::getAbsolutePath));
        output.setText("resources.arsc analisado por Apktool 3.0.3.\n" +
                "Arquivos XML gerados: " + xml.size() +
                "\nToque num XML para abrir; edite e exporte o documento.");
        content.addView(output);
        for (File f : xml) {
            content.addView(button(decoded.toPath().relativize(f.toPath()).toString(),
                    false, () -> loadArscXml(f)));
        }
        if (xml.isEmpty()) throw new IllegalStateException("A tabela não gerou XML; pode depender de framework ou formato não suportado.");
        editor = new EditText(this);
        editor.setTextSize(11);
        editor.setTypeface(Typeface.MONOSPACE);
        editor.setTextColor(Color.rgb(34, 39, 44));
        editor.setGravity(Gravity.TOP | Gravity.START);
        editor.setMinHeight(dp(320));
        editor.setPadding(dp(10), dp(10), dp(10), dp(10));
        editor.setBackgroundColor(Color.rgb(248, 248, 241));
        content.addView(editor, new LinearLayout.LayoutParams(-1, -2));
        content.addView(button("Exportar XML editado", true, this::exportArscXml));
        loadArscXml(xml.get(0));
        status.setText("ARSC decodificado · edição XML disponível; rebuild da tabela requer contexto APK/AAPT2.");
    }

    private void loadArscXml(File file) {
        try {
            selectedSmali = file;
            editor.setText(new String(Files.readAllBytes(file.toPath()), java.nio.charset.StandardCharsets.UTF_8));
            status.setText("XML de recursos: " + file.getName());
        } catch (Throwable error) {
            status.setText("Erro ao abrir XML: " + error.getMessage());
        }
    }

    private void exportArscXml() {
        try {
            if (selectedSmali == null || editor == null) return;
            File out = new File(getCacheDir(), selectedSmali.getName());
            Files.write(out.toPath(), editor.getText().toString().getBytes(java.nio.charset.StandardCharsets.UTF_8));
            requestSave(out, out.getName());
        } catch (Throwable error) {
            status.setText("Falha ao exportar XML: " + error.getMessage());
        }
    }

    private void requestSave(File file, String name) {
        pendingSave = file;
        pendingSaveName = name;
        Intent create = new Intent(Intent.ACTION_CREATE_DOCUMENT);
        create.addCategory(Intent.CATEGORY_OPENABLE);
        create.setType("application/octet-stream");
        create.putExtra(Intent.EXTRA_TITLE, name);
        try {
            startActivityForResult(create, SAVE_FILE);
        } catch (Throwable error) {
            status.setText("Não foi possível abrir o seletor para salvar: " + error.getMessage());
            pendingSave = null;
            pendingSaveName = "";
        }
    }

    private void deleteTree(File file) throws Exception {
        if (!file.exists()) return;
        try (java.util.stream.Stream<java.nio.file.Path> stream = Files.walk(file.toPath())) {
            stream.sorted(Comparator.reverseOrder()).forEach(p -> {
                try { Files.deleteIfExists(p); } catch (Exception ignored) { }
            });
        }
    }
}
'''
(java_dir / "BinaryWorkbenchActivity.java").write_text(activity, encoding="utf-8")

print("Binary Lab source generated: ELF/Capstone analysis + byte patching, DEX/Smali round-trip, and ARSC XML decode/export.")
