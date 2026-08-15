#!/usr/bin/env bash
# Build and install a pinned, Vulkan-enabled llama.cpp in the current user's home.
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
readonly VERSIONS_FILE="$PROJECT_ROOT/config/versions.env"

DRY_RUN=0
SKIP_PACKAGES=0
FORCE_REBUILD=0
JOBS="${JOBS:-$(nproc)}"
SOURCE_ROOT="${LOCAL_AI_SOURCE_ROOT:-${XDG_CACHE_HOME:-$HOME/.cache}/local-ai/src}"
INSTALL_ROOT="${LOCAL_AI_INSTALL_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/local-ai/runtime}"
BIN_DIR="${LOCAL_AI_BIN_DIR:-$HOME/.local/bin}"
MODEL_DIR="${LOCAL_AI_MODEL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/local-ai/models}"
CONFIG_DIR="${LOCAL_AI_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/local-ai}"

usage() {
  cat <<'EOF'
Usage: scripts/setup.sh [options]

Build and install the pinned llama.cpp release with the Vulkan backend.
The installation is user-local; sudo is used only to install missing OS packages.

Options:
  --dry-run          Print commands without changing the system
  --skip-packages    Do not install or verify distribution packages
  --force-rebuild    Remove the pinned source/build tree before building
  --jobs N           Number of parallel build jobs (default: all CPUs)
  -h, --help         Show this help

Environment overrides:
  LOCAL_AI_SOURCE_ROOT   Source checkout parent (default: ~/.cache/local-ai/src)
  LOCAL_AI_INSTALL_ROOT  Versioned runtime parent (default: ~/.local/share/local-ai/runtime)
  LOCAL_AI_BIN_DIR       Executable symlink directory (default: ~/.local/bin)
  LOCAL_AI_MODEL_DIR     Model storage directory (default: ~/.local/share/local-ai/models)
  LOCAL_AI_CONFIG_DIR    Runtime configuration directory (default: ~/.config/local-ai)
  JOBS                   Parallel build jobs
EOF
}

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

run() {
  printf '+'
  printf ' %q' "$@"
  printf '\n'
  (( DRY_RUN )) || "$@"
}

