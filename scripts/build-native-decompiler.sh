#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "${1:-.}" && pwd)"
WORK="${RUNNER_TEMP:-/tmp}/ilspy-native-decompiler"
NDK="${ANDROID_NDK_HOME:-${ANDROID_NDK_ROOT:-}}"
if [[ -z "$NDK" || ! -d "$NDK" ]]; then
  echo "Android NDK was not found." >&2
  exit 1
fi

command -v cmake >/dev/null
command -v git >/dev/null
command -v python3 >/dev/null
mkdir -p "$WORK"
RIZIN_SRC="$WORK/rizin"
GHIDRA_SRC="$WORK/rz-ghidra"
if [[ ! -d "$RIZIN_SRC/.git" ]]; then
  git clone --depth 1 --branch stable https://github.com/rizinorg/rizin.git "$RIZIN_SRC"
fi
if [[ ! -d "$GHIDRA_SRC/.git" ]]; then
  git clone --depth 1 --branch stable --recurse-submodules https://github.com/rizinorg/rz-ghidra.git "$GHIDRA_SRC"
fi
git -C "$GHIDRA_SRC" submodule update --init --recursive

# Build Ghidra's SLEIGH processor descriptions once on the host. The Android
# plugin consumes these generated .sla files; it never executes host binaries.
HOST_PREFIX="$WORK/host-sleigh-install"
HOST_SOURCE="$WORK/host-sleigh-project"
HOST_BUILD="$WORK/host-sleigh-build"
mkdir -p "$HOST_SOURCE"
cat > "$HOST_SOURCE/CMakeLists.txt" <<EOF
cmake_minimum_required(VERSION 3.22)
project(ILSpyHostSleigh C CXX)
find_package(ZLIB REQUIRED)
set(BUILD_SLASPECS ON CACHE BOOL "" FORCE)
set(BUILD_DECOMPILE_EXECUTABLE OFF CACHE BOOL "" FORCE)
set(BUILD_DECOMPILE_CLI_EXECUTABLE OFF CACHE BOOL "" FORCE)
set(BUILD_SLEIGH_PLUGIN OFF CACHE BOOL "" FORCE)
set(USE_SYSTEM_ZLIB ON CACHE BOOL "" FORCE)
set(RIZIN_INSTALL_PLUGDIR "lib/rizin/plugins" CACHE STRING "" FORCE)
add_subdirectory("$GHIDRA_SRC/ghidra" ghidra)
EOF
cmake -S "$HOST_SOURCE" -B "$HOST_BUILD" -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$HOST_PREFIX"
cmake --build "$HOST_BUILD" --target sla --parallel 2
cmake --install "$HOST_BUILD"

