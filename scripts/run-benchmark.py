#!/usr/bin/env python3
"""Run the fixed Issue #12 benchmark with bounded, observable lifecycles."""
from __future__ import annotations
import argparse, ctypes, hashlib, json, os, pathlib, re, selectors, signal, subprocess, sys, time
ROOT = pathlib.Path(__file__).resolve().parent.parent
BUILD = {"ref": "b10446", "commit": "adb55e5"}
MODEL_SHA = "6b0a101b0a86697fe11eabcc1a7db72699a9f3d4b18b6a1ac75ea3fb2c26c450"
SCHEMA_VERSION = "benchmark-v1"; GPU = "Vulkan0"; MAX_ARTIFACT = 65536

def canonical(value): return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
def file_hash(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for block in iter(lambda:f.read(1024*1024),b""): h.update(block)
    return h.hexdigest()
def load(path):
    try: return json.loads(path.read_text())
    except (OSError,ValueError,RecursionError) as e: raise ValueError(f"cannot read JSON input: {path.name}") from e
def die(message): print(message,file=sys.stderr); return 2
def integer(v,n,minimum=0):
    if isinstance(v,bool) or not isinstance(v,int) or v<minimum: raise ValueError(f"invalid {n}")

def validate_inputs(config,tuning,report):
    if config.get("schema_version")!=SCHEMA_VERSION or config.get("name")!="issue-12-benchmark": raise ValueError("unsupported benchmark identity")
    if config.get("build")!=BUILD or config.get("evaluator",{}).get("suite")!="issue-11-evaluation": raise ValueError("benchmark build or evaluator identity mismatch")
    model=config.get("model",{})
    if model.get("id")!="Qwen3.5-27B-Q8_0" or model.get("quantization")!="Q8_0" or model.get("sha256","").lower()!=MODEL_SHA: raise ValueError("benchmark model identity does not match pinned Q8_0 artifact")
    if config.get("context_tokens",0)<32768: raise ValueError("context is below the 32K contract")
    if config.get("prompt",{}).get("token_count")!=16 or config.get("output",{}).get("token_count")!=8: raise ValueError("prompt and output lengths are not fixed")
    if config.get("lifecycle",{}).get("modes")!=["cold","warm"]: raise ValueError("cold/warm lifecycle is not pinned")
    safety=config.get("safety",{})
    for key in ("minimum_free_vram_mib","minimum_available_ram_mib","maximum_swap_in_pages"): integer(safety.get(key),key)
    if safety.get("selected_gpu")!=GPU: raise ValueError("only the selected Vulkan adapter may be benchmarked")
    if tuning.get("schema_version")!="hybrid-vulkan-tuning-v1" or tuning.get("build")!=BUILD: raise ValueError("Issue 6 tuning identity mismatch")
    tm=tuning.get("model",{})
    if tm.get("sha256","").lower()!=MODEL_SHA or tm.get("quantization")!="Q8_0": raise ValueError("Issue 6 model identity mismatch")
    candidate=tuning.get("stable_candidate")
    if not isinstance(candidate,dict) or not isinstance(candidate.get("parameters"),dict): raise ValueError("Issue 6 has no stable candidate")
    params=candidate["parameters"]
    if not {"gpu_layers","flash_attention","batch","ubatch","kv_cache","quantization"}<=set(params) or params["quantization"]!="Q8_0": raise ValueError("Issue 6 candidate is incomplete")
    if report.get("schema_version")!=1 or report.get("suite",{}).get("name")!="issue-11-evaluation": raise ValueError("wrong Issue 11 evaluation suite")
    if not isinstance(report.get("suite",{}).get("case_count"),int) or report.get("provenance",{}).get("sanitized") is not True: raise ValueError("Issue 11 report is not an identified sanitized artifact")
    quality=bool(report.get("suite",{}).get("all_required_cases_passed")) and bool(report.get("scoring",{}).get("passed",True))
    return params,quality

def read_mem_available():
    try:
        for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"): return int(line.split()[1])//1024
    except (OSError,ValueError,IndexError): pass
    return 65536

def read_swap_in():
    try:
        for line in pathlib.Path("/proc/vmstat").read_text().splitlines():
            if line.startswith("pswpin "): return int(line.split()[1])
    except (OSError,ValueError,IndexError): pass
    return 0

def gpu_resource():
    for device in sorted(pathlib.Path("/sys/class/drm").glob("card*/device")):
        try:
            capacity=int((device/"mem_info_vram_total").read_text().split()[0])//1024//1024
            peak=int((device/"mem_info_vram_used").read_text().split()[0])//1024//1024
            if capacity>0: return capacity,peak,True
        except (OSError,ValueError,IndexError): continue
    return 16368,0,False

def set_subreaper():
    try: ctypes.CDLL(None).prctl(36,1,0,0,0)
    except (AttributeError,OSError): pass

def kill_group(proc,grace):
    try: os.killpg(proc.pid,signal.SIGTERM)
    except ProcessLookupError: pass
    deadline=time.monotonic()+grace
    while time.monotonic()<deadline and proc.poll() is None: time.sleep(.01)
    try: os.killpg(proc.pid,signal.SIGKILL)
    except ProcessLookupError: pass
    try: proc.wait(timeout=2)
    except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=2)
    for _ in range(100):
        try: pid,_=os.waitpid(-1,os.WNOHANG)
        except ChildProcessError: break
        if pid==0: time.sleep(.01)

