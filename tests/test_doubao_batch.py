import hashlib
import copy
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from doubao_automation.doubao_batch import (BatchGenerationMixin,batch_prompt,choose_reply,
                                             generation_unavailable_matches,quota_exhaustion_matches,
                                             quota_remaining)
from doubao_automation.doubao_agent import DEFAULT_QUOTA_EXHAUSTED_PATTERNS


def asset(url,index=0):
    return {'url':url,'stable':url.split('?')[0],'video_index':index,
            'message_id':'fixture-'+hashlib.sha256(url.split('?')[0].encode()).hexdigest()[:12],
            'message_ordinal':0,'duration':15,'ownership_verified':True,
            'owner_user_message_id':'fixture-owner'}


class Page:
    url='https://www.doubao.com/chat/123456789'
    def __init__(self):self.clock=0
    def locator(self,_):return self
    def inner_text(self,**kwargs):return '视频生成'
    def wait_for_timeout(self,_):self.clock+=10
    def goto(self,*args,**kwargs):pass


class Agent(BatchGenerationMixin):
    def __init__(self,steps,replies=None):
        self.page=Page();self.name='test';self.dcfg={'generation_timeout_minutes':1,'poll_interval_seconds':1}
        self.quota_patterns=[];self.failure_patterns=[];self.steps=iter(steps);self.last=[]
        self.replies=iter(replies or ['生成中']*20);self.sent=[]
    def open_chat(self):pass
    def new_chat(self):pass
    def configure_video_mode(self):pass
    def upload_images(self,_):pass
    def _assert_logged_in(self):pass
    def _wait_chat_ready(self, **kwargs):pass
    def _handle_dialogs(self):pass
    def type_and_send(self,text):self.sent.append(text)
    def _assistant_text(self):
        self._last_reply_source='latest_assistant'
        return next(self.replies,'生成中') if self.sent else '欢迎'
    def _batch_assets(self):
        self.last=next(self.steps,self.last)
        if isinstance(self.last,tuple):
            self._batch_cards_snapshot=self.last[1];return self.last[0]
        return self.last
    def _download_batch_asset(self,ref,dest):Path(dest).write_bytes(b'x'*2000);return True


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.tasks=[{'id':f's#seg{i}','seg_index':i,'seg_total':4,'target_seconds':15,'est_seconds':15,
                     'start_seconds':15*i,'end_seconds':15*(i+1),'prompt':f'本段原文{i}'} for i in range(2)]
        self.dests={t['id']:Path(self.tmp.name)/f'{i}.mp4' for i,t in enumerate(self.tasks)}
    def tearDown(self):self.tmp.cleanup()

    def test_platform_free_video_quota_word_order_is_recognized(self):
        patterns=[re.compile(pattern,re.IGNORECASE) for pattern in DEFAULT_QUOTA_EXHAUSTED_PATTERNS]
        self.assertTrue(quota_exhaustion_matches('今日视频生成免费额度已用完。开通订阅畅享更多额度。',patterns))
        self.assertTrue(quota_exhaustion_matches('今日免费视频额度已经耗尽',patterns))
        self.assertFalse(quota_exhaustion_matches('网络连接错误：今日视频生成免费额度已用完',patterns))
        self.assertFalse(quota_exhaustion_matches('今日视频生成暂时失败，请稍后重试',patterns))

    def test_explicit_rendering_capability_refusal_is_recognized(self):
        refusal=('我这边无法直接执行视频渲染输出，这份是可以直接投放使用的标准化生成脚本 + 英文生成 prompt，'
                 '你可以直接复制到 AI 视频生成工具，按参数 9:16 / 720p / 15s 运行这条独立视频。')
        self.assertTrue(generation_unavailable_matches(refusal))
        self.assertTrue(generation_unavailable_matches('我无法为你生成视频。'))
        self.assertTrue(generation_unavailable_matches('I cannot render or generate this video.'))
        self.assertFalse(generation_unavailable_matches('视频正在生成，请稍候。'))
        self.assertFalse(generation_unavailable_matches('无法直接提供脚本，可生成视频。'))
        self.assertFalse(generation_unavailable_matches('视频渲染输出格式为 720p，时长 15 秒。'))

    def test_definitive_quota_denial_after_active_reply_is_safe_to_release(self):
        agent=Agent([[]],['生成中','今日视频生成免费额度已用完。开通订阅畅享更多额度。'])
        agent.quota_patterns=[re.compile(pattern,re.IGNORECASE)
                              for pattern in DEFAULT_QUOTA_EXHAUSTED_PATTERNS]
        result,generated,_=self.run_agent(agent)
        self.assertEqual(result['error_kind'],'quota')
        self.assertTrue(result['continuation_safe'])
        self.assertFalse(generated)
        self.assertEqual(len(agent.sent),1)

    def test_reference_analysis_keeps_polling_and_allows_a_real_later_confirmation(self):
        a,b=asset('https://cdn/one.mp4'),asset('https://cdn/two.mp4',1)
        agent=Agent([[],[],[],[a,b]],[
            '当前视频解析大约需要 1 分钟，请耐心等待哦，回答完成后我会主动发送给你。视频生成失败了怎么办？',
            '请确认后开始生成。',
        ])
        agent.failure_patterns=[re.compile('视频生成失败')]
        result,generated,_=self.run_agent(agent)
        self.assertFalse(result['unresolved'])
        self.assertEqual(len(generated),2)
        self.assertEqual(len(agent.sent),2)
        self.assertIn('立即开始生成',agent.sent[1])

    def test_capability_refusal_stops_polling_without_claiming_video_was_generated(self):
        refusal='我无法直接执行视频渲染输出并提供下载文件，这份是已经打磨好的成品生成提示词。'
        agent=Agent([[]],[refusal])
        result,generated,_=self.run_agent(agent)
        self.assertEqual(result['error_kind'],'not_generated')
        self.assertTrue(result['continuation_safe'])
        self.assertEqual(result['rejection_evidence'],{
            'kind':'generation_unavailable','definitive':True,'active':False,
            'source':'latest_assistant','video_asset_found':False})
        self.assertFalse(generated)
        self.assertEqual(len(agent.sent),1)
        self.assertLess(agent.page.clock,60)

    def run_agent(self,agent,context=None):
        generated=[];submitted=[]
        with patch('doubao_automation.doubao_batch.time.monotonic',side_effect=lambda:agent.page.clock):
            result=agent.run_batch(self.tasks,[],self.dests,{'total_seconds':50,**(context or {})},
                                   lambda url,baseline:submitted.append((url,baseline)),
                                   lambda tid,ref:generated.append((tid,ref)))
        return result,generated,submitted
    def test_fresh_batch_filters_empty_refs_and_bills_each_actual_asset_once(self):
        a,b=asset('https://cdn/one.mp4'),asset('https://cdn/two.mp4',1)
        agent=Agent([[],[a],[a,b]])
        result,generated,_=self.run_agent(agent,{'remote_refs':{t['id']:{} for t in self.tasks}})
        self.assertEqual(set(result['videos']),set(self.dests))
        self.assertEqual([x[0] for x in generated],list(self.dests))
        self.assertEqual(len(agent.sent),1)
    def test_historical_video_is_excluded_from_new_batch(self):
        old=asset('https://cdn/old.mp4');a=asset('https://cdn/new1.mp4',1);b=asset('https://cdn/new2.mp4',2)
        result,generated,_=self.run_agent(Agent([[old],[old,a,b]]))
        self.assertEqual(len(generated),2)
        self.assertTrue(all(x[1]['url']!='https://cdn/old.mp4' for x in generated))
        self.assertFalse(result['unresolved'])
    def test_ambiguous_resumed_history_never_generates_or_bills_old_video(self):
        old=asset('blob:old');changed=asset('blob:changed')
        agent=Agent([[changed]])
        result,generated,_=self.run_agent(agent,{'resume_only':True,'chat_url':agent.page.url,
          'baseline':{'keys':[hashlib.sha256(b'blob:old').hexdigest()],'assets':[old]}})
        self.assertEqual(result['error_kind'],'needs_user')
        self.assertFalse(generated);self.assertFalse(agent.sent)
    def test_resume_only_downloads_and_reconciles_partial_result(self):
        a,b=asset('https://cdn/one.mp4'),asset('https://cdn/two.mp4',1)
        ref=dict(a,key=hashlib.sha256(b'https://cdn/one.mp4').hexdigest())
        agent=Agent([[a,b]])
        result,generated,_=self.run_agent(agent,{'resume_only':True,'chat_url':agent.page.url,
          'baseline':{'keys':[]},'remote_refs':{self.tasks[0]['id']:ref}})
        self.assertEqual(len(generated),2);self.assertFalse(result['unresolved']);self.assertFalse(agent.sent)

    def resumed_confirmation(self, controls, readiness_error=None):
        task={'id':'script#seg0','script_id':'script','seg_index':0,'seg_total':1,
              'target_seconds':15,'est_seconds':15,'start_seconds':0,'end_seconds':15,'prompt':'original'}
        agent=Agent([[]])
        if readiness_error:
            agent._wait_chat_ready=Mock(side_effect=readiness_error)
        owner={'prompt':'original prompt','marker':'【自动化任务标记：request-approved】',
               'chat_path':'/chat/123456789','control_texts':[],'task_ids':[task['id']]}
        owner['original_anchor_proof']=agent._original_anchor_proof(owner,owner['chat_path'])
        baseline={'scope_version':1,'ownership_acknowledged':True,'ownership':owner,
                  'keys':[],'assets':[],'card_count':0,'controls':copy.deepcopy(controls)}
        scope={'owner_found':True,'route_ready':True,'route_marker_found':True,
               'chat_path':owner['chat_path'],'anchor_kind':'original','user_message_id':'user-original',
               'latest_control_mounted':True,'later_user_turn':False,
               'latest_reply_message_id':'assistant-confirm'}
        def assets():
            agent._batch_scope_state=scope
            agent._batch_cards_snapshot=[]
            return []
        agent._batch_assets=assets
        replies=iter(['请确认后开始生成','生成中'])
        def assistant():
            agent._last_reply_source='latest_assistant'
            return next(replies,'生成中')
        agent._assistant_text=assistant
        submitted=[]
        with patch('doubao_automation.doubao_batch.time.monotonic',side_effect=lambda:agent.page.clock):
            result=agent.run_batch([task],[],{task['id']:self.dests['s#seg0']},
                {'total_seconds':15,'model':'mini','resume_only':True,'chat_url':agent.page.url,'task_ids':[task['id']],
                 'original_task_ids':[task['id']],'baseline':baseline},
                lambda url,value:submitted.append((url,copy.deepcopy(value))),lambda *_:None)
        return agent,result,submitted

    def test_approved_resume_sends_one_confirmation_without_resending_original_prompt(self):
        task_id='script#seg0'
        agent,result,submitted=self.resumed_confirmation([
            {'phase':'operator_approved_resume','task_ids':[task_id]},
        ])
        self.assertEqual(agent.sent,['确认，开始生成这条视频。'])
        self.assertNotIn('original prompt',agent.sent[0])
        self.assertEqual(result['submitted'],True)
        self.assertTrue(submitted)
        saved=[control for _,value in submitted for control in value['controls']
               if control['phase']=='operator_confirmation_dispatched']
        self.assertEqual(len(saved),1)

    def test_resume_without_approval_or_after_dispatch_never_sends_confirmation_twice(self):
        task_id='script#seg0'
        agent,result,_=self.resumed_confirmation([])
        self.assertEqual(result['error_kind'],'needs_user')
        self.assertEqual(agent.sent,[])
        agent,result,_=self.resumed_confirmation([
            {'phase':'operator_approved_resume','task_ids':[task_id]},
            {'phase':'operator_confirmation_dispatched','task_ids':[task_id]},
        ])
        self.assertEqual(result['error_kind'],'needs_user')
        self.assertEqual(agent.sent,[])

    def test_resume_without_ready_composer_does_not_record_confirmation_dispatch(self):
        task_id='script#seg0'
        agent,result,submitted=self.resumed_confirmation(
            [{'phase':'operator_approved_resume','task_ids':[task_id]}],
            readiness_error=RuntimeError('等待输入框超时'),
        )
        agent._wait_chat_ready.assert_called_once()
        self.assertIn('等待输入框超时',result['error'])
        self.assertEqual(agent.sent,[])
        self.assertFalse(any(control['phase']=='operator_confirmation_dispatched'
                             for _,value in submitted for control in value['controls']))
    def test_partial_unknown_request_never_automatically_resubmits(self):
        a=asset('https://cdn/one.mp4')
        agent=Agent([[],[a]])
        result,generated,_=self.run_agent(agent)
        self.assertEqual(result['unresolved'],[self.tasks[1]['id']])
        self.assertEqual(len(generated),1);self.assertEqual(len(agent.sent),1)
        self.assertFalse(result['continuation_safe'])

    def test_resumed_history_waits_for_all_baseline_cards_before_identity_check(self):
        old_a,old_b=asset('https://cdn/old-a.mp4'),asset('https://cdn/old-b.mp4',1)
        a,b=asset('https://cdn/new-a.mp4',2),asset('https://cdn/new-b.mp4',3)
        agent=Agent([[old_a],[old_a,old_b,a,b]])
        result,generated,_=self.run_agent(agent,{'resume_only':True,'chat_url':agent.page.url,
          'baseline':{'keys':[agent._key_asset(v) for v in (old_a,old_b)],'assets':[old_a,old_b]}})
        self.assertFalse(result['unresolved']);self.assertFalse(agent.sent)
        self.assertEqual([ref['url'] for _,ref in generated],[a['url'],b['url']])
    def test_choice_variants_are_bounded_to_one_control_reply(self):
        agent=Agent([[]],['方案一压缩为15秒，方案二分成两段15秒，请选择',
                          '你希望压缩，还是分成两段？']*8)
        # The model was verified on the initial video composer. The formal
        # conversation then shows only its text composer for clarification.
        with patch.object(agent, 'configure_video_mode') as configure, \
             patch.object(agent, '_require_video_model') as verify:
            self.run_agent(agent, {'model':'mini'})
        configure.assert_called_once_with(model='mini')
        verify.assert_called_once_with('mini')
        self.assertEqual(len(agent.sent),2)
        self.assertIn('选择分段',agent.sent[1]);self.assertNotIn('【完整剧本',agent.sent[1])
    def test_followup_fails_before_dispatch_when_composer_moves_to_other_chat(self):
        agent=Agent([[]],['请确认后开始生成'])
        def open_other_chat(**kwargs):
            agent.page.url='https://www.doubao.com/chat/987654321'
        agent._wait_chat_ready=Mock(side_effect=open_other_chat)
        result,_,submitted=self.run_agent(agent)
        agent._wait_chat_ready.assert_called_once()
        self.assertIn('已离开原对话', result['error'])
        self.assertEqual(len(agent.sent),1)
        self.assertFalse(any(baseline['controls'] for _,baseline in submitted))

    def test_explicit_single_limit_sends_only_missing_second_segment(self):
        a,b=asset('https://cdn/one.mp4'),asset('https://cdn/two.mp4',1)
        agent=Agent([[],[],[a],[a,b]],['一次只能生成一条视频','第一条已经生成完成'])
        absent_control = Mock()
        absent_control.count.return_value = 0
        agent.page.get_by_role = Mock(return_value=absent_control)
        with patch.object(agent, 'configure_video_mode'), patch.object(agent, '_require_video_model') as verify:
            result,generated,_=self.run_agent(agent, {'model':'fast'})
        verify.assert_called_once_with('fast')
        agent.page.get_by_role.assert_called_once()
        self.assertEqual(len(generated),2);self.assertEqual(len(agent.sent),3)
        self.assertIn('视频2/4',agent.sent[-1]);self.assertFalse(result['unresolved'])
    def test_prompt_preserves_full_length_and_only_two_windows(self):
        prompt=batch_prompt(self.tasks,{'total_seconds':50,'script_text':'完整50秒原文'})
        self.assertIn('50秒',prompt);self.assertIn('0–15',prompt);self.assertIn('15–30',prompt)
        with self.assertRaises(ValueError):batch_prompt(self.tasks*2,{'total_seconds':50})
    def test_parser_does_not_infer_quota_from_unrelated_daily_chat(self):
        self.assertEqual(quota_remaining('今日视频生成剩余2次'),2)
        self.assertIsNone(quota_remaining('每天4次，每次最多2条'))
        self.assertEqual(choose_reply('方案1压缩为15秒，方案2分成两段，请选择'),'split')

    def test_parser_requires_an_offer_to_generate_a_video_not_just_its_prompt(self):
        self.assertEqual(choose_reply('要不要我现在就按这份提示词，直接提交生成这条 15 秒视频？'), 'confirm')
        self.assertEqual(choose_reply('要我帮你生成视频吗？'), 'confirm')
        self.assertEqual(choose_reply('要不要我现在就帮你写一份视频生成提示词？'), '')
        self.assertEqual(choose_reply('要我帮你生成视频提示词吗？'), '')

    def test_failed_first_card_does_not_steal_successful_second_slot(self):
        failed={'kind':'card','status':'failed','card_position':0}
        ready={'kind':'card','status':'ready','card_position':1,'stable':'card:msg:1','url':'',
               'card_message_id':'msg','card_ordinal':1,'receive_index':0,
               'ownership_verified':True,'owner_user_message_id':'fixture-owner'}
        agent=Agent([([],[]),([ready],[failed,ready])],['你的内容已生成完毕，其中1个视频生成失败，失败视频的生成额度未扣除。'])
        result,generated,_=self.run_agent(agent)
        self.assertEqual([tid for tid,_ in generated],[self.tasks[1]['id']])
        self.assertEqual(set(result['videos']),{self.tasks[1]['id']})
        self.assertEqual(result['rejected_task_ids'],[self.tasks[0]['id']])
        self.assertTrue(result['rejection_evidence']['quota_not_charged'])

    def test_resume_filtered_request_keeps_original_card_slot_order(self):
        ready={'kind':'card','status':'ready','card_position':1,'stable':'card:msg:1','url':'',
               'card_message_id':'msg','card_ordinal':1,'receive_index':0,
               'ownership_verified':True,'owner_user_message_id':'fixture-owner'}
        failed={'kind':'card','status':'failed','card_position':0}
        agent=Agent([([ready],[failed,ready])])
        second=self.tasks[1];self.tasks=[second]
        result,generated,_=self.run_agent(agent,{'resume_only':True,'chat_url':agent.page.url,
                         'baseline':{'keys':[]},'original_task_ids':['s#seg0','s#seg1']})
        self.assertEqual([tid for tid,_ in generated],['s#seg1'])
        self.assertEqual(set(result['videos']),{'s#seg1'});self.assertFalse(agent.sent)


if __name__=='__main__':unittest.main()
