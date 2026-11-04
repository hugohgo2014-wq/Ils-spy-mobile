#!/usr/bin/env python3
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
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
print("Patched NativeBridge loading order and visible engine diagnostics.")