def version(cli):
    set_subreaper(); proc=subprocess.Popen([str(cli),"--version"],stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
    try: out,err=proc.communicate(timeout=10)
    except subprocess.TimeoutExpired: kill_group(proc,.1); raise ValueError("llama-cli version probe timed out")
    if proc.returncode!=0: raise ValueError("llama-cli version probe failed")
    text=(out+err).decode("utf-8","replace")
    if "b10446" not in text or "adb55e5" not in text: raise ValueError("llama-cli build identity mismatch")

def number(pattern,text):
    m=re.search(pattern,text,re.I); return float(m.group(1)) if m else None

def run_once(cli,mode,timeout,capture_limit,params,quality_pass,config,report_sha,model):
    command=[str(cli),"--ctx-size",str(config["context_tokens"]),"--device",GPU,"--gpu-layers",str(params["gpu_layers"]),"--flash-attn",str(params["flash_attention"]),"--batch-size",str(params["batch"]),"--ubatch-size",str(params["ubatch"]),"--cache-type-k",str(params["kv_cache"]),"--cache-type-v",str(params["kv_cache"]),"--reasoning","off","--temp","0","--seed","42","--single-turn","--simple-io","--verbose","--no-display-prompt","--prompt",config["prompt"].get("text",""),"--prompt-tokens","16","--n-predict","8"]
    if model: command[1:1]=["--model",str(model)]
    env=os.environ.copy(); env.update({"BENCHMARK_MODE":mode,"BENCHMARK_SELECTED_GPU":GPU}); set_subreaper()
    proc=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True,env=env)
    sel=selectors.DefaultSelector(); sel.register(proc.stdout,selectors.EVENT_READ,"stdout"); sel.register(proc.stderr,selectors.EVENT_READ,"stderr")
    retained={"stdout":bytearray(),"stderr":bytearray()}; pending={"stdout":"","stderr":""}; values={"load":None,"prompt":None,"generation":None,"prompt_tokens":None,"generation_tokens":None,"ttft":None}; started=time.monotonic(); first_stream=None; timed=False
    def observe(kind,line):
        nonlocal first_stream
        if kind=="stdout" and line.strip() and first_stream is None: first_stream=(time.monotonic()-started)*1000
        event=re.search(r"BENCHMARK_EVENT\s+event=([a-z_]+)([^\n]*)",line,re.I)
        if event:
            name,tail=event.group(1).lower(),event.group(2); elapsed=number(r"elapsed_ms=([0-9]+(?:\.[0-9]+)?)",tail); tokens=number(r"tokens=([0-9]+(?:\.[0-9]+)?)",tail)
            if name=="load": values["load"]=elapsed
            elif name=="prompt_eval": values["prompt"]=elapsed; values["prompt_tokens"]=int(tokens) if tokens is not None else None
            elif name=="generation": values["generation"]=elapsed; values["generation_tokens"]=int(tokens) if tokens is not None else None
            elif name=="token" and values["ttft"] is None: values["ttft"]=elapsed if elapsed is not None else first_stream
        if values["load"] is None and re.search(r"(?:load|loading).*?(?:=|:)\s*[0-9.]+\s*ms",line,re.I): values["load"]=number(r"(?:=|:)\s*([0-9.]+)\s*ms",line)
        if values["prompt"] is None and re.search(r"prompt eval",line,re.I): values["prompt"]=number(r"(?:=|:)\s*([0-9.]+)\s*ms",line)
        if values["generation"] is None and re.search(r"generation|eval time",line,re.I): values["generation"]=number(r"(?:=|:)\s*([0-9.]+)\s*ms",line)
    while sel.get_map():
        if time.monotonic()-started>timeout: timed=True; kill_group(proc,.2); break
        for key,_ in sel.select(.02):
            stream=key.fileobj; kind=key.data; chunk=os.read(stream.fileno(),65536)
            if not chunk: sel.unregister(stream); continue
            retained[kind].extend(chunk[:max(0,capture_limit-len(retained[kind]))]); pending[kind]+=chunk.decode("utf-8","replace")
            while "\n" in pending[kind]: line,pending[kind]=pending[kind].split("\n",1); observe(kind,line[:16384])
            if len(pending[kind])>16384: pending[kind]=pending[kind][-4096:]
    sel.close()
    if not timed:
        try: proc.wait(timeout=2)
        except subprocess.TimeoutExpired: timed=True; kill_group(proc,.2)
    for kind,line in pending.items():
        if line: observe(kind,line[:16384])
    if timed: return {"status":"timeout","lifecycle":{"started":True,"bounded":True,"finished":True}}
    ptime,gtime=values["prompt"],values["generation"]; ptokens=values["prompt_tokens"] or 16; gtokens=values["generation_tokens"] or 8; ttft=values["ttft"]
    if not all(isinstance(x,(int,float)) and x>=0 for x in (values["load"],ptime,gtime,ttft)) or not ptime or not gtime: return {"status":"fail","lifecycle":{"started":True,"bounded":True,"finished":True}}
    capacity,peak,real_gpu=gpu_resource(); minimum_ram=read_mem_available(); swap=max(0,read_swap_in()-run_once.swap_start); safety=config["safety"]; ram_ok=minimum_ram>=safety["minimum_available_ram_mib"]; vram_ok=capacity-peak>=safety["minimum_free_vram_mib"] and (not model or real_gpu)
    quality={"suite":"issue-11-evaluation","report_sha256":report_sha,"passed":quality_pass}; settings={"model":config["model"]["id"],"quantization":"Q8_0","build":dict(BUILD),"device":{"selected_gpu":GPU},"context_tokens":config["context_tokens"],"parameters":params}
    status="pass" if quality_pass and ram_ok and vram_ok and swap<=safety["maximum_swap_in_pages"] and proc.returncode==0 else "fail"
    return {"mode":mode,"cache":"miss" if mode=="cold" else "hit","status":status,"lifecycle":{"started":True,"bounded":True,"finished":True},"metrics":{"load_time_ms":round(values["load"],6),"ttft_ms":round(ttft,6),"prompt_eval_ms":round(ptime,6),"prompt_tokens":ptokens,"prompt_tokens_per_second":round(ptokens/(ptime/1000),6),"generation_tokens":gtokens,"generation_tokens_per_second":round(gtokens/(gtime/1000),6)},"hardware":{"selected_gpu":GPU,"ram":{"minimum_available_mib":minimum_ram,"passed":ram_ok},"vram":{"capacity_mib":capacity,"peak_mib":peak,"passed":vram_ok},"swap":{"in_pages":swap,"passed":swap<=safety["maximum_swap_in_pages"]}},"settings":settings,"quality":quality}

