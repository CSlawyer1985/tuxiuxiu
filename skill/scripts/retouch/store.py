from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .common import canonical_hash, encoded, save_json, verify_artifacts, check_executor, inside,artifact,renderer_signature

PROFILES = {'natural', 'clear', 'portrait', 'keep'}


class Store:
    def __init__(self, project):
        self.project = Path(project).resolve()
        self.directory = self.project / 'scratch/retouch'
        inside(self.project,'scratch/retouch/workflow.sqlite3',False)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'workflow.sqlite3'
        with self.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS decisions (request_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, response TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, body TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS outbox (event_id TEXT PRIMARY KEY, kind TEXT, body TEXT, status TEXT DEFAULT 'pending', receipt TEXT, created REAL);
            ''')

    @contextmanager
    def transaction(self):
        inside(self.project,'scratch/retouch/workflow.sqlite3',False)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=10000')
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN IMMEDIATE')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self, config):
        with self.transaction() as db:
            if db.execute('SELECT 1 FROM state').fetchone(): raise ValueError('活动任务已存在，直接继续即可')
            state = {'schema_version': 'retouch-workflow-1', 'run_id': uuid.uuid4().hex[:12],
                     'revision': 1, 'seq': 1, 'status': 'preparing', 'config': deepcopy(config),
                     'photos': [], 'samples': [], 'selection_artifacts': [], 'choice': None,
                     'sample_binding': None, 'sample_executor':check_executor(), 'created_at': time.time(), 'error': None}
            db.execute('INSERT INTO state VALUES (1,?)', (encoded(state).decode(),))
        return state

    @staticmethod
    def _state(db):
        row = db.execute('SELECT body FROM state WHERE id=1').fetchone()
        if not row: raise ValueError('活动尚未初始化')
        return json.loads(row[0])

    @staticmethod
    def _save(db, state):
        state['seq'] = state.get('seq', 0) + 1
        state['updated_at'] = time.time()
        db.execute('UPDATE state SET body=? WHERE id=1', (encoded(state).decode(),))

    def snapshot(self):
        with self.transaction() as db:
            state = self._state(db)
            state['events'] = [dict(row) for row in db.execute('SELECT * FROM events ORDER BY seq DESC LIMIT 30')]
            for event in state['events']: event['body'] = json.loads(event['body'])
            state['outbox'] = [dict(row) for row in db.execute('SELECT * FROM outbox ORDER BY created DESC LIMIT 20')]
            for event in state['outbox']:
                event['body'] = json.loads(event['body'])
                if event['receipt']: event['receipt'] = json.loads(event['receipt'])
            return state

    def mutate(self, callback):
        with self.transaction() as db:
            state = self._state(db)
            result = callback(state)
            self._save(db, state)
            return result if result is not None else deepcopy(state)

    @staticmethod
    def _event(db, kind, body, event_id=None, notify=False):
        db.execute('INSERT INTO events(kind,body,created) VALUES (?,?,?)', (kind, encoded(body).decode(), time.time()))
        if notify:
            db.execute('INSERT OR IGNORE INTO outbox(event_id,kind,body,created) VALUES (?,?,?,?)',
                       (event_id or uuid.uuid4().hex, kind, encoded(body).decode(), time.time()))

    def event(self, kind, body, event_id=None, notify=False):
        with self.transaction() as db:
            self._event(db, kind, body, event_id, notify)

    def outbox(self):
        with self.transaction() as db:
            return [dict(row) for row in db.execute('SELECT * FROM outbox ORDER BY created')]

    def receipt(self, event_id, status, detail):
        if status not in {'pending', 'sending', 'queued', 'observed', 'failed', 'delivery_unknown', 'disabled'}:
            raise ValueError('未知回传状态')
        with self.transaction() as db:
            db.execute('UPDATE outbox SET status=?,receipt=? WHERE event_id=?',
                       (status, encoded(detail).decode(), event_id))

    def decide(self, revision, binding, choice, request_id):
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', request_id):
            raise ValueError('无效的选择编号')
        if not isinstance(choice, dict) or choice.get('profile') not in PROFILES:
            raise ValueError('请选择本地修片方案')
        payload_hash = canonical_hash([revision, binding, choice])
        with self.transaction() as db:
            old = db.execute('SELECT * FROM decisions WHERE request_id=?', (request_id,)).fetchone()
            if old:
                if old['payload_hash'] != payload_hash: raise ValueError('重复选择编号对应不同内容')
                return json.loads(old['response'])
            state = self._state(db)
            if state['status'] not in {'needs_selection', 'needs_review'}:
                raise ValueError('此版本已确认或仍在准备，请刷新页面')
            check_executor(state.get('sample_executor'))
            if revision != state['revision'] or not binding or binding != state['sample_binding']:
                raise ValueError('样片版本已变化，请刷新后重新选择')
            pending=state.get('pending_request')
            if pending and pending['status'] not in {'awaiting_confirmation','approved'}:raise ValueError('修改要求尚未生成新样片，不能确认旧效果')
            ids = {photo['id'] for photo in state['photos']}
            allowed={'profile','watermark','watermark_option_id','overrides','skin_reviewed','note','note_intent','accept_warnings','approved_photo_ids'}
            if not set(choice)<=allowed:raise ValueError('选择包含未识别字段')
            eligible={photo['id'] for photo in state['photos'] if photo['status'] not in {'completed','excluded','error'}}
            approved_ids=choice.get('approved_photo_ids',list(eligible))
            if not isinstance(approved_ids,list) or not approved_ids or not set(approved_ids)<=ids:raise ValueError('应用范围与本批不符')
            if not set(approved_ids)<=eligible:raise ValueError('已交付或已跳过的照片不能直接重复确认，请先为目标照片生成新版本')
            if 'approved_photo_ids' in choice:
                for photo in state['photos']:
                    profile=choice.get('overrides',{}).get(photo['id'],choice['profile'])
                    if photo['id'] in approved_ids and not photo.get('candidates',{}).get(profile):
                        raise ValueError('当前照片尚未生成所选效果，请先为这张出样再确认')
            if not isinstance(choice.get('accept_warnings',[]),list) or not set(choice.get('accept_warnings',[]))<=ids:raise ValueError('高亮复核范围无效')
            if choice.get('watermark') and not state['config'].get('watermark'):raise ValueError('本活动没有可确认的水印资产')
            if choice.get('watermark') and state['config'].get('watermark_options'):
                if not state['config'].get('watermark_selection') or choice.get('watermark_option_id')!=state['config']['watermark_selection']:
                    raise ValueError('请先选择水印版本，核对更新后的样片')
            overrides = choice.get('overrides', {})
            if not isinstance(overrides, dict) or not set(overrides) <= ids or not set(overrides.values()) <= PROFILES:
                raise ValueError('逐图方案与本批照片不符')
            reviewed = choice.get('skin_reviewed', [])
            if not isinstance(reviewed, list) or not set(reviewed) <= ids:
                raise ValueError('选区核对记录无效')
            if type(choice.get('watermark', False)) is not bool or len(str(choice.get('note', ''))) > 2000:
                raise ValueError('水印或意见字段无效')
            if choice.get('note_intent','change_request') not in {'change_request','delivery_note'}:raise ValueError('请明确说明是修改要求还是交付备注')
            note=str(choice.get('note','')).strip()
            resolved_note=(pending and pending['status']=='awaiting_confirmation' and pending['text']==note and pending.get('sample_binding')==binding and bool(set(pending['photo_ids'])&set(approved_ids)))
            if note and choice.get('note_intent')!='delivery_note' and not resolved_note:raise ValueError('这段文字是修改要求，请先提交要求并更新样片，再确认效果')
            verify_artifacts(self.project, state['selection_artifacts'])
            record = {'request_id': request_id, 'revision': revision, 'sample_binding': binding,
                      'choice': choice, 'timestamp': time.time(), 'source': 'human_review_console'}
            if choice.get('watermark'):
                record['watermark_snapshot']=deepcopy(state['config']['watermark'])
                record['watermark_selection_ref']=state['config'].get('watermark_selection_ref')
            from . import engine,tuning
            record['parameter_snapshot']={}
            for photo in state['photos']:
                if photo['id'] not in approved_ids or not photo.get('analysis'):continue
                profile=overrides.get(photo['id'],choice['profile']);candidate=photo.get('candidates',{}).get(profile)
                adjustments=photo.get('tuning',{}).get(profile,{})
                from .runner import smoothing_recipe
                parameters=candidate['report']['recipe'] if candidate else smoothing_recipe(self.project,photo,engine.complete_recipe(profile,photo['analysis'],adjustments))[0]
                record['parameter_snapshot'][photo['id']]={'profile':profile,'adjustments':adjustments,'parameters':parameters,
                   'full_candidate_verified':bool(candidate),'recipe_artifact':candidate.get('recipe') if candidate else None}
            relative = f'input/activity-materials/网页选择_{request_id}.json'
            save_json(self.project / relative, record)
            snapshot_ref=artifact(self.project,self.project/relative,'user_confirmation')
            approval_reference={key:value for key,value in record.items() if key!='parameter_snapshot'}
            approval_reference['parameter_snapshot_ref']=snapshot_ref
            for photo in state['photos']:
                if photo['status'] not in {'completed','excluded','error'}:
                    if photo['id'] in approved_ids:
                        photo['approval']=deepcopy(approval_reference);photo['status']='queued';photo['error']=None
                    elif not photo.get('approval'):photo['status']='needs_review'
            analyzed=[p for p in state['photos'] if p.get('analysis') and p['id'] in approved_ids and p['id'] in state['samples'] and p['status']!='excluded']
            if analyzed:
                state['approval_scope']={'tags':sorted({tag for p in analyzed for tag in p['analysis']['tags']}),
                    'reference_min':min(p['analysis']['reference_p50'] for p in analyzed),
                    'reference_max':max(p['analysis']['reference_p50'] for p in analyzed),
                    'bright_max':max(p['analysis']['whole']['bright_fraction'] for p in analyzed),
                    'color_signatures':[p['analysis'].get('color_signature') for p in analyzed],
                    'face_color_sets':[p['analysis'].get('face_colors',[]) for p in analyzed]}
            state.update(status='running', choice=deepcopy(choice), approval=approval_reference, error=None,
                         scope_reuse='approved_photo_ids' not in choice)
            selected=[p for p in state['photos'] if p['id'] in approved_ids and p.get('analysis')]
            tuning_values=[p.get('tuning',{}).get(choice['profile'],{}) for p in selected]
            if tuning_values and all(value==tuning_values[0] for value in tuning_values):
                state['confirmed_tuning']={choice['profile']:deepcopy(tuning_values[0])}
            else:state.update(confirmed_tuning={},scope_reuse=False)
            state['confirmed_pipeline']={'choice':deepcopy(choice),'renderer_signature':renderer_signature(),
                'scope_reuse':state['scope_reuse'],'confirmation_ref':snapshot_ref,'timestamp':time.time()}
            if pending and pending['status']=='awaiting_confirmation':
                versions={p['id']:p.get('generation',1) for p in state['photos'] if p['id'] in pending['photo_ids']}
                accumulated={pid for pid in pending.get('approved_photo_ids',[]) if pending.get('target_generations',{}).get(pid)==versions.get(pid)}
                newly_approved={pid for pid in approved_ids if pid in pending['photo_ids'] and pending.get('target_generations',{}).get(pid)==versions.get(pid)}
                accumulated|=newly_approved
                pending['approved_photo_ids']=sorted(accumulated)
                pending.setdefault('approval_requests',{}).update({pid:request_id for pid in newly_approved})
                if set(pending['photo_ids'])<=accumulated:pending.update(status='approved',approval_request=request_id)
            self._save(db, state)
            response = {'accepted': True, 'request_id': request_id, 'revision': revision,
                        'decision_path': relative, 'status': 'running','parameter_snapshot_ref':snapshot_ref}
            db.execute('INSERT INTO decisions VALUES (?,?,?)', (request_id, payload_hash, encoded(response).decode()))
            self._event(db, 'decision', response, f'{state["run_id"]}:decision:{request_id}', notify=True)
            return response
