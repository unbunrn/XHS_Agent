"""Start only the approved project Docker bridge, from the user's normal desktop."""
from pathlib import Path
import hashlib, json, os, socket, subprocess, sys, time, urllib.request, urllib.error
ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT/'config/xhs-compose.yml'
OUT = ROOT/'var/xhs-container'
# Exact digest of the reviewed compose file; no arbitrary user/remote configuration.
EXPECTED_COMPOSE_SHA256 = '99a4d57c3b043276f603910748cab5a3af28794a76c6960ed8890e5c8f59d7f4'
CONTAINER = 'bot-xhs-bridge'
PROJECT = 'bot-xhs'
TOKEN = ''

def stage(value, **extra):
    OUT.mkdir(parents=True, exist_ok=True)
    payload={'stage':value,'at_unix':time.time(),'published':False,**extra}
    (OUT/'start-status.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    print(value,flush=True)

def run(args, *, env=None, timeout=30, progress=False):
    command=['docker','--context','desktop-linux',*args]
    if progress:
        log_path=OUT/'container-start.log'
        with log_path.open('a',encoding='utf-8') as log:
            result=subprocess.run(command,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=timeout)
        output=log_path.read_text(encoding='utf-8',errors='replace')[-500:]
    else:
        result=subprocess.run(command,cwd=ROOT,env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout)
        output=result.stdout
    if result.returncode:
        detail=(output if progress else result.stderr)[-400:]
        if TOKEN: detail=detail.replace(TOKEN,'[REDACTED]')
        raise RuntimeError('Docker command failed: '+detail)
    return output.strip()

def verify_local_context(host):
    if host not in {'npipe:////./pipe/dockerDesktopLinuxEngine','npipe:////./pipe/docker_engine'}:
        raise RuntimeError('Docker context is not the approved local Windows engine.')

def verify_compose():
    if hashlib.sha256(COMPOSE.read_bytes()).hexdigest()!=EXPECTED_COMPOSE_SHA256:
        raise RuntimeError('Container configuration changed; review it before starting.')

def main():
    global TOKEN
    verify_compose()
    stage('checking_local_docker')
    host=json.loads(run(['context','inspect','desktop-linux','--format','{{json .Endpoints.docker.Host}}']))
    verify_local_context(host)
    version=json.loads(run(['version','--format','{{json .Server.Version}}'],timeout=15))
    if not version: raise RuntimeError('Docker engine is not ready.')
    existing=run(['container','ls','-a','--filter','name=^/bot-xhs-bridge$','--format','{{.ID}}'])
    if existing:
        labels=json.loads(run(['inspect','--format','{{json .Config.Labels}}',CONTAINER])) or {}
        if labels.get('com.docker.compose.project')!=PROJECT:
            raise RuntimeError('Container name is owned by another project; refusing to replace it.')
    else:
        with socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(('127.0.0.1',18060))==0:
                raise RuntimeError('Port 18060 is already occupied; no process will be stopped.')
    sys.path.insert(0,str(ROOT))
    from bot.config import load_config
    cfg=load_config(ROOT/'config/bot.toml',root=ROOT,require_file=True)
    TOKEN=cfg.secrets.xiaohongshu_token or ''
    if not TOKEN: raise RuntimeError('Local bridge token is missing.')
    env=os.environ.copy();env['AUTH_TOKEN']=TOKEN
    (OUT/'data').mkdir(parents=True,exist_ok=True)
    (ROOT/'var/drafts').mkdir(parents=True,exist_ok=True)
    compose=['compose','--file',str(COMPOSE),'--project-name',PROJECT]
    run([*compose,'config','--quiet'],env=env)
    stage('downloading_approved_image',docker_version=version)
    run([*compose,'pull','bridge'],env=env,timeout=1200,progress=True)
    stage('starting_project_bridge')
    run([*compose,'up','--detach','bridge'],env=env,timeout=120,progress=True)
    http=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline=time.monotonic()+90
    while time.monotonic()<deadline:
        try:
            with http.open('http://127.0.0.1:18060/health',timeout=3) as response:
                health_ok=response.status==200 and json.load(response).get('success') is True
            if health_ok: break
        except (OSError,ValueError): pass
        time.sleep(2)
    else: raise RuntimeError('Container started but bridge health did not become ready.')
    try:
        with http.open('http://127.0.0.1:18060/api/v1/login/status',timeout=5):
            raise RuntimeError('Bridge did not reject an unauthenticated request.')
    except urllib.error.HTTPError as exc:
        if exc.code!=401: raise RuntimeError('Unexpected authentication check status.')
    stage('ready_for_qr',health_http=200,unauthenticated_http=401,container=CONTAINER,docker_version=version)
    print('Ready. Return to the chat for your Xiaohongshu login QR code.',flush=True)
    return 0

if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        detail=str(exc) if isinstance(exc,RuntimeError) else type(exc).__name__
        if TOKEN: detail=detail.replace(TOKEN,'[REDACTED]')
        stage('failed',error=detail)
        print('Start did not finish. See var/xhs-container/start-status.json.',flush=True)
        raise SystemExit(1)