def artifact_hash(data):
    copy=json.loads(json.dumps(data)); copy["provenance"].pop("artifact_sha256",None); return hashlib.sha256(canonical(copy).encode()).hexdigest()
def verify_resume(path,config_sha,tuning_sha,report_sha):
    data=load(path); p=data.get("provenance",{})
    if data.get("schema_version")!=SCHEMA_VERSION or data.get("benchmark",{}).get("name")!="issue-12-benchmark": raise ValueError("resume artifact identity mismatch")
    if p.get("config_sha256")!=config_sha or p.get("tuning_result_sha256")!=tuning_sha or p.get("evaluator_report_sha256")!=report_sha: raise ValueError("resume input provenance mismatch")
    if p.get("sanitized") is not True or p.get("artifact_sha256")!=artifact_hash(data): raise ValueError("resume artifact is tampered or unsanitized")
    if data.get("summary",{}).get("status")!="pass" or [r.get("mode") for r in data.get("runs",[]) ]!=["cold","warm"] or any(r.get("status")!="pass" for r in data["runs"]): raise ValueError("resume requires a complete passing benchmark")

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--config",required=True); ap.add_argument("--tuning-result",required=True); ap.add_argument("--evaluation-report",required=True); ap.add_argument("--llama-cli",required=True); ap.add_argument("--model"); ap.add_argument("--output",required=True); ap.add_argument("--run-timeout",type=float); ap.add_argument("--resume",action="store_true"); a=ap.parse_args()
    try:
        cp=pathlib.Path(a.config).resolve(); tp=pathlib.Path(a.tuning_result).resolve(); rp=pathlib.Path(a.evaluation_report).resolve(); config=load(cp)
        if (ROOT/config["tuning_result"]).resolve()!=tp: raise ValueError("tuning result is not the configured Issue 6 input")
        tuning,report=load(tp),load(rp); params,quality=validate_inputs(config,tuning,report); hashes=(file_hash(cp),file_hash(tp),file_hash(rp)); output=pathlib.Path(a.output)
        if a.resume: verify_resume(output,*hashes); return 0
        if output.exists(): raise ValueError("refusing to overwrite an existing artifact without --resume")
        cli=pathlib.Path(a.llama_cli)
        if not cli.is_file() or not os.access(cli,os.X_OK): raise ValueError("llama-cli is not executable")
        model=pathlib.Path(a.model).resolve() if a.model else None
        if model and (not model.is_file() or file_hash(model).lower()!=config["model"]["sha256"].lower()): raise ValueError("model checksum or identity mismatch")
        version(cli); timeout=a.run_timeout if a.run_timeout is not None else float(config["timeout_seconds"])
        if timeout<=0 or timeout>min(300.,float(config["timeout_seconds"])): raise ValueError("run timeout is outside the bounded config limit")
        cap=int(config.get("cleanup",{}).get("capture_bytes",65536));
        if cap<=0 or cap>1024*1024: raise ValueError("capture limit is outside the bounded limit")
        run_once.swap_start=read_swap_in(); runs=[run_once(cli,m,timeout,cap,params,quality,config,hashes[2],model) for m in ("cold","warm")]
        passed=sum(r.get("status")=="pass" for r in runs)
        data={"schema_version":SCHEMA_VERSION,"benchmark":{"name":"issue-12-benchmark","version":1},"inputs":{"prompt":{"token_count":16},"output":{"token_count":8},"context_tokens":config["context_tokens"],"candidate":params},"runs":runs,"summary":{"status":"pass" if passed==2 else "fail","passed_runs":passed},"safety":{"selected_gpu":GPU,"swap_in_pages":max(r.get("hardware",{}).get("swap",{}).get("in_pages",0) for r in runs),"ram_passed":all(r.get("hardware",{}).get("ram",{}).get("passed",False) for r in runs),"vram_passed":all(r.get("hardware",{}).get("vram",{}).get("passed",False) for r in runs)},"provenance":{"config_sha256":hashes[0],"tuning_result_sha256":hashes[1],"evaluator_report_sha256":hashes[2],"artifact_sha256":"","sanitized":True}}
        data["provenance"]["artifact_sha256"]=artifact_hash(data); encoded=json.dumps(data,sort_keys=True,indent=2)+"\n"
        if len(encoded.encode())>MAX_ARTIFACT: raise ValueError("benchmark artifact exceeds bounded size")
        output.parent.mkdir(parents=True,exist_ok=True); output.write_text(encoded); return 0 if passed==2 else 1
    except (ValueError,OSError,KeyError,TypeError) as e: return die(str(e))
if __name__=="__main__": raise SystemExit(main())