build_one_abi() {
  local abi="$1"
  local triple="$2"
  local cpu_family="$3"
  local cpu="$4"
  local prefix="$WORK/prefix-$abi"
  local build="$WORK/rizin-build-$abi"
  local cross="$WORK/meson-$abi.ini"
  local plugin_build="$WORK/ghidra-build-$abi"
  local toolbin="$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin"

  mkdir -p "$prefix"
  cat > "$cross" <<EOF
[binaries]
c = '$toolbin/${triple}26-clang'
cpp = '$toolbin/${triple}26-clang++'
ar = '$toolbin/llvm-ar'
as = '$toolbin/$triple-as'
ranlib = '$toolbin/$triple-ranlib'
strip = '$toolbin/$triple-strip'
pkgconfig = 'false'

[built-in options]
c_link_args = ['-lm']
cpp_link_args = ['-lm']

[properties]
sys_root = '$NDK/sysroot'
needs_exe_wrapper = true

[host_machine]
system = 'android'
cpu_family = '$cpu_family'
cpu = '$cpu'
endian = 'little'
EOF
  echo "==> Building Rizin for $abi"
  meson setup "$build" "$RIZIN_SRC" \
    --cross-file "$cross" \
    --prefix="$prefix" \
    --buildtype=release \
    --default-library=shared \
    -Dportable=true \
    -Dlocal=disabled \
    -Ddebugger=false \
    -Denable_tests=false \
    -Denable_rz_test=false \
    -Denable_examples=false \
    -Dinstall_sigdb=false \
    -Duse_lzma=false
  meson compile -C "$build" -j 2
  meson install -C "$build"

  echo "==> Building Ghidra decompiler plugin for $abi"
  cmake -S "$GHIDRA_SRC" -B "$plugin_build" \
    -DCMAKE_TOOLCHAIN_FILE="$NDK/build/cmake/android.toolchain.cmake" \
    -DANDROID_ABI="$abi" \
    -DANDROID_PLATFORM=android-26 \
    -DCMAKE_SHARED_LINKER_FLAGS=-lm \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_PREFIX_PATH="$prefix" \
    -DRizin_DIR="$prefix/lib/cmake/Rizin" \
    -DCMAKE_FIND_ROOT_PATH_MODE_PACKAGE=BOTH \
    -DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=BOTH \
    -DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=BOTH \
    -DANDROID_STL=c++_shared \
    -DCMAKE_INSTALL_PREFIX="$prefix" \
    -DRIZIN_INSTALL_PLUGDIR="$prefix/lib/rizin/plugins" \
    -DBUILD_SLASPECS=OFF \
    -DBUILD_SLEIGH_PLUGIN=OFF \
    -DBUILD_DECOMPILE_EXECUTABLE=OFF \
    -DBUILD_DECOMPILE_CLI_EXECUTABLE=OFF \
    -DUSE_SYSTEM_ZLIB=ON
  cmake --build "$plugin_build" --target core_ghidra --parallel 2
  cmake --install "$plugin_build"

  local sleigh_target="$prefix/lib/rizin/plugins/rz_ghidra_sleigh"
  mkdir -p "$sleigh_target"
  cp -a "$HOST_PREFIX/lib/rizin/plugins/rz_ghidra_sleigh/." "$sleigh_target/"

  local asset="$ROOT/app/src/main/assets/native-decompiler/$abi"
  mkdir -p "$asset/bin" "$asset/lib" "$asset/share"
  if [[ ! -x "$prefix/bin/rizin" ]]; then
    echo "Rizin executable missing after install for $abi" >&2
    find "$prefix" -maxdepth 4 -type f | head -100 >&2
    exit 1
  fi
  cp "$prefix/bin/rizin" "$asset/bin/rizin"
  # The command-line executable and the Ghidra plugin use the NDK shared C++ runtime.
  local cxx_shared="$NDK/sysroot/usr/lib/$triple/libc++_shared.so"
  if [[ -f "$cxx_shared" ]]; then cp "$cxx_shared" "$asset/lib/libc++_shared.so"; fi
  # Dereference versioned .so symlinks: Android assets cannot recreate symlinks.
  find "$prefix/lib" -maxdepth 1 \( -type f -o -type l \) -print | while read -r library; do
    case "$library" in
      *.so|*.so.*) cp -L "$library" "$asset/lib/$(basename "$library")" ;;
    esac
  done
  if [[ -d "$prefix/lib/rizin" ]]; then
    mkdir -p "$asset/lib/rizin"
    cp -aL "$prefix/lib/rizin/." "$asset/lib/rizin/"
  fi
  if [[ -d "$prefix/share/rizin" ]]; then
    cp -aL "$prefix/share/rizin" "$asset/share/"
  fi
  chmod 755 "$asset/bin/rizin"

  # Android 10+ blocks execve() from writable app-private directories (W^X).
  # Package the PIE executable and shared libraries in jniLibs; PackageManager
  # extracts them into nativeLibraryDir under /data/app.
  local jni="$ROOT/app/src/main/jniLibs/$abi"
  mkdir -p "$jni"
  cp "$prefix/bin/rizin" "$jni/libilspy_rizin.so"
  find "$prefix/lib" -maxdepth 1 \( -type f -o -type l \) -print | while read -r library; do
    case "$library" in
      *.so|*.so.*) cp -L "$library" "$jni/$(basename "$library")" ;;
    esac
  done
  # Keep core_ghidra.so in the private plugin directory under assets.
  # RZ_LIB_PLUGINS points there explicitly; do not scan nativeLibraryDir.

  test -s "$asset/lib/rizin/plugins/core_ghidra.so"
  test -s "$asset/lib/rizin/plugins/rz_ghidra_sleigh/AARCH64.sla"
  echo "Bundled Rizin + Ghidra for $abi: $(du -sh "$asset" | cut -f1)"
}

build_one_abi arm64-v8a aarch64-linux-android arm aarch64
build_one_abi x86_64 x86_64-linux-android x86 x86_64

echo "Rizin/Ghidra native C decompiler bundled for ARM64 and x86_64."
