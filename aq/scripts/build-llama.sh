#!/bin/bash
# Builds llama.cpp as static libraries for linking into aq, so the binary doesn't depend
# on a Homebrew llama.cpp (whose library ABI changes between releases).
#   bash scripts/build-llama.sh            -> vendor/llama/{include,lib}
# Pinned to the release aq was tested with. Needs cmake and Xcode.
set -euo pipefail
TAG="${LLAMA_TAG:-b11429}"   # llama.cpp 0.6.0, commit d812350
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$HERE/vendor/llama.cpp-src"
OUT="$HERE/vendor/llama"

if [ ! -d "$SRC/.git" ]; then
    git clone --depth 1 --branch "$TAG" https://github.com/ggml-org/llama.cpp "$SRC"
fi
# Build through a space-free symlink: CMake splits compiler flags on spaces, and the
# -ffile-prefix-map below needs the source path as one argument.
TMP="${TMPDIR:-/tmp}"; LINK="${TMP%/}/aq-llama-src"
ln -sfn "$SRC" "$LINK"

cmake -S "$LINK" -B "$LINK/build-static" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_OSX_ARCHITECTURES=arm64 \
    -DCMAKE_OSX_DEPLOYMENT_TARGET=14.0 \
    -DBUILD_SHARED_LIBS=OFF \
    -DGGML_METAL=ON \
    -DGGML_METAL_EMBED_LIBRARY=ON \
    -DGGML_NATIVE=OFF \
    -DGGML_BLAS=ON -DGGML_BLAS_VENDOR=Apple \
    -DLLAMA_CURL=OFF \
    -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_TOOLS=OFF -DLLAMA_BUILD_SERVER=OFF \
    -DLLAMA_BUILD_COMMON=OFF \
    -DCMAKE_C_FLAGS="-ffile-prefix-map=$LINK=llama.cpp" \
    -DCMAKE_CXX_FLAGS="-ffile-prefix-map=$LINK=llama.cpp" \
    -DCMAKE_OBJC_FLAGS="-ffile-prefix-map=$LINK=llama.cpp"   # no build-machine paths in the binary
cmake --build "$LINK/build-static" --config Release -j --target llama

rm -rf "$OUT"
mkdir -p "$OUT/include" "$OUT/lib"
cp "$SRC/include/llama.h" "$OUT/include/"
cp "$SRC"/ggml/include/*.h "$OUT/include/"
find "$SRC/build-static" -name "*.a" -exec cp {} "$OUT/lib/" \;
echo "static libraries:"
ls -la "$OUT/lib"
