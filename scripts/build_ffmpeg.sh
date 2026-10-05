#!/usr/bin/env bash
# Build the minimal FFmpeg that Baleen ships (spec §6.7, §15, R-08): FFmpeg + libx264 only, with
# FFmpeg's native decoders, encoders and (de)muxers, licensed GPL-2.0-or-later. Every input is pinned
# in scripts/runtimes.json (components.ffmpeg.build.sources) and checked by SHA-256, so the shipped
# source archives are exactly the sources the binaries were built from.
#
#   scripts/build_ffmpeg.sh --target win-x64|mac-arm64|mac-x64|linux-x64 --out DIR [--cache DIR] [--jobs N]
#   scripts/build_ffmpeg.sh --sources-only --target T --out DIR    # fetch and verify the sources only
#
# win-x64 cross-compiles on Linux with mingw-w64 (static; no MSYS2). macOS and Linux build natively.
# Needs: bash, curl, git, make, pkg-config, python3, and nasm for x86-64 targets.
#
# Output (DIR): ffmpeg[.exe], ffprobe[.exe], config.txt (configure lines and build configuration),
# BUILDINFO.json (pins, SHA-256 of binaries and sources), sources/ (the exact source archives).
set -euo pipefail

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-2}"; }

TARGET="" OUT="" CACHE="" JOBS="" SOURCES_ONLY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --target) TARGET="$2"; shift 2 ;;
        --out) OUT="$2"; shift 2 ;;
        --cache) CACHE="$2"; shift 2 ;;
        --jobs) JOBS="$2"; shift 2 ;;
        --sources-only) SOURCES_ONLY=1; shift ;;
        -h|--help) usage 0 ;;
        *) echo "unknown argument: $1" >&2; usage ;;
    esac
done
case "$TARGET" in win-x64|mac-arm64|mac-x64|linux-x64) ;; *) echo "--target is required" >&2; usage ;; esac
[ -n "$OUT" ] || { echo "--out is required" >&2; usage; }

REPO=$(cd "$(dirname "$0")/.." && pwd)
PINS="$REPO/scripts/runtimes.json"
PY=${PYTHON:-python3}
CACHE=${CACHE:-$REPO/.scratch/ffmpeg-cache}
JOBS=${JOBS:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)}
mkdir -p "$OUT/sources" "$CACHE"
OUT=$(cd "$OUT" && pwd)
CACHE=$(cd "$CACHE" && pwd)
WORK="$OUT/.work"
PREFIX="$WORK/prefix"
rm -rf "$WORK"
mkdir -p "$WORK" "$PREFIX"

log() { printf '\n== %s\n' "$*"; }
die() { printf 'build_ffmpeg: %s\n' "$*" >&2; exit 1; }

