"""Dashboard launches are mocked; all journals, logs and runner locks are temporary."""
import copy
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import dashboard
from doubao_automation.orchestrator import Pipeline
from doubao_automation.state import JsonStore
from doubao_automation.doubao_batch import BatchGenerationMixin


class RunResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='doubao-tests-')
        self.directory=Path(self.tmp.name)
        self.logs=self.directory/'logs';self.logs.mkdir()
        self.videos=self.directory/'videos';self.videos.mkdir()
        self.cfg={'accounts':{'doubao':[dict(name='fixture',profile='profiles/fixture')]}}
        self.child=Mock(pid=321)
        self.child.poll.return_value=None
        self.patches=[patch.object(dashboard,'STATE_DIR',self.directory),
                      patch.object(dashboard,'LOGS_DIR',self.logs),
                      patch.object(dashboard,'SCRIPTS_DIR',self.directory/'scripts'),
                      patch.object(dashboard,'VIDEOS_DIR',self.videos),
                      patch.object(dashboard,'proc',None),patch.object(dashboard,'pending_accounts',{}),
                      patch.object(dashboard.threading,'Thread'),
                      patch.object(dashboard.subprocess,'Popen',return_value=self.child),
                      patch.object(dashboard.postprocess,'busy',return_value=False),
                      patch.object(dashboard.postprocess,'STORE_PATH',self.directory/'postprocess.json'),
                      patch.object(dashboard,'load_config',return_value=self.cfg)]
        self.mocks=[item.start() for item in self.patches]
        self.popen=self.mocks[7]
        self.client=dashboard.app.test_client()
        self.client.environ_base.update({'HTTP_ORIGIN':'http://localhost','HTTP_X_CSRF_TOKEN':dashboard.CSRF_TOKEN})
        self.state=dashboard.store()
        self.log=self.logs/'original.log';self.log.write_text('original log\n',encoding='utf-8')
        self.original={
            'run_id':'original-run','run_log':str(self.log),'run_returncode':1,'requested_script_count':2,
            'sources':[dict(record_id='fixture',keyword='product',prompt_text='saved prompt',videos=[])],
            'products':[dict(keyword='product',images=['fixture.png'])],
            'scripts':[dict(id='s1',text='original script'),dict(id='s2',text='another script')],
            'tasks':[dict(id='s1#0',script_id='s1',status='done',request_id='accepted'),
                     dict(id='s1#1',script_id='s1',status='awaiting_result',request_id='accepted')],
            'requests':{'accepted':dict(phase='unknown',submitted=True,task_ids=['s1#0','s1#1'])},
            'assemblies':{}}
        self.state.reset(self.original)
        self.ledger=JsonStore(self.directory/'accounts.json')
        self.ledger.reset({'accounts':{'fixture':{'status':'available','quota_days':{'fixture-day':{'entries':{'accepted:s1#0':{'state':'charged'}}}}}}})

    def tearDown(self):
        for item in reversed(self.patches):item.stop()
        self.tmp.cleanup()

    def post(self,count=9,**payload):
        return self.client.post('/api/run',json={'task_count':count,**payload})

    def seed_ledger_recovery(self):
        request_ids=[
            'request_0dc0b8e4cafd4bb19a8176dec09518f9',
            'request_092c9aa19815408fa2813e2c3e595aef',
            'request_ef065a56991d45cb9136dd1688c74e45',
        ]
        tasks=[
            ('doubao2',request_ids[0],'r20261109_214857_952c_gc6cba47e7a_s0_1_pe39191f52b#seg0','38452883294829314'),
            ('doubao1',request_ids[1],'r20261109_214857_952c_gc6cba47e7a_s0_2_pe39191f52b#seg0','38452758495439106'),
            ('doubao3',request_ids[2],'r20261109_214857_952c_gc6cba47e7a_s0_3_pe39191f52b#seg0','38452758472218882'),
        ]
        accounts={}
        for name,rid,task_id,chat_id in tasks:
            chat_path='/chat/'+chat_id
            ownership={'prompt':'【独立视频1/1：本条内容长15秒】\n00:00-00:15','marker':f'【自动化任务标记：{rid}】','chat_path':chat_path}
            ownership['original_anchor_proof']=BatchGenerationMixin._original_anchor_proof(ownership,chat_path)
            baseline={'scope_version':1,'ownership_acknowledged':True,'ownership':ownership,
                      'controls':[{'phase':'operator_approved_resume','task_ids':[task_id]}]}
            accounts[name]={'status':'available','quota_requests':{rid:{'request_id':rid,'task_ids':[task_id],
                'submitted':True,'chat_url':'https://www.doubao.com'+chat_path,'baseline':baseline}},
                'quota_days':{'2026-11-10':{'entries':{rid+':'+task_id:{'request_id':rid,'task_id':task_id,'state':'unknown'}}}}}
        self.ledger.reset({'accounts':accounts})
        self.cfg['accounts']['doubao']=[dict(name=name,profile='profiles/'+name,enabled=True)
                                        for name,_,_,_ in tasks]
        original_run='r20261109_214857_952c'
        self.log=self.logs/(original_run+'.log');self.log.write_text('original request log\n',encoding='utf-8')
        self.state.reset({'run_id':'r20261111_020656_3dbe','run_log':str(self.logs/'r20261111_020656_3dbe.log'),
                          'run_returncode':-15,'sources':[dict(record_id='saved')],'scripts':[],'tasks':[],
                          'requests':{},'assemblies':{}})
        return request_ids

    def test_missing_or_disabled_accounts_block_launch_without_mutating_saved_work(self):
        for accounts in ([], [dict(name='fixture',profile='profiles/fixture',enabled=False)]):
            for resumed in (False,True):
                with self.subTest(accounts=accounts,resumed=resumed):
                    self.cfg['accounts']['doubao']=accounts
                    data=copy.deepcopy(self.original)
                    if not resumed:data.update(scripts=[],tasks=[],requests={},assemblies={})
                    self.state.reset(data)
                    ledger=self.ledger.snapshot()
                    response=self.post()
                    self.assertEqual(response.status_code,400)
                    self.assertIn('账号库',response.json['error'])
                    self.assertEqual(self.state.snapshot(),data)
                    self.assertEqual(self.ledger.snapshot(),ledger)
                    self.assertEqual(self.log.read_text(encoding='utf-8'),'original log\n')
                    self.popen.assert_not_called()

    def test_missing_or_disabled_accounts_block_launch_without_mutating_saved_work(self):
        for accounts in ([], [dict(name='fixture',profile='profiles/fixture',enabled=False)]):
            for resumed in (False,True):
                with self.subTest(accounts=accounts,resumed=resumed):
                    self.cfg['accounts']['doubao']=accounts
                    data=copy.deepcopy(self.original)
                    if not resumed:data.update(scripts=[],tasks=[],requests={},assemblies={})
                    self.state.reset(data)
                    ledger=self.ledger.snapshot()
                    response=self.post()
                    self.assertEqual(response.status_code,400)
                    self.assertIn('账号库',response.json['error'])
                    self.assertEqual(self.state.snapshot(),data)
                    self.assertEqual(self.ledger.snapshot(),ledger)
                    self.assertEqual(self.log.read_text(encoding='utf-8'),'original log\n')
                    self.popen.assert_not_called()

    def test_incomplete_plan_preserves_journal_count_ledger_and_appends_log(self):
        before_ledger=self.ledger.snapshot()
        response=self.post()
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json['resumed'])
        self.assertEqual(response.json['script_count'],2)
        self.assertIn('沿用原计划',response.json['message'])
        self.assertEqual(self.popen.call_args.args[0],
                         [sys.executable,'-u','run_pipeline.py','run','--wait-for-quota-reset'])
        actual=self.state.snapshot()
        for key in ('run_id','scripts','tasks','requests','assemblies','requested_script_count'):
            self.assertEqual(actual[key],self.original[key])
        self.assertEqual(self.ledger.snapshot(),before_ledger)
        self.assertEqual(actual['run_log'],str(self.log))
        self.assertIsNone(actual['run_returncode'])
        self.assertTrue(self.log.read_text(encoding='utf-8').startswith('original log\n'))

    def test_all_non_done_task_states_resume_without_force(self):
        for status in ('pending','reserved','running','failed','needs_user','downloaded','local_failed'):
            with self.subTest(status=status):
                dashboard.proc=None
                data=copy.deepcopy(self.original);data['tasks'][1]['status']=status
                self.state.reset(data)
                response=self.post(1)
                self.assertEqual(response.status_code,200)
                self.assertTrue(response.json['resumed'])
                self.assertEqual(self.popen.call_args.args[0][3],'run')
                self.assertIn('--wait-for-quota-reset',self.popen.call_args.args[0])
                self.assertEqual(self.state.snapshot()['tasks'],data['tasks'])

    def test_missing_failed_or_incomplete_assembly_resumes_finished_segments(self):
        for assembly in (None,{'status':'failed'},{'status':'running'},{'status':'done','output':str(self.videos/'missing.mp4')}):
            with self.subTest(assembly=assembly):
                dashboard.proc=None
                data=copy.deepcopy(self.original)
                for task in data['tasks']:task['status']='done'
                data['assemblies']={} if assembly is None else {'s1':assembly}
                self.state.reset(data)
                response=self.post()
                self.assertEqual(response.status_code,200)
                self.assertTrue(response.json['resumed'])
                self.assertEqual(self.state.snapshot()['tasks'],data['tasks'])
                self.assertEqual(self.state.snapshot()['assemblies'],data['assemblies'])

    def test_complete_plan_or_no_plan_starts_new_generation(self):
        final=self.videos/'s1_final.mp4';final.write_bytes(b'owned fixture')
        for no_plan in (False,True):
            with self.subTest(no_plan=no_plan):
                dashboard.proc=None
                data=copy.deepcopy(self.original)
                for task in data['tasks']:task['status']='done'
                data['assemblies']={'s1':dict(status='done',output=str(final))}
                if no_plan:data.update(tasks=[],scripts=[],assemblies={},requests={})
                self.state.reset(data)
                response=self.post(3)
                self.assertEqual(response.status_code,200)
                self.assertFalse(response.json['resumed'])
                self.assertIsNone(response.json['script_count'])
                command=self.popen.call_args.args[0]
                self.assertIn('all',command);self.assertIn('--force',command);self.assertIn('--from-saved',command)
                self.assertNotIn('--limit',command)
                actual=self.state.snapshot()
                self.assertNotEqual(actual['run_id'],data['run_id'])
                self.assertEqual(actual['requested_script_count'],data['requested_script_count'])
                for key in ('scripts','tasks'):self.assertEqual(actual[key],[])
                self.assertEqual(actual['requests'],{})
                self.assertEqual(actual['assemblies'],{})
                self.assertEqual(self.log.read_text(encoding='utf-8'),'original log\n')

    def test_resume_does_not_require_sources_for_a_new_gemini_plan(self):
        data=copy.deepcopy(self.original);data.update(sources=[],products=[])
        self.state.reset(data)
        self.assertEqual(self.post().status_code,200)
        self.assertEqual(self.popen.call_args.args[0][3],'run')
        self.assertIn('--wait-for-quota-reset',self.popen.call_args.args[0])

    def test_state_exposes_small_resume_summary_without_scripts_or_requests(self):
        response=self.client.get('/api/state')
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json['can_resume'])
        self.assertEqual(response.json['resume_script_count'],2)
        self.assertEqual(response.json['resume_run_id'],'original-run')
        self.assertNotIn('scripts',response.json);self.assertNotIn('requests',response.json)

    def test_empty_journal_exposes_only_proven_ledger_requests_as_recovery_plan(self):
        request_ids=self.seed_ledger_recovery()
        response=self.client.get('/api/state')
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json['can_resume'])
        self.assertEqual(response.json['recovery_request_ids'],request_ids)
        self.assertEqual(response.json['resume_script_count'],3)
        self.assertEqual(response.json['resume_run_id'],'r20261109_214857_952c')

    def test_approved_ledger_recovery_runs_only_selected_original_requests(self):
        request_ids=self.seed_ledger_recovery()
        state_before=self.state.snapshot();ledger_before=self.ledger.snapshot()
        response=self.post(recovery_request_ids=request_ids)
        self.assertEqual(response.status_code,200,response.json)
        self.assertTrue(response.json['resumed'])
        self.assertEqual(response.json['script_count'],3)
        command=self.popen.call_args.args[0]
        self.assertEqual(command[:4],[sys.executable,'-u','run_pipeline.py','run'])
        self.assertEqual([command[i+1] for i,v in enumerate(command[:-1]) if v=='--recover-request-id'],request_ids)
        self.assertNotIn('--force',command)
        actual=self.state.snapshot()
        for key in ('scripts','tasks','requests','assemblies','sources'):
            self.assertEqual(actual.get(key),state_before.get(key))
        self.assertEqual(actual['run_id'],'r20261109_214857_952c')
        self.assertEqual(actual['run_log'],str(self.log))
        self.assertEqual(self.ledger.snapshot(),ledger_before)
        self.assertTrue(self.log.read_text(encoding='utf-8').startswith('original request log\n'))

    def test_ledger_recovery_rejects_missing_or_tampered_selection_without_writes(self):
        request_ids=self.seed_ledger_recovery()
        state_before=self.state.snapshot();ledger_before=self.ledger.snapshot()
        response=self.post(recovery_request_ids=request_ids[:2])
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.state.snapshot(),state_before)
        self.assertEqual(self.ledger.snapshot(),ledger_before)
        rid=request_ids[0]
        with self.ledger.txn() as data:
            saved=data['accounts']['doubao2']['quota_requests'][rid]
            saved['baseline']['ownership']['marker']='tampered'
        ledger_before=self.ledger.snapshot()
        response=self.client.get('/api/state')
        self.assertTrue(response.json['can_resume'])
        self.assertEqual(response.json['recovery_request_ids'],[])
        self.assertTrue(response.json['recovery_error'])
        response=self.post(recovery_request_ids=request_ids)
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.state.snapshot(),state_before)
        self.assertEqual(self.ledger.snapshot(),ledger_before)
        self.popen.assert_not_called()

    def test_live_runner_guard_blocks_reset_even_after_idle_probe_race(self):
        owner=Pipeline.__new__(Pipeline);owner.store=self.state
        before=self.state.snapshot()
        with owner._run_lock(),patch.object(dashboard,'running',return_value=False):
            response=self.post()
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.state.snapshot(),before)
        self.popen.assert_not_called()
        self.assertEqual(self.log.read_text(encoding='utf-8'),'original log\n')

    def test_runner_lock_is_released_before_mock_child_starts(self):
        def spawn(*args,**kwargs):
            child=Pipeline.__new__(Pipeline);child.store=self.state
            with child._run_lock():pass
            return self.child
        self.popen.side_effect=spawn
        self.assertEqual(self.post().status_code,200)

    def test_second_click_is_rejected_without_mutation(self):
        self.assertEqual(self.post().status_code,200)
        before=self.state.snapshot();log=self.log.read_text(encoding='utf-8')
        self.assertEqual(self.post().status_code,409)
        self.assertEqual(self.state.snapshot(),before)
        self.assertEqual(self.log.read_text(encoding='utf-8'),log)
        self.popen.assert_called_once()

    def test_cli_acquiring_lock_after_prepare_blocks_dashboard_spawn(self):
        before=self.state.snapshot()
        with patch.object(dashboard,'running',side_effect=[False,True]):
            response=self.post()
        self.assertEqual(response.status_code,409)
        for key in ('run_id','scripts','tasks','requests','assemblies','requested_script_count'):
            self.assertEqual(self.state.snapshot()[key],before[key])
        self.popen.assert_not_called()

    def test_stale_watcher_cannot_override_resumed_run_returncode(self):
        with self.state.txn() as data:data['run_attempt_id']='current-attempt'
        stale=Mock();stale.wait.return_value=99
        dashboard.watch_run(stale,'original-run','old-attempt')
        self.assertEqual(self.state.snapshot()['run_returncode'],1)
        dashboard.watch_run(stale,'original-run')
        self.assertEqual(self.state.snapshot()['run_returncode'],1)
        current=Mock();current.wait.return_value=0
        dashboard.watch_run(current,'original-run','current-attempt')
        self.assertEqual(self.state.snapshot()['run_returncode'],0)

    def test_archive_stopped_run_removes_exact_tasks_and_scripts_but_keeps_quota_audit(self):
        scripts=self.directory/'scripts';scripts.mkdir()
        task_ids=[f'old-script-{index}#seg0' for index in range(1,4)]
        request_ids=[f'old-request-{index}' for index in range(1,4)]
        tasks=[];requests={};script_rows=[];entries={};quota_requests={}
        for index,(task_id,rid) in enumerate(zip(task_ids,request_ids),1):
            script_id=f'old-script-{index}'
            path=scripts/f'{script_id}_fixture.txt';path.write_text('stale script',encoding='utf-8')
            script_rows.append(dict(id=script_id,file=str(path),text='stale script'))
            tasks.append(dict(id=task_id,script_id=script_id,seg_index=0,status='awaiting_result',
                              account='fixture',request_id=rid,raw_video='',video='',remote_ref={}))
            requests[rid]=dict(request_id=rid,account='fixture',task_ids=[task_id],
                               submitted=True,phase='unknown')
            quota_requests[rid]=dict(request_id=rid,date='2000-01-01',task_ids=[task_id],submitted=True)
            entries[rid+':'+task_id]=dict(task_id=task_id,request_id=rid,state='unknown',remote_ref={})
        self.state.reset(dict(run_id='old-run',run_log=str(self.log),run_returncode=-15,
                              scripts=script_rows,tasks=tasks,requests=requests,assemblies={}))
        self.ledger.reset({'accounts':{'fixture':{'status':'busy','quota_requests':quota_requests,
            'quota_days':{'2000-01-01':{'limit':4,'baseline_used':0,'entries':entries}}}}})
        response=self.client.post('/api/run/archive',json={'run_id':'old-run','task_ids':task_ids})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json['removed_tasks'],3)
        self.assertEqual(len(response.json['deleted_script_files']),3)
        saved=self.state.snapshot()
        self.assertEqual((saved['run_id'],saved['tasks'],saved['scripts'],saved['requests']),('',[],[],{}))
        self.assertEqual(saved['archived_runs'][0]['task_ids'],task_ids)
        self.assertTrue(all(not (scripts/f'old-script-{index}_fixture.txt').exists() for index in range(1,4)))
        ledger=self.ledger.snapshot()['accounts']['fixture']
        self.assertEqual(ledger['status'],'available')
        self.assertTrue(all(e['state']=='archived_unknown' for e in ledger['quota_days']['2000-01-01']['entries'].values()))
        quota=dashboard.account_manager(['fixture']).quota('fixture')
        self.assertEqual((quota['pending_recovery'],quota['unknown'],quota['remaining']),(0,0,4))

    def test_archive_rejects_changed_task_list_without_mutation(self):
        state_before=self.state.snapshot();ledger_before=self.ledger.snapshot()
        response=self.client.post('/api/run/archive',json={'run_id':'original-run','task_ids':['wrong-task']})
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.state.snapshot(),state_before)
        self.assertEqual(self.ledger.snapshot(),ledger_before)

    def test_archive_recovers_files_from_legacy_plan_task_ids(self):
        request_ids=self.seed_ledger_recovery()
        scripts=self.directory/'scripts';scripts.mkdir()
        task_ids=[
            'r20261109_214857_952c_gc6cba47e7a_s0_1_pe39191f52b#seg0',
            'r20261109_214857_952c_gc6cba47e7a_s0_2_pe39191f52b#seg0',
            'r20261109_214857_952c_gc6cba47e7a_s0_3_pe39191f52b#seg0',
        ]
        files=[]
        for index in range(1,4):
            path=scripts/f'r20261109_214857_952c_gc6cba47e7a_s0_{index}_reczz28SP0lW5yhl.txt'
            path.write_text('legacy script',encoding='utf-8');files.append(path)
        unrelated=scripts/'r20261109_214857_952c_gc6cba47e7a_s0_10_reczz28SP0lW5yhl.txt'
        unrelated.write_text('unrelated script',encoding='utf-8')
        archived=dict(run_id='r20261109_214857_952c',task_ids=task_ids,
            request_bindings=[
                dict(account=name,request_id=rid,task_ids=[task_id])
                for name,rid,task_id in zip(('doubao2','doubao1','doubao3'),request_ids,task_ids)],
            script_files=[],task_count=3,script_count=3)
        with self.state.txn() as data:
            data.update(run_id='',scripts=[],tasks=[],requests={},assemblies={},archived_runs=[archived])

        response=self.client.post('/api/run/archive',json={'run_id':archived['run_id'],'task_ids':task_ids})

        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(set(response.json['deleted_script_files']),{path.name for path in files})
        self.assertTrue(all(not path.exists() for path in files))
        self.assertTrue(unrelated.exists())
        saved=self.state.snapshot()['archived_runs'][0]
        self.assertEqual(set(Path(path).name for path in saved['script_files']),{path.name for path in files})


if __name__=='__main__':unittest.main()
