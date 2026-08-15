#!/usr/bin/env bash
# Collect a portable, read-only host baseline. Only --output-dir is written.
set -u

usage() {
  printf 'Usage: %s --output-dir DIRECTORY\n' "$0"
}

output_dir=''
while (($#)); do
  case "$1" in
    --output-dir)
      (($# >= 2)) || { usage >&2; exit 2; }
      output_dir=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

[[ -n "$output_dir" ]] || { usage >&2; exit 2; }
mkdir -p -- "$output_dir" || exit 1

# Keep command failures from making a partial baseline look like a failure.
read_command() {
  local value
  if value=$("$@" 2>/dev/null); then
    printf '%s' "$value"
  else
    printf 'unavailable'
  fi
}

first_line() {
  printf '%s\n' "$1" | head -n 1
}

mem_value() {
  awk -v key="$1" '$1 == key {print $2 " " $3; found=1} END {if (!found) print "unavailable"}' /proc/meminfo 2>/dev/null
}

# Prefer the DRM device with the largest non-zero VRAM heap. This selects the
# target discrete adapter when an integrated adapter also exposes a small heap.
target_card=''
target_vram_file=''
best_vram_bytes=0
for card_device in /sys/class/drm/card*/device; do
  candidate="$card_device/mem_info_vram_total"
  candidate_bytes=0
  if [[ -r "$candidate" ]]; then
    candidate_bytes=$(awk '$1 > 0 {print $1; exit}' "$candidate" 2>/dev/null)
    [[ "$candidate_bytes" =~ ^[0-9]+$ ]] || candidate_bytes=0
  fi
  if (( candidate_bytes > best_vram_bytes )); then
    best_vram_bytes=$candidate_bytes
    target_card=${card_device%/device}
    target_vram_file=$candidate
  fi
done

gpu='unavailable'
gpu_pci='unavailable'
if [[ -n "$target_card" ]]; then
  uevent="$target_card/device/uevent"
  if command -v lspci >/dev/null 2>&1 && [[ -r "$uevent" ]]; then
    gpu_pci=$(awk -F= '$1 == "PCI_SLOT_NAME" {print $2; exit}' "$uevent")
    [[ -n "$gpu_pci" ]] || gpu_pci='unavailable'
    if [[ "$gpu_pci" != unavailable ]]; then
      gpu=$(lspci -s "$gpu_pci" 2>/dev/null || printf 'unavailable')
    fi
  fi
  [[ "$gpu" != unavailable ]] || gpu=$(basename "$target_card")
elif command -v lspci >/dev/null 2>&1; then
  gpu=$(lspci 2>/dev/null | awk 'tolower($0) ~ /vga compatible controller|3d controller|display controller/ {print; found=1; exit} END {if (!found) print "unavailable"}')
fi
[[ -n "$gpu" ]] || gpu='unavailable'

vram='unavailable'
idle_vram='unavailable'
if [[ -n "$target_vram_file" ]]; then
  vram=$(awk '{printf "%.2f GiB", $1 / 1024 / 1024 / 1024}' "$target_vram_file" 2>/dev/null)
  idle_file="${target_vram_file%_total}_used"
  if [[ -r "$idle_file" ]]; then
    idle_vram=$(awk '{printf "%.2f GiB", $1 / 1024 / 1024 / 1024}' "$idle_file" 2>/dev/null)
  fi
fi

# Only emit Vulkan device/driver lines when the selected PCI device can be
# matched to a Vulkan device. Never combine one adapter's PCI data with another
# adapter's Vulkan data.
vulkan='unavailable (target GPU association not verified)'
if [[ "$gpu_pci" != unavailable ]] && command -v vulkaninfo >/dev/null 2>&1; then
  # Match the selected PCI adapter by a model identifier present in both
  # lspci and vulkaninfo. Do not report another adapter's Vulkan driver.
  target_hint=$(printf '%s\n' "$gpu" | grep -Eio 'navi[[:space:]]*[0-9]+' | tr -d '[:space:]')
  if [[ -z "$target_hint" ]]; then
    target_hint=$(printf '%s\n' "$gpu" | grep -Eio 'radeon[[:space:]]+rx[[:space:]]*[0-9]+([[:space:]]*[a-z]+)?' | head -n 1)
  fi
  if [[ -n "$target_hint" ]]; then
    vulkan=$(vulkaninfo --summary 2>/dev/null | awk -v hint="$target_hint" '
      /deviceName/ {in_target = index(tolower($0), tolower(hint)) > 0}
      in_target && /deviceName|driverName|driverInfo/ {print}
    ')
    [[ -n "$vulkan" ]] || vulkan='unavailable (target GPU association not verified)'
  fi
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
report="$output_dir/baseline-$stamp.txt"
{
  printf 'Local AI hardware baseline\n'
  printf 'Report version: 1\n'
  printf 'Collected UTC: %s\n\n' "$stamp"
  printf 'GPU: %s\n' "$gpu"
  printf 'GPU PCI: %s\n' "$gpu_pci"
  printf 'VRAM (target GPU): %s\n' "$vram"
  printf 'Idle VRAM usage (target GPU): %s\n' "$idle_vram"
  printf 'Vulkan (target GPU): %s\n' "$vulkan"
  cpu=$(read_command lscpu | awk -F: '/Model name/ {gsub(/^[ \t]+/, "", $2); print $2; exit}')
  [[ -n "$cpu" ]] || cpu='unavailable'
  printf 'CPU: %s\n' "$(first_line "$cpu")"
  printf 'RAM: total=%s; available=%s\n' "$(mem_value MemTotal:)" "$(mem_value MemAvailable:)"
  printf 'swap: %s\n' "$(mem_value SwapTotal:)"
  printf 'disk: %s\n' "$(read_command df -h -- "$output_dir" | tail -n 1)"
  printf 'kernel: %s\n' "$(read_command uname -srvm)"
  printf 'Pi: %s\n' "$(first_line "$(read_command pi --version)")"
} >"$report" || exit 1

printf '%s\n' "$report"