# pin components.ffmpeg.build.sources.<name>.<key>  (prints '' when absent; lists are space-separated)
pin() {
    "$PY" - "$PINS" "$1" "$2" <<'EOF'
import json, sys
src = json.load(open(sys.argv[1], encoding="utf-8"))["components"]["ffmpeg"]["build"]["sources"].get(sys.argv[2], {})
val = src.get(sys.argv[3], "")
print(" ".join(val) if isinstance(val, list) else val)
EOF
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

verify() {  # file expected-sha256
    local got
    got=$(sha256_of "$1")
    [ "$got" = "$2" ] || die "SHA-256 mismatch for $(basename "$1"): got $got, expected $2"
}

for_target() {  # name -> 0 if the source is used for this target
    local only
    only=$(pin "$1" platforms)
    [ -z "$only" ] || [[ " $only " == *" $TARGET "* ]]
}

fetch_url() {  # name: download (with mirrors) into the cache, verify, copy to OUT/sources
    local name=$1 file sha url
    file=$(pin "$name" file); sha=$(pin "$name" sha256)
    if [ ! -f "$CACHE/$file" ] || [ "$(sha256_of "$CACHE/$file")" != "$sha" ]; then
        rm -f "$CACHE/$file"
        for url in $(pin "$name" url) $(pin "$name" mirrors); do
            echo "fetch $url"
            if curl -fsSL --retry 3 -o "$CACHE/$file.part" "$url"; then mv "$CACHE/$file.part" "$CACHE/$file"; break; fi
        done
        [ -f "$CACHE/$file" ] || die "could not download $file"
    fi
    verify "$CACHE/$file" "$sha"
    cp "$CACHE/$file" "$OUT/sources/$file"
}

fetch_x264() {
    # Clone with full history (blob-less) so x264's version.sh can name the revision, archive the pinned
    # commit with fixed line endings, and check the archive against the pinned SHA-256. The build then
    # uses the checkout of that same commit.
    local commit file sha url
    commit=$(pin x264 commit); file=$(pin x264 file); sha=$(pin x264 sha256)
    for url in $(pin x264 git); do
        echo "clone $url"
        rm -rf "$WORK/x264"
        if git -c core.autocrlf=false clone --quiet --filter=blob:none --no-checkout \
               -c core.autocrlf=false -c core.eol=lf "$url" "$WORK/x264"; then
            break
        fi
    done
    [ -d "$WORK/x264/.git" ] || die "could not clone x264"
    git -C "$WORK/x264" checkout --quiet --detach "$commit"
    git -C "$WORK/x264" archive --format=tar --prefix="x264-$commit/" "$commit" > "$OUT/sources/$file"
    echo "x264 archive sha256 $(sha256_of "$OUT/sources/$file")"
    verify "$OUT/sources/$file" "$sha"
}

log "sources for $TARGET"
fetch_url ffmpeg
fetch_x264
if for_target zlib; then fetch_url zlib; fi
if [ "$SOURCES_ONLY" = 1 ]; then
    rm -rf "$WORK"
    ls -l "$OUT/sources"
    exit 0
fi

# --------------------------------------------------------------------------- toolchain
EXE="" CROSS="" HOST_FLAGS=() FF_TARGET=() X264_TARGET=()
case "$TARGET" in
    win-x64)
        CROSS=x86_64-w64-mingw32-
        EXE=.exe
        X264_TARGET=(--host=x86_64-w64-mingw32 --cross-prefix="$CROSS")
        FF_TARGET=(--arch=x86_64 --target-os=mingw32 --cross-prefix="$CROSS" --enable-cross-compile
                   --pkg-config=pkg-config --enable-w32threads --extra-ldflags=-static)
        ;;
    mac-arm64|mac-x64)
        arch=arm64; [ "$TARGET" = mac-x64 ] && arch=x86_64
        export MACOSX_DEPLOYMENT_TARGET=${MACOSX_DEPLOYMENT_TARGET:-14.0}
        HOST_FLAGS=(-arch "$arch" -mmacosx-version-min="$MACOSX_DEPLOYMENT_TARGET")
        X264_TARGET=(--enable-pic --extra-cflags="${HOST_FLAGS[*]}" --extra-ldflags="${HOST_FLAGS[*]}")
        [ "$TARGET" = mac-x64 ] && X264_TARGET+=(--host=x86_64-apple-darwin)
        # VideoToolbox, AudioToolbox and the other Apple frameworks stay off (--disable-autodetect);
        # zlib is the macOS system library.
        FF_TARGET=(--arch="$arch" --enable-pthreads --extra-cflags="${HOST_FLAGS[*]}"
                   --extra-ldflags="${HOST_FLAGS[*]}")
        ;;
    linux-x64)
        X264_TARGET=(--enable-pic)
        FF_TARGET=(--enable-pthreads)
        ;;
esac
CC="${CROSS}gcc"; case "$TARGET" in mac-*) CC=clang ;; esac
command -v "$CC" >/dev/null || die "compiler $CC not found"

# --------------------------------------------------------------------------- zlib (win-x64 only)
if for_target zlib; then
    log "zlib $(pin zlib version)"
    tar -xf "$OUT/sources/$(pin zlib file)" -C "$WORK"
    ( cd "$WORK/zlib-$(pin zlib version)"
      make -f win32/Makefile.gcc PREFIX="$CROSS" -j"$JOBS" libz.a
      mkdir -p "$PREFIX/include" "$PREFIX/lib"
      cp zlib.h zconf.h "$PREFIX/include/"
      cp libz.a "$PREFIX/lib/" )
fi

# --------------------------------------------------------------------------- x264
log "x264 $(pin x264 commit)"
( cd "$WORK/x264"
  ./configure --prefix="$PREFIX" --enable-static --disable-cli --disable-opencl "${X264_TARGET[@]}"
  make -j"$JOBS"
  make install-lib-static )
X264_POINTVER=$(sed -n 's/^#define X264_POINTVER "\(.*\)"/\1/p' "$PREFIX/include/x264_config.h")

# --------------------------------------------------------------------------- FFmpeg
FF_VERSION=$(pin ffmpeg version)
log "FFmpeg $FF_VERSION"
tar -xf "$OUT/sources/$(pin ffmpeg file)" -C "$WORK"
FF_CONFIGURE=(
    --prefix="$PREFIX" --pkg-config-flags=--static --extra-version=baleen
    --enable-gpl --enable-libx264 --enable-static --disable-shared
    --disable-autodetect --enable-zlib
    --disable-network --disable-doc --disable-ffplay --disable-debug
    # libavdevice keeps only the lavfi input, for test patterns in fixtures and checks; no capture devices.
    --disable-indevs --enable-indev=lavfi --disable-outdevs
    --extra-cflags="-I$PREFIX/include" --extra-ldflags="-L$PREFIX/lib"
    "${FF_TARGET[@]}"
)
( cd "$WORK/ffmpeg-$FF_VERSION"
  PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig" PKG_CONFIG_LIBDIR="$PREFIX/lib/pkgconfig" \
      ./configure "${FF_CONFIGURE[@]}" || { tail -50 ffbuild/config.log; exit 1; }
  make -j"$JOBS"
  make install )
