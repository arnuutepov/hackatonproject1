"""Sequential real localhost HTTP measurement; no external inference or raw logs."""
import argparse
import json
import os
from pathlib import Path
import platform
import socket
import secrets
import subprocess
import sys
import time
from urllib.request import Request
from support_copilot.local_http import open_local
from urllib.error import URLError

from support_copilot.measure import ROOT,load_control,percentile,implementation_hash


def benchmark(passes=3) -> dict:
    if not 1 <= passes <= 10:
        raise ValueError('Passes must be 1..10')
    rows = load_control()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port = sock.getsockname()[1]
    service_token = secrets.token_urlsafe(32)
    env = {**os.environ, 'COPILOT_AUTH_MODE':'required', 'COPILOT_TRIAGE_TOKEN':service_token,'COPILOT_DRAFT_PROVIDER':'template','COPILOT_VERIFIED_TEMPLATES':'on','PYTHONUTF8':'1'}
    started = time.perf_counter()
    process = subprocess.Popen([sys.executable,'-m','uvicorn','support_copilot.api:app','--host','127.0.0.1',
        '--port',str(port),'--no-access-log','--no-proxy-headers'],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
    try:
        base = f'http://127.0.0.1:{port}'
        while True:
            try:
                with open_local(Request(base+'/health',headers={'Authorization':'Bearer '+service_token}),timeout=1) as response:
                    health = json.load(response)
                break
            except (URLError,TimeoutError):
                if process.poll() is not None or time.perf_counter()-started > 10:
                    raise ValueError('Local benchmark server did not start') from None
                time.sleep(.02)
        startup = (time.perf_counter()-started)*1000
        def post(row):
            payload = json.dumps(row['request'],ensure_ascii=False).encode('utf-8')
            start = time.perf_counter()
            with open_local(Request(base+'/triage',data=payload,headers={'Content-Type':'application/json','Authorization':'Bearer '+service_token}),timeout=10) as response:
                value = json.load(response)
            elapsed = (time.perf_counter()-start)*1000
            if not value.get('draft_available') or value['generation']['attempts']:
                raise ValueError('Unexpected benchmark response or provider attempt')
            return elapsed
        for row in rows[:5]:
            post(row)
        times = [post(row) for _ in range(passes) for row in rows]
        return dict(protocol='wave5-local-control-v1',mode='offline-template-real-localhost-http',
                    n=len(times),distinct_cases=len(rows),passes=passes,warmups_excluded=5,failures=0,
                    cold_startup_ms=startup,p50_ms=percentile(times,.5),p95_ms=percentile(times),
                    max_ms=max(times),p95_limit_ms=5000,local_p95_target_met=percentile(times)<=5000,
                    live_model_p95_ms=None,production_target_met=None,model_api_calls=0,
                    total_cost_usd=None,python=platform.python_version(),platform=platform.platform(),
                    health=health,implementation_sha256=implementation_hash(),raw_latency_ms=times,
                    note='Sequential localhost result includes serialization and client transport, not WAN/model/concurrent production traffic')
    finally:
        process.terminate()
        process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--passes',type=int,default=3)
    args = parser.parse_args()
    report = benchmark(args.passes)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({key:report[key] for key in ('n','p50_ms','p95_ms','max_ms','cold_startup_ms','local_p95_target_met','model_api_calls')}))


if __name__ == '__main__':
    main()
