"""Bounded, explicitly authorized phone approval rehearsal; never runs a publisher."""
from pathlib import Path
import sys, json, threading, os, time
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if '--send-authorized' not in sys.argv:
    raise SystemExit('Requires explicit authorization to send this rehearsal and its result receipt.')
from bot.config import load_config
from bot.db import Database
from bot.timeutil import SystemClock, to_iso
from bot.lark_gateway import LarkLongConnection, parse_message_event
from bot.services.approvals import verify_approval_artifact
from bot.services.notifications import deliver_pending
from bot.adapters.notify import LarkNotifier
import lark_oapi as lark
cfg = load_config(ROOT/'config/feishu-rehearsal.toml', root=ROOT, require_file=True)
out = ROOT/'var/feishu-rehearsal-20261106'
assert cfg.db_path.resolve() == out/'rehearsal.sqlite3'
assert cfg.mode == 'offline' and cfg.get('publisher.provider') == 'mock'
assert not cfg.get('publisher.live_enabled') and not cfg.enabled_sources()
clock = SystemClock(); db = Database(cfg.db_path); db.init()
approval = db.fetchone('SELECT * FROM approvals ORDER BY id DESC LIMIT 1')
assert approval is not None and approval['status'] == 'pending'
valid, reason, _ = verify_approval_artifact(db, approval)
assert valid, reason
code = approval['code']
gateway = LarkLongConnection(cfg, db, clock)
notifier = LarkNotifier(cfg, clock)
connected = threading.Event(); received = threading.Event()
report = {'approval_code':code, 'published':False, 'llm_calls':0, 'delivery':[], 'outcomes':[]}
lock = threading.Lock()
def finish(stage, exit_code):
    with lock:
        report.update(at=to_iso(clock.now()), stage=stage,
            approval_status=db.scalar('SELECT status FROM approvals WHERE id=?',(approval['id'],)),
            publish_attempts=db.scalar('SELECT count(*) FROM publish_attempts'),
            notifications=[dict(r) for r in db.fetchall('SELECT kind,state,attempts FROM notifications ORDER BY id')])
        (out/'phone-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=True),flush=True)
    os._exit(exit_code)
def on_message(data):
    event = parse_message_event(data)
    observation = {'at':to_iso(clock.now()),'parsed':event is not None,
        'owner_matches':bool(event and event['sender_id']==gateway.owner_open_id),
        'chat_type':event['chat_type'] if event else None,
        'text_length':len(event['text']) if event else 0,
        'expected_code_present':bool(event and code in event['text']),
        'message_type':getattr(getattr(getattr(data,'event',None),'message',None),'message_type',None)}
    (out/'last-event-observation.json').write_text(json.dumps(observation,indent=2),encoding='utf-8')
    print('Inbound observation: '+json.dumps(observation),flush=True)
    if not event or event['sender_id'] != gateway.owner_open_id or event['chat_type'] not in {'p2p','private'}:
        return
    from bot.services.approvals import _parse_command
    action, parsed_code, _ = _parse_command(event['text'])
    if action not in {'approve','reject'} or parsed_code != code:
        return
    result = gateway.handle_event(data)
    report['outcomes'].append({'ok':result.get('ok'),'outcome':result.get('outcome')})
    received.set()
def deliver():
    try:
        if not connected.wait(60):
            finish('connection_not_ready',2)
        rows = deliver_pending(db,cfg,clock,notifier,limit=1)
        report['delivery'] = [{k:r[k] for k in ('ok','state','images_sent','images_total') if k in r} for r in rows]
        state = db.scalar("SELECT state FROM notifications WHERE kind='approval' ORDER BY id DESC LIMIT 1")
        if state != 'sent':
            finish('approval_delivery_incomplete',2)
        print('Full text and all images delivered; approval receiver is ready. Code: '+code,flush=True)
        while True:
            if not received.wait(1):
                continue
            received.clear()
            time.sleep(1)
            receipts=deliver_pending(db,cfg,clock,notifier,limit=10)
            report['receipt_delivery']=[{'ok':r.get('ok'),'state':r.get('state')} for r in receipts]
            status=db.scalar('SELECT status FROM approvals WHERE id=?',(approval['id'],))
            if status in {'approved','rejected'}:
                pending=db.scalar("SELECT count(*) FROM notifications WHERE kind='outcome' AND state!='sent'")
                finish('decision_received_receipt_sent' if pending==0 else 'decision_received_receipt_incomplete',0 if pending==0 else 2)
    except Exception as exc:
        finish('delivery_error_'+type(exc).__name__,2)
class Client(lark.ws.Client):
    async def _connect(self):
        await super()._connect()
        print('Feishu approval rehearsal connected.',flush=True)
        connected.set()
handler=lark.EventDispatcherHandler.builder('','').register_p2_im_message_receive_v1(on_message).build()
timer=threading.Timer(900,lambda:finish('timed_out_waiting_for_decision',2));timer.daemon=True;timer.start()
threading.Thread(target=deliver,daemon=True).start()
client=Client(cfg.secrets.lark_app_id,cfg.secrets.lark_app_secret,event_handler=handler,log_level=lark.LogLevel.ERROR)
try:
    client.start()
except Exception as exc:
    finish('connection_error_'+type(exc).__name__,2)