FF_CONFIGURATION=$(sed -n 's/^#define FFMPEG_CONFIGURATION "\(.*\)"/\1/p' "$WORK/ffmpeg-$FF_VERSION/config.h")
grep -q -- '--enable-version3' <<<"$FF_CONFIGURATION" && die "configuration enables version3"
grep -q -- '--enable-nonfree' <<<"$FF_CONFIGURATION" && die "configuration enables nonfree"
grep -q '^#define CONFIG_GPLV3 0' "$WORK/ffmpeg-$FF_VERSION/config.h" || die "CONFIG_GPLV3 is not 0"
grep -q '^#define CONFIG_NONFREE 0' "$WORK/ffmpeg-$FF_VERSION/config.h" || die "CONFIG_NONFREE is not 0"

cp "$PREFIX/bin/ffmpeg$EXE" "$PREFIX/bin/ffprobe$EXE" "$OUT/"

# The programs must depend on operating-system libraries only (no MinGW runtime or zlib DLLs, no
# Homebrew dylibs), so the shipped files are the whole binary.
log "dynamic dependencies"
for prog in ffmpeg ffprobe; do
    case "$TARGET" in
        win-x64)
            deps=$("${CROSS}objdump" -p "$OUT/$prog$EXE" | sed -n 's/^\s*DLL Name: //p' | sort -u)
            echo "$prog$EXE: $(echo $deps)"
            if grep -qiE 'winpthread|libgcc|libstdc|zlib|libz|x264' <<<"$deps"; then die "$prog$EXE needs a non-system DLL"; fi ;;
        mac-*)
            deps=$(otool -L "$OUT/$prog" | tail -n +2 | awk '{print $1}')
            echo "$prog: $(echo $deps)"
            if grep -vqE '^(/usr/lib/|/System/)' <<<"$deps"; then die "$prog links a non-system library"; fi ;;
        linux-x64)
            ldd "$OUT/$prog" || true ;;
    esac
done

# --------------------------------------------------------------------------- records
{
    echo "Baleen FFmpeg build for $TARGET"
    echo
    echo "FFmpeg $FF_VERSION, x264 $X264_POINTVER (commit $(pin x264 commit))"
    for_target zlib && echo "zlib $(pin zlib version) (static)"
    echo
    echo "x264 configure:"
    echo "  ./configure --prefix=<prefix> --enable-static --disable-cli --disable-opencl ${X264_TARGET[*]}"
    echo
    echo "FFmpeg configure (as run):"
    printf '  ./configure'; printf ' %q' "${FF_CONFIGURE[@]}"; echo
    echo
    echo "FFmpeg configuration (FFMPEG_CONFIGURATION, as ffmpeg -buildconf prints it):"
    echo "  $FF_CONFIGURATION"
    echo
    echo "Toolchain: $("$CC" --version 2>&1 | head -1)"
    echo "Sources (also in sources/):"
    for f in "$OUT"/sources/*; do echo "  $(basename "$f")  sha256 $(sha256_of "$f")"; done
    if [ -z "$EXE" ]; then
        echo
        echo "ffmpeg -L:"
        "$OUT/ffmpeg" -hide_banner -L | sed 's/^/  /'
        echo
        echo "ffmpeg -buildconf:"
        "$OUT/ffmpeg" -hide_banner -buildconf | sed 's/^/  /'
    fi
} > "$OUT/config.txt"

"$PY" - "$OUT" "$TARGET" "$FF_VERSION" "$(pin x264 commit)" "$X264_POINTVER" "$FF_CONFIGURATION" \
      "$("$CC" --version 2>&1 | head -1)" "$EXE" <<'EOF'
import datetime, hashlib, json, os, sys
from pathlib import Path
out, target, ffver, x264_commit, x264_version, configuration, toolchain, exe = sys.argv[1:9]
out = Path(out)
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
info = {
    "schema_version": 1,
    "target": target,
    "license": "GPL-2.0-or-later",
    "ffmpeg_version": ffver,
    "x264": {"commit": x264_commit, "version": x264_version.strip()},
    "configuration": configuration,
    "toolchain": toolchain,
    "binaries": {n + exe: sha(out / (n + exe)) for n in ("ffmpeg", "ffprobe")},
    "sources": {p.name: sha(p) for p in sorted((out / "sources").iterdir())},
    "built_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "built_from": {"repository": os.environ.get("GITHUB_REPOSITORY", ""), "commit": os.environ.get("GITHUB_SHA", ""),
                   "run": os.environ.get("GITHUB_RUN_ID", "")},
}
(out / "BUILDINFO.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
print(json.dumps(info, indent=2))
EOF
rm -rf "$WORK"
ls -l "$OUT" "$OUT/sources"
