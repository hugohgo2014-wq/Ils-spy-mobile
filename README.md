# ILSpy Mobile

Android app built around the real **ICSharpCode.Decompiler** engine from ILSpy.

- **Interface:** Java + Android Material Components
- **Native bridge:** C++20 + JNI
- **Decompiler:** ICSharpCode.Decompiler 11.2.0.10050, compiled to a NativeAOT shared library
- **Languages:** Português (Brasil) and English
- **Icon:** original SVG and Android VectorDrawable XML; no PNG launcher artwork

## Source

The complete Android/Gradle/C++/C# project is encoded in [`source-bundle/`](./source-bundle/). GitHub Actions reconstructs the ZIP automatically for builds. The icon SVG and Android vector resources are included.

## Build

Open **Actions → Build ILSpy Mobile APK → Run workflow**, or push to `main`.

Artifacts:
- `app-debug.apk`: debug-signed APK for device testing.
- `app-release-unsigned.apk`: release build, which must be signed with your own key before distribution.

## Supported Android ABIs

| ABI | Status |
|---|---|
| `arm64-v8a` | Supported |
| `x86_64` | Supported |
| `armeabi-v7a` | Not supported by the .NET NativeAOT Bionic shared-library backend |
| `x86` | Not supported by the .NET NativeAOT Bionic shared-library backend |

The .NET Android Bionic NativeAOT shared-library path is experimental and currently documents only `linux-bionic-arm64` and `linux-bionic-x64`. JNI is implemented manually in the C++ bridge. The project deliberately does not package an ABI without a matching decompiler engine.

See `README.md` and `THIRD_PARTY_NOTICES.md` inside the source archive for setup, architecture and licensing.

## Native and Android binary workbench

The **Binary Lab** screen adds these formats:

- **ELF / `.so`:** reads ELF32/ELF64 headers and locates an executable load segment. Capstone 5.0.6 disassembles the first 64 KiB of that segment for ARM, AArch64, x86 and x86_64. A bounded byte-patch tool can write a *new patched copy* when the replacement bytes stay inside that executable segment.
- **Important native limitation:** Capstone is a disassembler, not a C/C++ decompiler or linker. The app does not reconstruct original C/C++ source and does not rebuild a complete shared library from pseudocode. The `.so` patch action edits machine-code bytes in a copy; it is not a full source recompilation.
- **DEX:** Apktool's Smali decoder extracts editable `.smali` classes. The integrated Smali assembler rebuilds a DEX from the edited workspace and exports it with Android's Save As picker.
- **`resources.arsc`:** Apktool decodes the binary resource table into XML files that can be edited and exported. A standalone `.arsc` recompiler is not provided: rebuilding the binary resource table needs the surrounding APK project, resource files/framework context and a compatible AAPT2 build pipeline.

The Binary Lab works on a copy in the app's private cache. It does not execute imported native libraries.

## Editing and recompiling

The Android workbench includes an editable C# source view and an in-app Roslyn compiler:
- Rebuild edited managed C# source as a new DLL or console EXE.
- Import additional managed DLL / WINMD references with the + Ref button when the source depends on them.
- Export the rebuilt file through Android's document picker (Save As).
- This is a source rebuild, not a byte-for-byte patch: original strong-name signatures, resources, PDB files, and exact assembly identity are not automatically retained.
- WINMD files can be inspected when they contain supported CLR metadata. The WINMDOBJ option creates the compiler's intermediate .winmdobj; producing a final Windows Runtime .winmd requires Microsoft's WinMDExp tooling on Windows and is not performed by this Android build.
- Native C/C++ DLLs and EXEs do not contain managed CIL/C# for ILSpy to reconstruct. The app accepts managed PE assemblies/modules such as DLL, EXE, WINMD, NETMODULE, and WINMDOBJ; it does not promise source recompilation for native binaries.
