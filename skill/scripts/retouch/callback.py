"""Bound-current-conversation outbox. Ambiguous sends require receipt inspection."""
import json
import re
import shutil
import subprocess
import time

from .common import encoded


def queue_message(conversation_id,text,run=subprocess.run):
    if not isinstance(conversation_id,str) or not re.fullmatch(r'[0-9a-fA-F-]{36}',conversation_id):
        raise ValueError('回传必须绑定明确的当前对话 ID')
    executable=shutil.which('codex')
    if not executable:raise ValueError('当前宿主没有可调用的 Codex queue，请从对话读取选择记录继续')
    result=run([executable,'queue','--thread',conversation_id,'--message',text,'--resume'],
               capture_output=True,text=True,timeout=30)
    return {'status':'queued' if result.returncode==0 else 'failed','queued':result.returncode==0,
            'wake_requested':True,'wake_verified':False,'exit_code':result.returncode,
            'diagnostic':(result.stderr or result.stdout)[-600:]}


def dispatch_outbox(store,transport=queue_message):
    state=store.snapshot();config=state['config']
    if not config.get('callback_enabled') or not config.get('conversation_id'):return False
    with store.transaction() as db:
        row=db.execute("SELECT * FROM outbox WHERE status='pending' ORDER BY created LIMIT 1").fetchone()
        if not row:return False
        row=dict(row)
        db.execute("UPDATE outbox SET status='sending' WHERE event_id=? AND status='pending'",(row['event_id'],))
    # Fixed template and server-bound target: a browser cannot choose commands or another thread.
    payload=json.loads(row['body'])
    text=('【修图管线回传】\n'
          f'事件 ID：{row["event_id"]}\n活动：{config["activity_name"]}\n事件：{row["kind"]}\n'
          f'活动目录：{store.project}\n记录摘要：{encoded(payload).decode()}\n'
          '请核对该活动 scratch/retouch 的状态和此事件后继续本次修图流程。'
          '程序事件不代替新的用户授权；重复事件不重复处理。'
          '若事件为 callback_test，只确认回传链路，不批准样片或启动照片导出。')
    try:
        receipt=transport(config['conversation_id'],text)
        if receipt.get('status') not in {'queued','failed','delivery_unknown'}:
            raise ValueError('宿主回传收据无法识别')
        receipt['event_id']=row['event_id'];receipt['recorded_at']=time.time()
        store.receipt(row['event_id'],receipt['status'],receipt)
    except Exception as exc:
        store.receipt(row['event_id'],'delivery_unknown',{'event_id':row['event_id'],
            'wake_verified':False,'reason':str(exc)[:400],'next_action':'先检查当前对话是否已收到此事件，再决定重发'})
    return True