while (($#)); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --skip-packages) SKIP_PACKAGES=1 ;;
    --force-rebuild) FORCE_REBUILD=1 ;;
    --jobs)
      shift
      (($#)) || die "--jobs requires a value"
      JOBS="$1"
      ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
  shift
done

[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || die "job count must be a positive integer"
[[ -r "$VERSIONS_FILE" ]] || die "missing $VERSIONS_FILE"
# shellcheck disable=SC1090
source "$VERSIONS_FILE"
: "${LLAMA_CPP_REPOSITORY:?missing LLAMA_CPP_REPOSITORY in config/versions.env}"
: "${LLAMA_CPP_REF:?missing LLAMA_CPP_REF in config/versions.env}"

install_packages() {
  (( SKIP_PACKAGES )) && { log "Skipping distribution package installation"; return; }

  local -a packages=()
  local -a command_names=(git cmake ninja c++ curl)
  local command_name

  if command -v pacman >/dev/null 2>&1; then
    local -A arch_packages=(
      [git]=git [cmake]=cmake [ninja]=ninja [c++]=gcc [curl]=curl
    )
    for command_name in "${command_names[@]}"; do
      command -v "$command_name" >/dev/null 2>&1 || packages+=("${arch_packages[$command_name]}")
    done
    # These packages provide the loader, Vulkan/SPIR-V headers, shader compiler,
    # and AMD Vulkan driver. llama.cpp finds spirv-headers through CMake config.
    for package in vulkan-headers spirv-headers vulkan-icd-loader shaderc vulkan-radeon; do
      pacman -Q "$package" >/dev/null 2>&1 || packages+=("$package")
    done
    if ((${#packages[@]})); then
      log "Installing missing Arch Linux build dependencies"
      require_sudo
      run sudo pacman -S --needed -- "${packages[@]}"
    fi
  elif command -v apt-get >/dev/null 2>&1; then
    local -A debian_packages=(
      [git]=git [cmake]=cmake [ninja]=ninja-build [c++]=g++ [curl]=curl
    )
    for command_name in "${command_names[@]}"; do
      command -v "$command_name" >/dev/null 2>&1 || packages+=("${debian_packages[$command_name]}")
    done
    for package in libvulkan-dev spirv-headers glslc mesa-vulkan-drivers; do
      dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'ok installed' || packages+=("$package")
    done
    if ((${#packages[@]})); then
      log "Installing missing Debian/Ubuntu build dependencies"
      require_sudo
      run sudo apt-get update
      run sudo apt-get install -y --no-install-recommends "${packages[@]}"
    fi
  else
    die "unsupported package manager; install git, cmake, ninja, a C++ compiler, curl, and Vulkan development packages, then use --skip-packages"
  fi
}

require_sudo() {
  if (( DRY_RUN )); then
    printf '+ sudo -v\n'
    return
  fi
  command -v sudo >/dev/null 2>&1 || die "sudo is required to install missing packages"
  if ! sudo -n true 2>/dev/null; then
    [[ -t 0 || -t 1 || -r /dev/tty ]] || die "sudo credentials are required but no terminal is available"
    log "Authenticating sudo for package installation"
    sudo -v
  fi
}

verify_commands() {
  local missing=0 command_name
  for command_name in git cmake ninja c++ curl; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
      printf 'error: required command not found: %s\n' "$command_name" >&2
      missing=1
    fi
  done
  (( missing == 0 )) || die "install missing dependencies or rerun without --skip-packages"
}

install_packages
(( DRY_RUN )) || verify_commands

readonly SOURCE_DIR="$SOURCE_ROOT/llama.cpp-$LLAMA_CPP_REF"
readonly BUILD_DIR="$SOURCE_DIR/build-vulkan"
readonly PREFIX="$INSTALL_ROOT/llama.cpp-$LLAMA_CPP_REF"

log "Preparing directories"
run mkdir -p "$SOURCE_ROOT" "$INSTALL_ROOT" "$BIN_DIR" "$MODEL_DIR" "$CONFIG_DIR"

if (( FORCE_REBUILD )) && [[ -e "$SOURCE_DIR" ]]; then
  log "Removing existing source tree"
  run rm -rf -- "$SOURCE_DIR"
fi

if [[ ! -d "$SOURCE_DIR/.git" ]]; then
  log "Cloning llama.cpp $LLAMA_CPP_REF"
  run git clone --filter=blob:none --branch "$LLAMA_CPP_REF" --depth 1 \
    "$LLAMA_CPP_REPOSITORY" "$SOURCE_DIR"
else
  log "Using existing source tree $SOURCE_DIR"
  if (( ! DRY_RUN )); then
    current_ref="$(git -C "$SOURCE_DIR" describe --tags --exact-match 2>/dev/null || true)"
    [[ "$current_ref" == "$LLAMA_CPP_REF" ]] || die "$SOURCE_DIR is at '${current_ref:-an untagged commit}', expected $LLAMA_CPP_REF; use --force-rebuild"
  fi
fi

log "Configuring Vulkan build"
run cmake -S "$SOURCE_DIR" -B "$BUILD_DIR" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="$PREFIX" \
  -DBUILD_SHARED_LIBS=OFF \
  -DGGML_VULKAN=ON \
  -DLLAMA_BUILD_TESTS=OFF \
  -DLLAMA_BUILD_EXAMPLES=OFF \
  -DLLAMA_BUILD_TOOLS=ON \
  -DLLAMA_BUILD_APP=OFF \
  -DLLAMA_BUILD_SERVER=ON

log "Building llama.cpp"
run cmake --build "$BUILD_DIR" --parallel "$JOBS" --target llama-cli llama-server llama-bench

log "Installing llama.cpp to $PREFIX"
for executable in llama-cli llama-server llama-bench; do
  built_executable="$BUILD_DIR/bin/$executable"
  installed_executable="$PREFIX/bin/$executable"
  if (( ! DRY_RUN )); then
    [[ -x "$built_executable" ]] || die "expected executable was not built: $built_executable"
  fi
  run install -Dm755 "$built_executable" "$installed_executable"
  run ln -sfn "$installed_executable" "$BIN_DIR/$executable"
done

log "Verifying installation"
if (( DRY_RUN )); then
  printf '+ %q --version\n' "$BIN_DIR/llama-server"
  printf '+ %q --list-devices\n' "$BIN_DIR/llama-cli"
else
  "$BIN_DIR/llama-server" --version
  device_output="$({ "$BIN_DIR/llama-cli" --list-devices; } 2>&1)"
  printf '%s\n' "$device_output"
  grep -Eqi 'Vulkan|Radeon|AMD' <<<"$device_output" || die "llama.cpp did not report a Vulkan/AMD device"
fi

cat <<EOF

Setup complete.

Runtime: $PREFIX
Models:  $MODEL_DIR
Config:  $CONFIG_DIR
Binaries: $BIN_DIR

Ensure $BIN_DIR is in PATH, then continue with model download and router setup.
EOF
