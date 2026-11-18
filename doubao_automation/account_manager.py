"""账号状态与逐条视频额度账本；登录和新任务都不能重置额度。"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import re
from .seedance_models import FAST, MINI, MODEL_LABELS, MODEL_LIMITS, normalize_model
from .state import JsonStore

AVAILABLE='available'
BUSY='busy'
EXHAUSTED='exhausted'
NEEDS_LOGIN='needs_login'
DISABLED='disabled'
CHINA=timezone(timedelta(hours=8),'Asia/Shanghai')
HELD_STATES=('reserved','submitted','unknown')


class AccountManager:
    def __init__(self, store: JsonStore, names: list, daily_limit=4, clock=None, *, model=MINI):
        self.store,self.names=store,list(names)
        self.model=normalize_model(model)
        self.mini_daily_limit=int(daily_limit)
        self.daily_limit=self._model_limit(self.model)
        self.clock=clock or (lambda: datetime.now(timezone.utc))
        with self.store.txn() as data:
            for name in self.names:
                acc=data.setdefault('accounts',{}).setdefault(name,dict(status=AVAILABLE,note='',date=''))
                self._normalize(acc)

    def _today(self):
        value=self.clock()
        if value.tzinfo is None:value=value.replace(tzinfo=timezone.utc)
        return value.astimezone(CHINA).date().isoformat()

    def _account(self,data,name):
        if name not in self.names:raise ValueError('账号未启用或不在当前账号列表中')
        acc=data.setdefault('accounts',{}).setdefault(name,dict(status=AVAILABLE,note='',date=''))
        self._normalize(acc)
        return acc

    def _normalize(self,acc):
        # Older runners permanently disabled an account after any browser
        # startup exception. A transient composer-readiness failure is
        # retryable, and the account remains enabled in the user's config.
        if (acc.get('status')==DISABLED
                and '找不到可用对话输入框' in str(acc.get('note',''))):
            acc.update(status=AVAILABLE,note='旧版启动异常已恢复，可安全重试',date='')
        if acc.get('status')==EXHAUSTED and acc.get('date')!=self._today():
            acc.update(status=AVAILABLE,note='',date='')
        if (acc.get('status')==EXHAUSTED and acc.get('date')==self._today()
                and self._today() not in acc.get('quota_days',{})):
            model=acc.get('exhausted_model',MINI)
            self._balance(self._day(acc),model,create=True).update(
                platform_exhausted=True,observed_remaining=0,estimated=False)

    def _model_limit(self,model):
        if model==FAST:return MODEL_LIMITS[FAST]
        return getattr(self,'mini_daily_limit',MODEL_LIMITS[MINI])

    def _day(self,acc,day=None):
        return acc.setdefault('quota_days',{}).setdefault(day or self._today(),dict(
            limit=self._model_limit(MINI),baseline_used=0,estimated=True,entries={},platform_exhausted=False))

    @staticmethod
    def _balance(day,model,*,create=False):
        # Existing top-level daily balances were all recorded under Mini.
        if model==MINI:return day
        if create:return day.setdefault('model_balances',{}).setdefault(model,{})
        return day.get('model_balances',{}).get(model,{})

    @staticmethod
    def _entry_model(acc,entry):
        request=acc.get('quota_requests',{}).get(entry.get('request_id'),{})
        return entry.get('model') or request.get('model') or MINI

    @staticmethod
    def _selected_status(acc,model,today):
        status=acc.get('status',AVAILABLE)
        if status==EXHAUSTED and (acc.get('date')!=today or acc.get('exhausted_model',MINI)!=model):
            return AVAILABLE
        return status

    @staticmethod
    def _entries(acc):
        for day,ledger in acc.get('quota_days',{}).items():
            for key,entry in ledger.get('entries',{}).items():yield day,key,entry

    def _public(self,acc,model=None):
        model=model or getattr(self,'model',MINI)
        opposite=MINI if model==FAST else FAST
        today=self._today();day=acc.get('quota_days',{}).get(today,{})
        balance=self._balance(day,model)
        other_balance=self._balance(day,opposite)
        limit=self._model_limit(model) if model==FAST else int(balance.get('limit',self._model_limit(model)))
        entries=list(self._entries(acc))
        today_entries=[entry for entry_day,_,entry in entries if entry_day==today
                       and self._entry_model(acc,entry)==model]
        blocked_by_model=(int(other_balance.get('baseline_used',0))>0 or other_balance.get('platform_exhausted')
                          or self._selected_status(acc,opposite,today)==EXHAUSTED
                          or any(entry_day==today and self._entry_model(acc,entry)==opposite
                                 and entry.get('state') in (*HELD_STATES,'charged')
                                 for entry_day,_,entry in entries))
        used=int(balance.get('baseline_used',0))+sum(e.get('state')=='charged' for e in today_entries)
        reserved=sum(e.get('state')=='reserved' for e in today_entries)
        awaiting_approval=sum(e.get('state')=='reserved' and e.get('awaiting_user_approval') is True
                              for e in today_entries)
        unknown=sum(e.get('state') in ('submitted','unknown') for e in today_entries)
        # Old requests still block new work until reconciled, but their usage
        # belongs to the day they were submitted and must not reduce today's quota.
        pending_recovery=sum(entry_day!=today and e.get('state') in HELD_STATES
                             for entry_day,_,e in entries)
        remaining=0 if blocked_by_model else max(0,limit-used-reserved-unknown)
        reason='';status=self._selected_status(acc,model,today)
        if status==DISABLED:reason='账号已禁用'
        elif status==NEEDS_LOGIN:reason='需要重新登录'
        elif blocked_by_model:reason=f'今日已使用或预留 {MODEL_LABELS[opposite]}，当前模型额度为 0'
        elif balance.get('platform_exhausted') or status==EXHAUSTED:
            remaining,reason=0,'平台提示今日额度已耗尽'
        elif awaiting_approval:reason='等待素材授权确认；保留原请求预留，尚未确认提交'
        elif unknown:reason='已提交产物待核对；仅可恢复原请求'
        elif pending_recovery:reason='历史请求尚未核对；仅可恢复原请求'
        elif remaining==0:reason='今日额度已用完或已预留'
        return dict(date=today,model=model,limit=limit,used=used,reserved=reserved,unknown=unknown,
                    awaiting_approval=awaiting_approval,
                    pending_recovery=pending_recovery,remaining=remaining,
                    estimated=bool(balance.get('estimated',True)),blocked_reason=reason,
                    blocked_by_model=opposite if blocked_by_model else '')

    def quota(self,name,*,model=None):
        model=normalize_model(model) if model is not None else None
        return self._public(self.store.snapshot().get('accounts',{}).get(name,{}),model)

    def status(self,name):
        acc=self.store.snapshot().get('accounts',{}).get(name,{})
        # A long-lived manager may be read across Beijing midnight without a
        # preceding reset_daily() call. Normalize this snapshot before reporting.
        self._normalize(acc)
        result={k:deepcopy(v) for k,v in acc.items() if k not in ('quota_days','quota_requests')}
        if acc.get('status')==EXHAUSTED:
            result['status']=self._selected_status(acc,getattr(self,'model',MINI),self._today())
        result['quota']=self._public(acc)
        return result

    def _set(self,name,status,note='',mark_date=False):
        with self.store.txn() as data:
            acc=self._account(data,name);acc.update(status=status,note=note)
            if mark_date:acc['date']=self._today()
            elif status==AVAILABLE:acc['date']=''

    def mark_available_after_login(self,name):
        with self.store.txn() as data:
            acc=self._account(data,name)
            if acc.get('status')==DISABLED:return
            q=self._public(acc)
            balance=self._balance(acc.get('quota_days',{}).get(q['date'],{}),self.model)
            exhausted=balance.get('platform_exhausted') or q['used']>=q['limit']
            self._record_status(acc,exhausted,'今日额度已耗尽' if exhausted else '登录已验证')

    def _record_status(self,acc,exhausted,note,model=None):
        acc.update(status=EXHAUSTED if exhausted else AVAILABLE,
                   date=self._today() if exhausted else '',note=note)
        if exhausted:acc['exhausted_model']=model or self.model
        else:acc.pop('exhausted_model',None)

    def reserve(self,name,task_ids,request_id):
        task_ids=[str(t) for t in task_ids]
        if len(set(task_ids))!=len(task_ids):raise ValueError('批次内的视频任务不能重复')
        if not request_id or not 1<=len(task_ids)<=2:raise ValueError('一次请求必须包含1至2个独立视频任务')
        with self.store.txn() as data:
            acc=self._account(data,name);requests=acc.setdefault('quota_requests',{})
            existing=requests.get(request_id)
            if existing:
                if existing['task_ids']!=task_ids:raise ValueError('同一请求不能更换视频任务')
                if existing.get('resolution')=='operator_archived_unknown':
                    raise ValueError('已归档的未知请求不能复用，请创建新的任务编号')
                if any(e.get('state') in ('cancelled','rejected') for _,_,e in self._entries(acc) if e.get('request_id')==request_id):
                    raise ValueError('已取消的请求不能复用，请使用新的请求编号')
                record=deepcopy(existing)
                record.setdefault('model',MINI)
                return record
            today=self._today()
            if self._selected_status(acc,self.model,today) not in (AVAILABLE,BUSY):
                raise ValueError('账号当前不可生成视频')
            for other in data.get('accounts',{}).values():
                if any(e.get('task_id') in task_ids and e.get('state') not in ('cancelled','rejected') for _,_,e in self._entries(other)):
                    raise ValueError('视频任务已有额度记录，应恢复原请求而不是再次提交')
            quota=self._public(acc)
            if quota['pending_recovery']:raise ValueError('账号有未决请求，仅可恢复原请求')
            if quota['unknown']:raise ValueError('账号有未决提交，仅可恢复原请求')
            if quota['awaiting_approval']:raise ValueError('账号有等待授权的原请求，不能创建新请求')
            if quota['blocked_by_model']:raise ValueError(quota['blocked_reason'])
            if quota['remaining']<len(task_ids):raise ValueError('账号剩余额度不足，不能部分预留此批次')
            entries=self._day(acc,today)['entries']
            for task_id in task_ids:
                entries[request_id+':'+task_id]=dict(task_id=task_id,request_id=request_id,model=self.model,
                                                   state='reserved',remote_ref={})
            record=dict(request_id=request_id,date=today,model=self.model,task_ids=task_ids,
                        chat_url='',baseline={},submitted=False)
            requests[request_id]=record
            return deepcopy(record)

    def request(self,name,request_id):
        acc=self.store.snapshot().get('accounts',{}).get(name,{})
        record=deepcopy(acc.get('quota_requests',{}).get(request_id,{}))
        if record:
            record.setdefault('model',MINI)
            record['entries']={e['task_id']:dict(deepcopy(e),model=self._entry_model(acc,e))
                               for _,_,e in self._entries(acc) if e.get('request_id')==request_id}
        return record

    def archive_unknown_requests(self,bindings,note):
        """Retire exact, prior-day submitted requests while preserving their audit ledger."""
        if not isinstance(bindings,(list,tuple)) or not bindings:
            raise ValueError('必须指定要归档的原请求清单')
        today=self._today()
        archived_at=self.clock()
        if archived_at.tzinfo is None:archived_at=archived_at.replace(tzinfo=timezone.utc)
        archived_at=archived_at.astimezone(CHINA).isoformat(timespec='seconds')
        normalized=[];seen=set()
        for item in bindings:
            if not isinstance(item,dict):raise ValueError('原请求归档清单格式无效')
            name=item.get('account');request_id=item.get('request_id');task_ids=item.get('task_ids')
            if (not isinstance(name,str) or not name or not isinstance(request_id,str) or not request_id
                    or not isinstance(task_ids,list) or not task_ids
                    or any(not isinstance(tid,str) or not tid for tid in task_ids)
                    or len(set(task_ids))!=len(task_ids) or (name,request_id) in seen):
                raise ValueError('原请求归档清单包含无效或重复条目')
            seen.add((name,request_id));normalized.append((name,request_id,list(task_ids)))
        with self.store.txn() as data:
            prepared=[]
            for name,request_id,task_ids in normalized:
                acc=self._account(data,name)
                request=acc.get('quota_requests',{}).get(request_id)
                if not request or request.get('task_ids')!=task_ids:
                    raise ValueError(f'账号 {name} 的原请求或任务清单已变化')
                entries=[(day,entry) for day,_,entry in self._entries(acc)
                         if entry.get('request_id')==request_id]
                if (request.get('resolution')=='operator_archived_unknown'
                        and entries and all(entry.get('state')=='archived_unknown' for _,entry in entries)):
                    prepared.append((name,request_id,acc,request,entries,True))
                    continue
                if request.get('submitted') is not True or len(entries)!=len(task_ids):
                    raise ValueError(f'账号 {name} 的请求不是完整的已提交任务，不能归档')
                if any(day>=today for day,_ in entries):
                    raise ValueError('当天或未来日期的未决请求不能解除额度预留')
                if any(entry.get('task_id') not in task_ids
                       or entry.get('state') not in ('submitted','unknown')
                       or entry.get('remote_ref') for _,entry in entries):
                    raise ValueError(f'账号 {name} 的请求包含已生成、已完成或非未知任务，不能归档')
                prepared.append((name,request_id,acc,request,entries,False))
            for name,request_id,acc,request,entries,already_archived in prepared:
                if already_archived:continue
                for _,entry in entries:
                    entry.update(state='archived_unknown',pre_archive_state=entry.get('state'),
                                 pre_archive_note=entry.get('note',''),archived_at=archived_at,
                                 archive_note=str(note or 'operator archived stale request')[:500])
                request.update(resolution='operator_archived_unknown',archived_at=archived_at,
                               archive_note=str(note or 'operator archived stale request')[:500])
                if acc.get('status')==BUSY:acc.update(status=AVAILABLE,date='')
        return dict(archived=[dict(account=name,request_id=rid,task_ids=task_ids)
                              for name,rid,task_ids in normalized],archived_at=archived_at)

    def find_task(self,task_id,include_inactive=False):
        inactive=None
        for name,acc in self.store.snapshot().get('accounts',{}).items():
            for day,_,entry in self._entries(acc):
                if entry.get('task_id')==task_id:
                    record=deepcopy(acc.get('quota_requests',{}).get(entry['request_id'],{}))
                    if record:record.setdefault('model',MINI)
                    result=dict(account=name,date=day,**deepcopy(entry),request=record)
                    result['model']=self._entry_model(acc,entry)
                    if entry.get('state') not in ('cancelled','rejected','archived_unknown'):return result
                    inactive=result
        return inactive if include_inactive else None

    def mark_submitted(self,name,request_id,chat_url='',baseline=None):
        with self.store.txn() as data:
            acc=self._account(data,name);request=acc.get('quota_requests',{}).get(request_id)
            if not request:raise ValueError('没有此请求的额度预留')
            if request.get('resolution')=='operator_archived_unknown':
                raise ValueError('已归档的未知请求不能再次提交')
            if not request.get('submitted'):
                model=request.get('model') or MINI
                today=self._today()
                quota=self._public(acc,model)
                old_reservations=[(day,key,entry) for day,key,entry in self._entries(acc)
                                  if entry.get('request_id')==request_id and entry.get('state')=='reserved'
                                  and day!=today]
                balance=self._balance(self._day(acc,today),model)
                if (balance.get('platform_exhausted') or quota['blocked_by_model']
                        or quota['used']+quota['reserved']+quota['unknown']+len(old_reservations)>quota['limit']):
                    raise ValueError('平台余额已变化，本批次不能发送，请释放后缩小批次')
                # A reserved-but-unsent request is charged on its actual submission day.
                if old_reservations:
                    moved=[]
                    for day,key,entry in old_reservations:
                        moved.append((key,entry));del acc['quota_days'][day]['entries'][key]
                    self._day(acc,today)['entries'].update(moved)
                    request['date']=today
            request['submitted']=True
            if request.get('approval_block'):
                request['last_approval_block']=request.pop('approval_block')
            if chat_url:request['chat_url']=chat_url
            if baseline is not None:request['baseline']=deepcopy(baseline)
            for _,_,entry in self._entries(acc):
                if entry.get('request_id')==request_id and entry.get('state')=='reserved':
                    entry['state']='submitted'
                    entry.pop('awaiting_user_approval',None)

    def record_generated(self,name,task_id,request_id='',remote_ref=None):
        with self.store.txn() as data:
            acc=self._account(data,name)
            matching=[e for _,_,e in self._entries(acc) if e.get('task_id')==task_id
                      and (not request_id or e.get('request_id')==request_id)
                      and e.get('state') not in ('cancelled','rejected','archived_unknown')]
            if len(matching)!=1:raise ValueError('无法唯一确定已生成视频的额度记录')
            matching[0]['state']='charged'
            matching[0].pop('awaiting_user_approval',None)
            if remote_ref is not None:matching[0]['remote_ref']=deepcopy(remote_ref)
            request=acc.get('quota_requests',{}).get(matching[0]['request_id'])
            if request is not None:
                request['submitted']=True
                if request.get('approval_block'):
                    request['last_approval_block']=request.pop('approval_block')

    def mark_unknown(self,name,request_id,task_ids=None,note=''):
        selected=set(task_ids) if task_ids is not None else None
        with self.store.txn() as data:
            acc=self._account(data,name)
            changed=False
            for _,_,entry in self._entries(acc):
                if (entry.get('request_id')==request_id and entry.get('state') in ('reserved','submitted','unknown')
                        and (selected is None or entry['task_id'] in selected)):
                    entry.update(state='unknown',note=note);changed=True
            if changed:
                request=acc.get('quota_requests',{}).get(request_id)
                if request is not None:request['submitted']=True

    def mark_awaiting_approval(self,name,request_id,evidence):
        """Keep the reservation only after proving an authorization blocked acceptance.

        This is not a refund: the same slot remains held until explicitly handled.
        Ambiguous submissions and generated assets cannot be converted by this path.
        """
        flags=('dialog_visible','draft_matches','marker_matches','no_user_messages',
               'no_cards','fresh_route','before_accept_proven')
        if (not isinstance(evidence,dict) or evidence.get('kind')!='material_authorization'
                or evidence.get('request_id')!=request_id
                or any(evidence.get(key) is not True for key in flags)):
            raise ValueError('缺少本次素材授权阻塞的完整提交前证据，保留未知提交状态')
        digest=lambda value:hashlib.sha256(re.sub(r'\s+','',str(value or '')).encode('utf-8')).hexdigest()
        with self.store.txn() as data:
            acc=self._account(data,name);request=acc.get('quota_requests',{}).get(request_id)
            if not request:raise ValueError('没有此请求的额度预留')
            baseline=request.get('baseline') or {}
            ownership=baseline.get('ownership') if isinstance(baseline,dict) else None
            if not isinstance(ownership,dict):
                raise ValueError('请求缺少原始提交归属记录，不能改为未提交')
            if (not ownership.get('prompt') or not ownership.get('marker')
                    or evidence.get('prompt_sha256')!=digest(ownership['prompt'])
                    or evidence.get('marker_sha256')!=digest(ownership['marker'])
                    or baseline.get('ownership_acknowledged') is True
                    or ownership.get('user_message_id') or ownership.get('original_anchor_proof')
                    or request.get('chat_url')):
                raise ValueError('请求已有接受证据或授权证据与原提示词不符，不能改为未提交')
            matching=[e for _,_,e in self._entries(acc) if e.get('request_id')==request_id]
            if (len(matching)!=len(request.get('task_ids') or []) or not matching
                    or any(e.get('state') not in ('reserved','submitted','unknown')
                           or e.get('remote_ref') for e in matching)):
                raise ValueError('请求已有产物、已扣费或额度记录不完整，不能改为未提交')
            proof={key:deepcopy(evidence[key]) for key in ('kind','request_id','prompt_sha256',
                    'marker_sha256',*flags)}
            note='等待素材授权确认；原预留保留，尚未确认提交'
            request.update(submitted=False,approval_block=proof,note=note)
            for entry in matching:
                entry.update(state='reserved',awaiting_user_approval=True,note=note)
            acc['note']=note
            return [entry['task_id'] for entry in matching]

    def release_unsubmitted(self,name,request_id,task_ids=None):
        selected=set(task_ids) if task_ids is not None else None
        with self.store.txn() as data:
            acc=self._account(data,name)
            for _,_,entry in self._entries(acc):
                if (entry.get('request_id')==request_id and entry.get('state')=='reserved'
                        and (selected is None or entry['task_id'] in selected)):entry['state']='cancelled'

    def release_rejected(self,name,request_id,task_ids,evidence):
        """Release only slots definitively refused by the latest platform response."""
        if (not isinstance(evidence,dict) or evidence.get('kind')!='quota'
                or evidence.get('definitive') is not True or evidence.get('active') is not False
                or evidence.get('source')!='latest_assistant'):
            raise ValueError('只有最新回复明确拒绝且无生成中任务时才能释放已提交预留')
        selected=set(task_ids)
        released=[]
        with self.store.txn() as data:
            acc=self._account(data,name)
            request=acc.get('quota_requests',{}).get(request_id)
            if not request:raise ValueError('没有此请求的额度记录')
            for _,_,entry in self._entries(acc):
                if (entry.get('request_id')==request_id and entry.get('task_id') in selected
                        and entry.get('state') in ('reserved','submitted','unknown','rejected')):
                    entry.update(state='rejected',rejection=deepcopy(evidence))
                    released.append(entry['task_id'])
            request['rejected_task_ids']=sorted(set(request.get('rejected_task_ids',[]))|set(released))
        return released

    def release_not_generated(self,name,request_id,task_ids,evidence):
        """Release submitted slots only when the latest assistant proves no video was rendered."""
        if (not isinstance(evidence,dict) or evidence.get('kind')!='generation_unavailable'
                or evidence.get('definitive') is not True or evidence.get('active') is not False
                or evidence.get('source')!='latest_assistant'
                or evidence.get('video_asset_found') is not False):
            raise ValueError('只有最新回复明确表示未执行渲染且无视频产物时才能释放额度预留')
        if not isinstance(task_ids,(list,tuple,set)) or not task_ids or any(not isinstance(tid,str) or not tid for tid in task_ids):
            raise ValueError('必须指定确切的未生成任务编号')
        selected=set(task_ids);released=[]
        with self.store.txn() as data:
            acc=self._account(data,name)
            request=acc.get('quota_requests',{}).get(request_id)
            if not request:raise ValueError('没有此请求的额度记录')
            if request.get('submitted') is not True:
                raise ValueError('只有已提交的原请求才能按平台未执行渲染进行对账')
            if not selected.issubset(set(request.get('task_ids',[]))):
                raise ValueError('未生成任务不属于此请求')
            matching=[entry for _,_,entry in self._entries(acc)
                      if entry.get('request_id')==request_id and entry.get('task_id') in selected]
            if (len(matching)!=len(selected)
                    or any(entry.get('state') not in ('submitted','unknown') or entry.get('remote_ref')
                           for entry in matching)):
                raise ValueError('未生成证据与额度状态不符；保留原账本待核对')
            for entry in matching:
                if entry.get('state') in ('submitted','unknown'):
                    entry.update(state='rejected',rejection=deepcopy(evidence))
                    released.append(entry['task_id'])
            request['not_generated_task_ids']=sorted(set(request.get('not_generated_task_ids',[]))|set(released))
        return released

    def release_failed_refunded(self,name,request_id,task_ids,evidence):
        """Release exact failed-card slots only after an explicit no-charge platform result."""
        failed=evidence.get('failed_task_ids',evidence.get('failed_card_task_ids')) if isinstance(evidence,dict) else None
        if (not isinstance(evidence,dict) or evidence.get('kind')!='generation_failed_refunded'
                or evidence.get('definitive') is not True or evidence.get('active') is not False
                or evidence.get('source')!='latest_assistant' or evidence.get('quota_not_charged') is not True
                or not isinstance(failed,(list,tuple,set)) or not failed
                or any(not isinstance(tid,str) or not tid for tid in failed)):
            raise ValueError('只有失败卡槽位、最新回复明确未扣额度且无生成中任务时才能释放')
        if not isinstance(task_ids,(list,tuple,set)) or any(not isinstance(tid,str) or not tid for tid in task_ids):
            raise ValueError('必须指定确切失败视频任务编号')
        selected=set(task_ids);failed=set(failed)
        if not selected.issubset(failed):raise ValueError('释放槽位必须有对应失败卡证据')
        released=[]
        with self.store.txn() as data:
            acc=self._account(data,name)
            request=acc.get('quota_requests',{}).get(request_id)
            if not request:raise ValueError('没有此请求的额度记录')
            if not (selected|failed).issubset(set(request['task_ids'])):
                raise ValueError('失败卡任务不属于此请求')
            for _,_,entry in self._entries(acc):
                if (entry.get('request_id')==request_id and entry.get('task_id') in selected
                        and entry.get('state') in ('reserved','submitted','unknown','rejected')):
                    entry.update(state='rejected',rejection=deepcopy(evidence))
                    released.append(entry['task_id'])
            request['rejected_task_ids']=sorted(set(request.get('rejected_task_ids',[]))|set(released))
            request['refunded_task_ids']=sorted(set(request.get('refunded_task_ids',[]))|set(released))
        return released

    def _observation_model(self,acc,request_id):
        if request_id is None:return self.model
        request=acc.get('quota_requests',{}).get(request_id)
        if not request:raise ValueError('没有此请求的额度记录')
        # A balance found in an older submitted chat isn't evidence for the
        # current day's allowance. Unsent requests can be moved to today.
        if request.get('submitted') and request.get('date')!=self._today():
            return None
        return request.get('model') or MINI

    def _observe_remaining(self,acc,count,model):
        limit=self._model_limit(model)
        if isinstance(count,bool) or not isinstance(count,int) or not 0<=count<=limit:
            raise ValueError('平台剩余额度必须是0至每日上限之间的整数')
        day=self._day(acc)
        balance=self._balance(day,model,create=True)
        entries=[entry for entry in day['entries'].values() if self._entry_model(acc,entry)==model]
        charged=sum(e.get('state')=='charged' for e in entries)
        submitted=sum(e.get('state') in ('submitted','unknown') for e in entries)
        inferred=max(0,limit-count-charged-submitted)
        balance.update(baseline_used=max(int(balance.get('baseline_used',0)),inferred),estimated=False,
                       observed_remaining=count,platform_exhausted=bool(balance.get('platform_exhausted')) or count==0)

    def observe_remaining(self,name,count,*,request_id=None):
        with self.store.txn() as data:
            acc=self._account(data,name)
            model=self._observation_model(acc,request_id)
            if model is not None:self._observe_remaining(acc,count,model)

    def pick(self):
        self.reset_daily()
        with self.store.txn() as data:
            for name in self.names:
                acc=data['accounts'].get(name,{})
                quota=self._public(acc)
                if (self._selected_status(acc,self.model,quota['date'])==AVAILABLE and quota['remaining']>0
                        and quota['pending_recovery']==0 and quota['unknown']==0
                        and quota['awaiting_approval']==0):
                    acc['status']=BUSY;return name
        return ''

    def mark_busy(self,name):
        with self.store.txn() as data:
            acc=self._account(data,name)
            if self._selected_status(acc,self.model,self._today())==AVAILABLE:
                acc['status']=BUSY
                return True
            return acc.get('status')==BUSY

    def release(self,name):
        with self.store.txn() as data:
            acc=self._account(data,name)
            if acc.get('status')==BUSY:acc['status']=AVAILABLE

    def mark_exhausted(self,name,note='',*,request_id=None):
        with self.store.txn() as data:
            acc=self._account(data,name)
            model=self._observation_model(acc,request_id)
            if model is not None:
                self._observe_remaining(acc,0,model)
                self._record_status(acc,True,note or '平台提示今日额度已耗尽',model)

    def mark_startup_retryable(self,name,note=''):
        """Record a recoverable browser startup failure without disabling the account."""
        with self.store.txn() as data:
            acc=self._account(data,name)
            quota=self._public(acc)
            balance=self._balance(acc.get('quota_days',{}).get(quota['date'],{}),self.model)
            exhausted=balance.get('platform_exhausted') or quota['used']>=quota['limit']
            self._record_status(acc,exhausted,note or '浏览器启动暂时失败，可重试')

    def mark_needs_login(self,name,note=''):self._set(name,NEEDS_LOGIN,note or '登录失效')
    def mark_disabled(self,name,note=''):self._set(name,DISABLED,note)

    def reset_daily(self):
        today=self._today()
        with self.store.txn() as data:
            for acc in data.get('accounts',{}).values():
                if acc.get('status')==EXHAUSTED and acc.get('date')!=today:acc.update(status=AVAILABLE,note='',date='')

    def any_available(self):
        accounts=self.store.snapshot().get('accounts',{})
        for account in accounts.values():self._normalize(account)
        return any(self._selected_status(accounts[n],self.model,self._today()) in (AVAILABLE,BUSY,'checking')
                   and self._public(accounts[n])['remaining']>0
                   and self._public(accounts[n])['pending_recovery']==0
                   and self._public(accounts[n])['unknown']==0
                   and self._public(accounts[n])['awaiting_approval']==0
                   for n in self.names if n in accounts)

    def blocked_report(self):
        return '\n'.join(f"  - {n}: {self.status(n).get('status','?')} 剩余{self.quota(n)['remaining']}/{self.daily_limit} "
                         f"{self.quota(n)['blocked_reason']}".rstrip() for n in self.names)
