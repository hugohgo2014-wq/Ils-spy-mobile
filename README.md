# ILSpy Mobile

Android app built around the real **ICSharpCode.Decompiler** engine from ILSpy.

- **Interface:** Java + Android Material Components
- **Native bridge:** C++20 + JNI
- **Decompiler:** ICSharpCode.Decompiler 11.2.0.10050, compiled to a NativeAOT shared library
- **Languages:** Português (Brasil) and English
- **Icon:** original SVG and Android VectorDrawable XML; no PNG launcher artwork

## Source

The complete Android/Gradle/C++/C# project is in [ILSpyMobile-source.zip](./ILSpyMobile-source.zip). The GitHub Actions workflow automatically extracts this source bundle, builds the native engine, runs Android lint/unit-test tasks and assembles the APKs.

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

See the project README and `THIRD_PARTY_NOTICES.md` inside the source bundle for detailed setup, architecture and licensing.
