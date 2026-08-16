#!/usr/bin/env bash
# Run a deterministic, bounded direct llama.cpp baseline and emit sanitized JSON.
set -u

usage() {
  printf 'Usage: %s --model FILE --sha256 HEX --llama-cli FILE [options]\n' "$0"
  printf 'Options: --prompt TEXT --expected-completion TEXT --context TOKENS\n'
  printf '         --timeout SECONDS --gpu-layers N --flash-attn on|off\n'
  printf '         --batch-size N --ubatch-size N --cache-type-k q8_0 --cache-type-v q8_0\n'
  printf '         --max-stdout-bytes N --max-stderr-bytes N --output FILE\n'
}
model=''; expected_sha=''; cli=''; prompt='Say hello in one short sentence.'
context=32768; timeout_seconds=300; output=''; expected_completion=''; gpu_layers="${BASELINE_GPU_LAYERS:-20}"
flash_attention='on'; batch_size=256; ubatch_size=128; cache_type_k='q8_0'; cache_type_v='q8_0'
max_stdout_bytes="${BASELINE_MAX_STDOUT_BYTES:-1048576}"
max_stderr_bytes="${BASELINE_MAX_STDERR_BYTES:-4194304}"
while (($#)); do
  case "$1" in
    --model) (($# >= 2)) || { usage >&2; exit 2; }; model=$2; shift 2 ;;
    --sha256) (($# >= 2)) || { usage >&2; exit 2; }; expected_sha=$2; shift 2 ;;
    --llama-cli) (($# >= 2)) || { usage >&2; exit 2; }; cli=$2; shift 2 ;;
    --prompt) (($# >= 2)) || { usage >&2; exit 2; }; prompt=$2; shift 2 ;;
    --expected-completion) (($# >= 2)) || { usage >&2; exit 2; }; expected_completion=$2; shift 2 ;;
    --context) (($# >= 2)) || { usage >&2; exit 2; }; context=$2; shift 2 ;;
    --timeout) (($# >= 2)) || { usage >&2; exit 2; }; timeout_seconds=$2; shift 2 ;;
    --gpu-layers) (($# >= 2)) || { usage >&2; exit 2; }; gpu_layers=$2; shift 2 ;;
    --flash-attn) (($# >= 2)) || { usage >&2; exit 2; }; flash_attention=$2; shift 2 ;;
    --batch-size) (($# >= 2)) || { usage >&2; exit 2; }; batch_size=$2; shift 2 ;;
    --ubatch-size) (($# >= 2)) || { usage >&2; exit 2; }; ubatch_size=$2; shift 2 ;;
    --cache-type-k) (($# >= 2)) || { usage >&2; exit 2; }; cache_type_k=$2; shift 2 ;;
    --cache-type-v) (($# >= 2)) || { usage >&2; exit 2; }; cache_type_v=$2; shift 2 ;;
    --max-stdout-bytes|--stdout-max-bytes) (($# >= 2)) || { usage >&2; exit 2; }; max_stdout_bytes=$2; shift 2 ;;
    --max-stderr-bytes|--stderr-max-bytes) (($# >= 2)) || { usage >&2; exit 2; }; max_stderr_bytes=$2; shift 2 ;;
    --output) (($# >= 2)) || { usage >&2; exit 2; }; output=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done
[[ -n "$model" && -n "$expected_sha" && -n "$cli" && -n "$output" ]] || { usage >&2; exit 2; }
[[ -r "$model" ]] || { printf 'model is not readable\n' >&2; exit 1; }
[[ "$expected_sha" =~ ^[[:xdigit:]]{64}$ ]] || { printf 'invalid SHA-256\n' >&2; exit 2; }
[[ "$context" =~ ^[0-9]+$ && "$context" -ge 32768 ]] || { printf 'context must be at least 32768\n' >&2; exit 2; }
[[ "$flash_attention" == on || "$flash_attention" == off ]] || { printf 'invalid flash attention setting\n' >&2; exit 2; }
[[ "$batch_size" =~ ^[0-9]+$ && "$batch_size" -gt 0 ]] || { printf 'invalid batch size\n' >&2; exit 2; }
[[ "$ubatch_size" =~ ^[0-9]+$ && "$ubatch_size" -gt 0 ]] || { printf 'invalid ubatch size\n' >&2; exit 2; }
[[ "$cache_type_k" == q8_0 && "$cache_type_v" == q8_0 ]] || { printf 'KV cache must be q8_0 for both K and V\n' >&2; exit 2; }
[[ "$timeout_seconds" =~ ^[0-9]+([.][0-9]+)?$ ]] || { printf 'invalid timeout\n' >&2; exit 2; }
awk -v timeout="$timeout_seconds" 'BEGIN {exit !(timeout > 0)}' || { printf 'timeout must be greater than zero\n' >&2; exit 2; }
[[ "$gpu_layers" =~ ^[0-9]+$ ]] || { printf 'invalid GPU layers\n' >&2; exit 2; }
[[ "$max_stdout_bytes" =~ ^[0-9]+$ && "$max_stderr_bytes" =~ ^[0-9]+$ ]] || { printf 'invalid capture limit\n' >&2; exit 2; }
command -v sha256sum >/dev/null 2>&1 || { printf 'sha256sum is required\n' >&2; exit 1; }
[[ -x "$cli" ]] || { printf 'llama-cli is not executable\n' >&2; exit 1; }
cli_version=$($cli --version 2>&1) || { printf 'cannot read llama-cli version\n' >&2; exit 1; }
[[ "$cli_version" =~ adb55e5 ]] || { printf 'llama-cli commit mismatch: expected b10446/adb55e5\n' >&2; exit 1; }
actual_sha=$(sha256sum -- "$model" | awk '{print $1}')
if [[ "${actual_sha,,}" != "${expected_sha,,}" ]]; then printf 'model checksum mismatch\n' >&2; exit 1; fi
mkdir -p -- "$(dirname -- "$output")" || exit 1

tmp=$(mktemp -d)
runner_pid=''; watchdog_pid=''; stdout_cap_pid=''; stderr_cap_pid=''; monitor_pid=''; cleaned=false
terminate_runner_group() {
  [[ -n "$runner_pid" ]] || return 0
  # setsid gives the supervisor and every CLI descendant a private process group;
  # kill the group so descendants cannot retain the capture FIFOs.
  kill -TERM -- "-$runner_pid" 2>/dev/null || true
  sleep 0.05
  kill -KILL -- "-$runner_pid" 2>/dev/null || true
  kill -TERM "$runner_pid" 2>/dev/null || true
}
cleanup() {
  [[ "$cleaned" == true ]] && return
  cleaned=true
  [[ -n "$monitor_pid" ]] && kill "$monitor_pid" 2>/dev/null || true
  [[ -n "$watchdog_pid" ]] && kill "$watchdog_pid" 2>/dev/null || true
  terminate_runner_group
  [[ -n "$stdout_cap_pid" ]] && kill "$stdout_cap_pid" 2>/dev/null || true
  [[ -n "$stderr_cap_pid" ]] && kill "$stderr_cap_pid" 2>/dev/null || true
  rm -rf -- "$tmp"
}
on_term() { cleanup; exit 143; }
on_int() { cleanup; exit 130; }
trap cleanup EXIT
trap on_term TERM
trap on_int INT
stdout_file=$tmp/stdout; stderr_file=$tmp/stderr
stdout_fifo=$tmp/stdout.fifo; stderr_fifo=$tmp/stderr.fifo
mkfifo "$stdout_fifo" "$stderr_fifo"
# Consume the complete pipes, retaining only the prefix. This prevents a noisy or
# wedged child from filling disk, while allowing it to exit normally.
capper=$tmp/cap.py
cat >"$capper" <<'PY'
import json, os, sys, time
source, destination, limit, marker, evidence = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5]
written = 0; reads = []
with open(destination, 'wb') as out, open(source, 'rb', buffering=0) as inp:
    while True:
        chunk = os.read(inp.fileno(), 65536)
        if not chunk: break
        reads.append({'timestamp': time.time(), 'read_boundary': len(chunk)})
        if written < limit:
            piece = chunk[:limit - written]
            out.write(piece); written += len(piece)
with open(marker, 'w') as state:
    state.write('true' if written >= limit else 'false')
with open(evidence, 'w') as stream:
    json.dump(reads, stream)
PY
python3 "$capper" "$stdout_fifo" "$stdout_file" "$max_stdout_bytes" "$tmp/stdout.truncated" "$tmp/stdout.evidence.json" & stdout_cap_pid=$!
python3 "$capper" "$stderr_fifo" "$stderr_file" "$max_stderr_bytes" "$tmp/stderr.truncated" "$tmp/stderr.evidence.json" & stderr_cap_pid=$!

# --simple-io keeps the stream machine-readable (no interactive banner or UI), while
# --single-turn and --no-display-prompt make this a deterministic one-shot prompt.
run_args=(--model "$model" --ctx-size "$context" --device Vulkan0 --gpu-layers "$gpu_layers" --flash-attn "$flash_attention" --batch-size "$batch_size" --ubatch-size "$ubatch_size" --cache-type-k "$cache_type_k" --cache-type-v "$cache_type_v" --reasoning off --temp 0 --seed 42 --single-turn --simple-io --verbose --no-display-prompt --prompt "$prompt" --n-predict 128)
measure_file=$tmp/measure.json
sysfs_root=${BASELINE_SYSFS_ROOT:-/sys}
target_vram_file=''; target_vram_capacity='unavailable'; target_vram_card='unavailable'; target_vram_pci_id='unavailable'
for card_device in "$sysfs_root"/class/drm/card*/device; do
  [[ -d "$card_device" ]] || continue
  card_name=$(basename "${card_device%/device}")
  [[ "$card_name" =~ ^card[0-9]+$ ]] || continue
  pci=$(awk -F= '$1=="PCI_ID" {print toupper($2); exit}' "$card_device/uevent" 2>/dev/null || true)
  if [[ -z "$pci" ]]; then
    vendor=$(tr -d '[:space:]' <"$card_device/vendor" 2>/dev/null || true)
    device_id=$(tr -d '[:space:]' <"$card_device/device" 2>/dev/null || true)
    pci="${vendor#0x}:${device_id#0x}"; pci=${pci^^}
  fi
  if [[ "$pci" == '1002:73BF' ]]; then
    target_vram_file="$card_device/mem_info_vram_used"
    total_bytes=$(awk '{print $1; exit}' "$card_device/mem_info_vram_total" 2>/dev/null || true)
    [[ "$total_bytes" =~ ^[0-9]+$ ]] && target_vram_capacity=$((total_bytes / 1024 / 1024))
    target_vram_card=$(basename "${card_device%/device}")
    target_vram_pci_id=$pci
    break
  fi
done
if [[ -n "${BASELINE_MEASURE_FILE:-}" && -r "${BASELINE_MEASURE_FILE}" ]]; then cp -- "${BASELINE_MEASURE_FILE}" "$measure_file"; else printf '{"ram_mib":null,"vram_mib":null,"swap_mib":null}\n' >"$measure_file"; fi
if [[ -z "${BASELINE_MEASURE_FILE:-}" ]]; then
  monitor() {
    local ram=0 swap=0 vram=0 vram_peak=0 available=0 samples=0 system_swap_peak=0 swap_in0='' swap_out0='' swap_in=0 swap_out=0 pid="$1" value card child pids queue sample_ram sample_swap sample_available system_swap_now vm_in vm_out
    while kill -0 "$pid" 2>/dev/null; do
      # timeout is only a supervisor; include the CLI and all descendants.
      pids="$pid"; queue="$pid"
      while [[ -n "$queue" ]]; do
        child=''
        for parent in $queue; do
          descendants=$(pgrep -P "$parent" 2>/dev/null || true)
          [[ -n "$descendants" ]] && child="$child $descendants"
        done
        [[ -n "$child" ]] || { queue=''; continue; }
        pids="$pids $child"; queue="$child"
      done
      sample_ram=0; sample_swap=0
      for child in $pids; do
        [[ -r "/proc/$child/status" ]] || continue
        value=$(awk '$1=="VmRSS:" {print $2; exit}' "/proc/$child/status"); [[ "$value" =~ ^[0-9]+$ ]] && ((sample_ram += value))
        value=$(awk '$1=="VmSwap:" {print $2; exit}' "/proc/$child/status"); [[ "$value" =~ ^[0-9]+$ ]] && ((sample_swap += value))
      done
      ((sample_ram > ram)) && ram=$sample_ram
      ((sample_swap > swap)) && swap=$sample_swap
      sample_available=$(awk '$1=="MemAvailable:" {print $2; exit}' /proc/meminfo 2>/dev/null)
      if [[ "$sample_available" =~ ^[0-9]+$ ]] && { ((available == 0)) || ((sample_available < available)); }; then available=$sample_available; fi
      system_swap_now=$(awk '$1=="SwapTotal:" {total=$2} $1=="SwapFree:" {free=$2} END {if (total >= free) print total-free}' /proc/meminfo 2>/dev/null)
      [[ "$system_swap_now" =~ ^[0-9]+$ ]] && ((system_swap_now > system_swap_peak)) && system_swap_peak=$system_swap_now
      vm_in=$(awk '$1=="pswpin" {print $2; exit}' /proc/vmstat 2>/dev/null); vm_out=$(awk '$1=="pswpout" {print $2; exit}' /proc/vmstat 2>/dev/null)
      [[ -z "$swap_in0" && "$vm_in" =~ ^[0-9]+$ ]] && swap_in0=$vm_in
      [[ -z "$swap_out0" && "$vm_out" =~ ^[0-9]+$ ]] && swap_out0=$vm_out
      [[ "$vm_in" =~ ^[0-9]+$ && "$swap_in0" =~ ^[0-9]+$ ]] && swap_in=$((vm_in - swap_in0))
      [[ "$vm_out" =~ ^[0-9]+$ && "$swap_out0" =~ ^[0-9]+$ ]] && swap_out=$((vm_out - swap_out0))
      vram=0
      if [[ -n "$target_vram_file" && -r "$target_vram_file" ]]; then
        value=$(awk '{print $1; exit}' "$target_vram_file")
        [[ "$value" =~ ^[0-9]+$ ]] && vram=$value
      fi
      ((vram > vram_peak)) && vram_peak=$vram
      ((samples++))
      printf '{"ram_mib":%s,"vram_mib":%s,"swap_mib":%s,"mem_available_mib":%s,"system_swap_used_mib":%s,"swap_in_pages":%s,"swap_out_pages":%s,"samples":%s}\n' "$((ram / 1024))" "$((vram_peak / 1024 / 1024))" "$((swap / 1024))" "$((available / 1024))" "$((system_swap_peak / 1024))" "$swap_in" "$swap_out" "$samples" >"$measure_file"
      sleep 1
    done
  }
fi
set +e
# Run the CLI in its own session and attribute deadlines with our own marker;
# this distinguishes a natural child exit 124 from a watchdog timeout.
setsid --wait "$cli" "${run_args[@]}" >"$stdout_fifo" 2>"$stderr_fifo" & runner_pid=$!
(
  sleep "$timeout_seconds"
  if kill -0 "$runner_pid" 2>/dev/null; then
    : >"$tmp/timed_out"
    kill -TERM -- "-$runner_pid" 2>/dev/null || true
    sleep 2
    kill -KILL -- "-$runner_pid" 2>/dev/null || true
  fi
) & watchdog_pid=$!
[[ -z "${BASELINE_MEASURE_FILE:-}" ]] && monitor "$runner_pid" & monitor_pid=$! || monitor_pid=''
wait "$runner_pid"; status=$?
[[ -n "$watchdog_pid" ]] && kill "$watchdog_pid" 2>/dev/null || true
[[ -n "$watchdog_pid" ]] && wait "$watchdog_pid" 2>/dev/null || true
# Reap/terminate any CLI child that inherited a FIFO before waiting for cappers.
terminate_runner_group
wait "$stdout_cap_pid" 2>/dev/null; wait "$stderr_cap_pid" 2>/dev/null
[[ -n "$monitor_pid" ]] && kill "$monitor_pid" 2>/dev/null || true
[[ -n "$monitor_pid" ]] && wait "$monitor_pid" 2>/dev/null || true
set -e
[[ -f "$tmp/timed_out" ]] && timed_out=true || timed_out=false
measurements=$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))))' "$measure_file" 2>/dev/null || printf '{"ram_mib":null,"vram_mib":null,"swap_mib":null}')
export model cli cli_version prompt expected_completion context timeout_seconds actual_sha expected_sha status timed_out output measurements stdout_file stderr_file gpu_layers flash_attention batch_size ubatch_size cache_type_k cache_type_v max_stdout_bytes max_stderr_bytes target_vram_capacity target_vram_card target_vram_pci_id BASELINE_SYSFS_ROOT="${BASELINE_SYSFS_ROOT:-}" STDOUT_EVIDENCE_FILE="$tmp/stdout.evidence.json"
python3 - <<'PY'
import json, os, pathlib, re

def clean(value):
    s = str(value)
    # Allowlist-oriented output: redact credential material before collapsing
    # arbitrary absolute paths to their final component.
    s = re.sub(r'(?is)-+BEGIN[^\n]*PRIVATE KEY-+.*?-+END[^\n]*PRIVATE KEY-+', '[PRIVATE KEY REDACTED]', s)
    s = re.sub(r'(?i)bearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [REDACTED]', s)
    s = re.sub(r'(?i)ghp_[A-Za-z0-9]+', '[GITHUB TOKEN REDACTED]', s)
    s = re.sub(r'(?i)(api[_-]?key|access[_-]?token|secret|password)\s*[=:]\s*[^\s,]+', r'\1=[REDACTED]', s)
    s = re.sub(r'(?i)https?://[^\s/@]+:[^\s/@]+@', 'https://[CREDENTIALS REDACTED]@', s)
    s = re.sub(r'(?<![A-Za-z0-9])/(?!/)[^\s"\']+', lambda m: pathlib.PurePosixPath(m.group(0)).name, s)
    return s
def read(name): return pathlib.Path(os.environ[name]).read_text(errors='replace')
stdout, stderr = read('stdout_file'), read('stderr_file')
combined = stdout + "\n" + stderr
# Do not accept a generic Vulkan banner: the target adapter must be identified.
device = next((clean(x.strip()) for x in combined.splitlines()
               if re.search(r'(?i)vulkan', x) and re.search(r'(?i)RX[ -]?6900', x)), 'unavailable')
ctx_match = re.search(r'(?i)(?:n_ctx|context(?: size| tokens)?)[^0-9]{0,20}(' + re.escape(os.environ['context']) + r')', combined)
confirmed_context = bool(ctx_match)
stop_match = re.search(r'(?i)stop reason\s*[:=]\s*([^\s]+)', combined)
stop = clean(stop_match.group(1)) if stop_match else 'unknown'
if re.search(r'(?i)stopped\s+by\s+EOS', combined):
    stop_event = 'stopped-by-EOS'
    stop = 'stop'
elif re.search(r'(?i)finish_reason\s*=\s*stop', combined):
    stop_event = 'finish_reason=stop'
    stop = 'stop'
elif stop_match:
    stop_event = 'stop'
else:
    stop_event = 'unknown'
# llama.cpp versions print timings in several formats. A stop reason is also
# terminal evidence for the bounded single-turn run (and is present in the
# small fake fixture used by the portable gate).
timing_match = re.search(r'(?im)(?:prompt eval|eval|total).*?(?:time|t/s|tokens?/s)', combined)
timing_evidence = bool(timing_match)
def timing_metric(kind):
    line = next((x for x in combined.splitlines() if re.search(kind, x, re.I)), '')
    ms = re.search(r'=\s*([0-9]+(?:\.[0-9]+)?)\s*ms', line, re.I)
    tokens = re.search(r'/\s*([0-9]+)\s*(?:tokens?|runs?)', line, re.I)
    tps = re.findall(r'([0-9]+(?:\.[0-9]+)?)\s*(?:tokens?/s|tokens?\s+per\s+second)', line, re.I)
    return (float(ms.group(1)) if ms else None, int(tokens.group(1)) if tokens else None, float(tps[-1]) if tps else None)
prompt_timing = timing_metric(r'prompt\s+eval')
generation_timing = timing_metric(r'(?<!prompt\s)eval\s+time')
# With --simple-io stdout is the completion stream. Remove recognizable CLI
# banner lines before checking it, so an exit-0 UI/banner-only run is rejected.
ui_line = re.compile(r'(?i)^\s*(?:llama[- ]?cli(?:\s+version)?|build info|system[_ ]info|sampling|command|prompt|main|input)\s*[:=]|^\s*>\s*')
noise_line = re.compile(r'(?i)(?:^|\s)(?:timing|prompt eval|eval time|tokens?/s|exit(?:ing)?|exiting|llama_[a-z_]+)\b')
stream_text = '\n'.join(line for line in stdout.splitlines() if not ui_line.search(line) and not noise_line.search(line)).strip()
prompt_text = os.environ['prompt']
if prompt_text and stream_text.startswith(prompt_text):
    stream_text = stream_text[len(prompt_text):].lstrip()
thinking_parts = re.findall(r'(?is)<think(?:ing)?>(.*?)</think(?:ing)?>', stream_text)
thinking = clean('\n'.join(part.strip() for part in thinking_parts if part.strip()))
final_response = re.sub(r'(?is)<think(?:ing)?>.*?</think(?:ing)?>', '', stream_text).strip()
# An unterminated thinking block is not a final response.
if re.search(r'(?is)<think(?:ing)?>', final_response):
    thinking = clean(final_response)
    final_response = ''
expected = os.environ.get('expected_completion', '')
expected_clean = clean(expected)
ansi = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
raw_chunks = [clean(x.strip()) for x in stdout.splitlines() if x.strip()]
stdout_lines = [clean(ansi.sub('', x).strip()) for x in stdout.splitlines()]
expected_indexes = [i for i, line in enumerate(stdout_lines) if line == expected_clean]
# After the exact response, only known terminal UI/timing lines are allowed.
terminal_noise = re.compile(r'(?i)^(?:\[\s*Prompt:.*\]|Exiting\.\.\.|\s*)$')
exact_terminal = bool(expected_indexes) and not any(
    line and not terminal_noise.match(line) for line in stdout_lines[expected_indexes[-1] + 1:]
)
cleaned_final = clean(final_response)
expected_match = ((not expected_clean and bool(cleaned_final)) or
                  (bool(expected_clean) and exact_terminal and cleaned_final == expected_clean))
completion = cleaned_final
response_chunks = [clean(x.strip()) for x in final_response.splitlines() if x.strip()]
# Preserve observed FIFO read boundaries and timestamps instead of claiming
# that post-processed output lines are transport chunks.
try: chunk_evidence = json.loads(pathlib.Path(os.environ['STDOUT_EVIDENCE_FILE']).read_text())
except Exception: chunk_evidence = []
chunks = response_chunks
offload_log_evidence = bool(re.search(r'(?i)(offload|offloaded|layers?.*(?:vulkan|gpu)|(?:vulkan|gpu).*layers?)', combined))
metadata = [clean(x.strip()) for x in combined.splitlines() if re.search(r'(?i)(llama_model_loader|^llama_vocab|^general\.|^tokenizer\.)', x)]
try: measurements = json.loads(os.environ['measurements'])
except Exception: measurements = {'ram_mib': None, 'vram_mib': None, 'swap_mib': None}
# The injected file is explicitly fixture evidence, never live evidence.
measurement_source = 'fixture' if os.environ.get('BASELINE_MEASURE_FILE') else 'live'
# Fixture doubles may omit verbose offload lines; live evidence must show it.
offload_evidence = offload_log_evidence or (measurement_source == 'fixture' and int(os.environ['gpu_layers']) > 0)
for key in ('ram_mib', 'vram_mib', 'swap_mib'):
    if not isinstance(measurements.get(key), int) or measurements[key] < 0:
        measurements[key] = None
swap_peak = measurements.get('swap_mib')
system_swap_peak = measurements.get('system_swap_used_mib')
swap_in_pages = measurements.get('swap_in_pages')
swap_out_pages = measurements.get('swap_out_pages')
vr_match = re.search(r'(?i)(?:device\s*=|deviceName\s*[:=])\s*((?:AMD\s+)?Radeon\s+RX\s*6900\s*XT)', combined)
vram_device = clean(vr_match.group(1)) if vr_match else 'unavailable'
if vram_device != 'unavailable' and not vram_device.lower().startswith('amd '):
    vram_device = 'AMD ' + vram_device
# Associate VRAM accounting with the discrete adapter by PCI identity, not a
# marketing string. The iGPU is card0 (1002:13C0); the target is card1 (1002:73BF).
vram_card = os.environ.get('target_vram_card', 'unavailable')
vram_pci_id = os.environ.get('target_vram_pci_id', 'unavailable')
# Fixtures may provide an injectable sysfs root and still use a measurement
# file; the shell collector resolves the same PCI identity in both modes.
# The verbose Vulkan inventory names the adapter while sysfs supplies the
# unambiguous PCI association used for its VRAM counter.
if device != 'unavailable' and vram_pci_id == '1002:73BF':
    vram_device = 'AMD Radeon RX 6900 XT'
cmd = ['llama-cli', '--model', pathlib.Path(os.environ['model']).name, '--ctx-size', os.environ['context'], '--device', 'Vulkan0', '--gpu-layers', os.environ['gpu_layers'], '--flash-attn', os.environ['flash_attention'], '--batch-size', os.environ['batch_size'], '--ubatch-size', os.environ['ubatch_size'], '--cache-type-k', os.environ['cache_type_k'], '--cache-type-v', os.environ['cache_type_v'], '--reasoning', 'off', '--temp', '0', '--seed', '42', '--single-turn', '--simple-io', '--verbose', '--no-display-prompt', '--prompt', clean(os.environ['prompt']), '--n-predict', '128']
result = {'schema_version': 1, 'model': pathlib.Path(os.environ['model']).name, 'model_sha256': os.environ['actual_sha'], 'llama_cpp': {'release': 'b10446', 'commit': 'adb55e5', 'version_output': clean(os.environ['cli_version'])}, 'command': cmd, 'device': device, 'context_tokens': int(os.environ['context']), 'context_confirmed': confirmed_context, 'gpu_layers': int(os.environ['gpu_layers']), 'offload_evidence': offload_evidence, 'reasoning_mode': 'off', 'expected_completion': expected_clean, 'expected_completion_match': expected_match, 'final_section_confirmed': bool(completion) and expected_match, 'stream': {'completion': completion, 'chunks': chunks, 'response_chunks': response_chunks, 'chunk_evidence': chunk_evidence, 'capture_mode': 'fifo-read-boundaries', 'thinking': thinking, 'prompt': clean(prompt_text), 'enabled': bool(response_chunks and chunk_evidence)}, 'stop_reason': stop, 'stop_event': stop_event, 'timing_evidence': timing_evidence, 'metrics': {'prompt_eval_ms': prompt_timing[0], 'prompt_tokens': prompt_timing[1], 'prompt_tokens_per_second': prompt_timing[2], 'generation_eval_ms': generation_timing[0], 'generation_tokens': generation_timing[1], 'generation_tokens_per_second': generation_timing[2]}, 'vram_device': vram_device, 'vram_card': vram_card, 'vram_pci_id': vram_pci_id, 'measurement_source': measurement_source, 'vram_capacity_mib': int(os.environ['target_vram_capacity']) if os.environ['target_vram_capacity'].isdigit() else None, 'swap_activity': {'peak_mib': swap_peak, 'system_used_peak_mib': system_swap_peak, 'pages_in': swap_in_pages, 'pages_out': swap_out_pages}, 'exit_code': int(os.environ['status']), 'timed_out': os.environ['timed_out'] == 'true', 'measurements': measurements, 'model_metadata': metadata, 'startup_log': clean(stderr), 'capture_limits': {'stdout_bytes': int(os.environ['max_stdout_bytes']), 'stderr_bytes': int(os.environ['max_stderr_bytes'])}}
pathlib.Path(os.environ['output']).write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
PY
if [[ "$timed_out" == true || $status -ne 0 ]]; then exit 1; fi
# Exit zero is not sufficient: require live startup/device/context and a real
# streamed response with terminal evidence in the bounded captures.
evidence_ok=$(python3 - "$output" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
print(1 if (r.get('device') != 'unavailable'
           and r.get('context_confirmed') is True
           and r.get('stream', {}).get('completion', '').strip()
           and len(r.get('stream', {}).get('chunks', [])) > 0
           and r.get('final_section_confirmed') is True
           and r.get('offload_evidence') is True
           and r.get('vram_device') == 'AMD Radeon RX 6900 XT'
           and (r.get('measurement_source') == 'fixture' or r.get('vram_pci_id') == '1002:73BF')
           and all(isinstance(r.get('measurements', {}).get(k), int) and r['measurements'][k] >= 0 for k in ('ram_mib', 'vram_mib', 'swap_mib'))
           and (r.get('measurement_source') == 'fixture' or (r.get('measurements', {}).get('samples', 0) >= 2 and isinstance(r.get('swap_activity', {}).get('system_used_peak_mib'), int) and isinstance(r.get('swap_activity', {}).get('pages_in'), int) and isinstance(r.get('swap_activity', {}).get('pages_out'), int)))
           and (r.get('stop_event') != 'unknown' or r.get('timing_evidence') is True)) else 0)
PY
)
if [[ "$evidence_ok" != 1 ]]; then
  printf 'required live baseline evidence missing (device/context/completion/terminal evidence)\n' >&2
  exit 1
fi
exit 0
