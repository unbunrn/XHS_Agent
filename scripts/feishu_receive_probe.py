"""Bounded receive-only Feishu probe; never sends or approves anything."""
from pathlib import Path
import sys, json, threading, os, faulthandler
faulthandler.dump_traceback_later(20, repeat=False)
from datetime import datetime, timezone
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from bot.config import load_config
from bot.lark_gateway import parse_message_event
import lark_oapi as lark
cfg=load_config(root=ROOT)
out=ROOT/"var/live-verification/feishu-receive-test.json"
def finish(status,code):
    data={"at":datetime.now(timezone.utc).isoformat(),"status":status,"messages_sent":0,"published":False}
    out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(data,ensure_ascii=False),flush=True)
    os._exit(code)
def receive(data):
    event=parse_message_event(data)
    if event and event["sender_id"]==cfg.secrets.lark_owner_open_id and event["chat_type"] in {"p2p","private"}:
        finish("owner_private_message_received",0)
timer=threading.Timer(900,lambda:finish("timed_out_waiting_for_owner_message",2))
timer.daemon=True
timer.start()
handler=lark.EventDispatcherHandler.builder("","").register_p2_im_message_receive_v1(receive).build()
class ProbeClient(lark.ws.Client):
    async def _connect(self):
        await super()._connect()
        faulthandler.cancel_dump_traceback_later()
        print("Feishu WebSocket connected; waiting for owner private message.",flush=True)
client=ProbeClient(cfg.secrets.lark_app_id,cfg.secrets.lark_app_secret,event_handler=handler,log_level=lark.LogLevel.ERROR)
print("Receive-only listener starting; expires after 15 minutes. No outgoing messages.",flush=True)
try:
    client.start()
except Exception as exc:
    finish("connection_error_"+type(exc).__name__,1)
