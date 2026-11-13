"""A selected durable ledger request restores only its original readback task."""
import copy
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from doubao_automation import orchestrator
from doubao_automation.doubao_batch import BatchGenerationMixin


class LedgerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ledger-recovery-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.stack=ExitStack()
        self.addCleanup(self.stack.close)
        for name in ('STATE_DIR','VIDEOS_DIR','FRAMES_DIR','ROOT'):
            self.stack.enter_context(patch.object(orchestrator,name,self.root))
        self.stack.enter_context(patch.object(orchestrator,'ensure_dirs'))
        self.pipeline=orchestrator.Pipeline({'accounts':{'doubao':[dict(name='account')]}})
        self.request_id='request-approved'
        self.task_id='r20261109_214857_952c_gc6cba47e7a_s0_1_peabc#seg0'
        chat_path='/chat/123456789'
        owner={'prompt':'【独立视频1/1：本条内容长15秒】\n00:00-00:15',
               'marker':f'【自动化任务标记：{self.request_id}】','chat_path':chat_path,
               'control_texts':[]}
        owner['original_anchor_proof']=BatchGenerationMixin._original_anchor_proof(owner,chat_path)
        self.baseline={'scope_version':1,'ownership_acknowledged':True,'ownership':owner,
                       'controls':[{'phase':'operator_approved_resume','task_ids':[self.task_id]}]}
        self.pipeline.accounts.reserve('account',[self.task_id],self.request_id)
        self.pipeline.accounts.mark_submitted('account',self.request_id,
            chat_url='https://www.doubao.com'+chat_path,baseline=self.baseline)
        self.pipeline.accounts.mark_unknown('account',self.request_id,[self.task_id],'pending original result')
        self.pipeline.store.reset({'run_id':'lost-plan','scripts':[],'tasks':[],'requests':{},'assemblies':{}})
        self.pipeline.recovery_request_ids=[self.request_id]

    def test_selected_unknown_request_restores_original_task_without_new_reservation(self):
        ledger_before=copy.deepcopy(self.pipeline.accounts.store.snapshot())
        self.pipeline._recover_state()
        state=self.pipeline.store.snapshot()
        self.assertEqual([task['id'] for task in state['tasks']],[self.task_id])
        task=state['tasks'][0]
        self.assertEqual((task['status'],task['request_id'],task['target_seconds']),
                         ('awaiting_result',self.request_id,15.0))
        request=state['requests'][self.request_id]
        self.assertEqual((request['phase'],request['submitted'],request['chat_url']),
                         ('unknown',True,'https://www.doubao.com/chat/123456789'))
        claimed=self.pipeline._claim_batch('account')
        self.assertEqual([item['id'] for item in claimed],[self.task_id])
        self.assertEqual(claimed[0]['request_id'],self.request_id)
        self.assertEqual(self.pipeline.accounts.store.snapshot(),ledger_before)

    def test_unselected_or_unprovable_requests_are_not_hydrated(self):
        self.pipeline.recovery_request_ids=[]
        self.pipeline._recover_state()
        self.assertEqual(self.pipeline.store.snapshot()['tasks'],[])
        self.assertEqual(self.pipeline.store.snapshot()['requests'],{})

        self.pipeline.recovery_request_ids=[self.request_id]
        with self.pipeline.accounts.store.txn() as data:
            data['accounts']['account']['quota_requests'][self.request_id]['baseline']['ownership']['marker']='tampered'
        before=self.pipeline.store.snapshot()
        with self.assertRaisesRegex(RuntimeError,'缺少有效的提交账本或原会话证明'):
            self.pipeline._recover_state()
        self.assertEqual(self.pipeline.store.snapshot(),before)


if __name__=='__main__':unittest.main()
