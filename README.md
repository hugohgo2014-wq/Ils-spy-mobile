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

## Editing and recompiling

The Android workbench includes an editable C# source view and an in-app Roslyn compiler:
- Rebuild edited managed C# source as a new DLL or console EXE.
- Import additional managed DLL / WINMD references with the + Ref button when the source depends on them.
- Export the rebuilt file through Android's document picker (Save As).
- This is a source rebuild, not a byte-for-byte patch: original strong-name signatures, resources, PDB files, and exact assembly identity are not automatically retained.
- WINMD files can be inspected when they contain supported CLR metadata. The WINMDOBJ option creates the compiler's intermediate .winmdobj; producing a final Windows Runtime .winmd requires Microsoft's WinMDExp tooling on Windows and is not performed by this Android build.
- Native C/C++ DLLs and EXEs do not contain managed CIL/C# for ILSpy to reconstruct. The app accepts managed PE assemblies/modules such as DLL, EXE, WINMD, NETMODULE, and WINMDOBJ; it does not promise source recompilation for native binaries.
