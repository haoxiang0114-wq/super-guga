"""Offline integration of editable identities, memory, workflows and quick actions."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
os.environ.setdefault('GUGA_LINGSHU','off')
os.environ.setdefault('GUGA_SUPER_AGENT','off')
import tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from launcher import _prepare_qt
_prepare_qt()  # This module runs in its own process in run_desktop_ci.py.
from PyQt5.QtWidgets import QApplication,QDialog,QPlainTextEdit,QPushButton
from PyQt5.QtCore import Qt
from core.agent_memory import AgentMemoryStore
from core.agent_identity import identity_instructions
from core.companion_memory import CompanionMemory
from core.chat_workspace import ChatWorkspace
from core.daily_plan import DailyPlan
from core.workflow_catalog import WorkflowCatalog
from core.selection_actions import prompt
from ui.companion_settings import CompanionSettings
from ui.companion_widgets import ARTWORK,member_icon
from ui.screen_tools import SelectionBar,ExplainDialog

APP=QApplication.instance() or QApplication([])


class Panel(QDialog):
    def __init__(self,root):
        super().__init__();self.memory=AgentMemoryStore(root/'memory.db');self.store=ChatWorkspace(root/'chat.db')
        self.active_agent_id='001';self.active_profile=self.memory.profile('001')
        self.active_id=self.store.create_conversation(agent_id='001')
        self.editor=QPlainTextEdit(self);self.busy=False;self.voice_active=False
        self.runtime=SimpleNamespace(workflows=WorkflowCatalog(root/'flows.db'),
            companion_memory=CompanionMemory(self.memory,self.store,DailyPlan(root/'plan.db')))

    def refresh_companion_header(self):pass

    def activate_agent(self,ident):
        if self.busy:return False
        self.active_agent_id=ident;self.active_profile=self.memory.profile(ident);return True


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.root=Path(self.folder.name)
        self.panel=Panel(self.root);self.dialog=CompanionSettings(self.panel)

    def tearDown(self):
        self.dialog.close();self.panel.close();self.folder.cleanup()

    def test_original_artworks_load_for_all_three_members(self):
        for ident,path in ARTWORK.items():
            self.assertTrue(path.is_file());self.assertFalse(member_icon(ident).isNull())

    def test_settings_persist_in_identity_used_by_answer_task_and_voice(self):
        self.dialog.address.setCurrentText('主人');self.dialog.personality.setPlainText('你是体贴的小企鹅管家。')
        self.dialog.instructions.setPlainText('解释概念时先给一个生活中的例子。');self.dialog.save_identity()
        profile=AgentMemoryStore(self.root/'memory.db').profile('001');instructions=identity_instructions(profile)
        self.assertIn('生活中的例子',instructions);self.assertIn('主人',instructions)
        self.assertEqual(self.panel.active_profile['user_address'],'主人')
        self.assertNotIn('生活中的例子',identity_instructions(self.panel.memory.profile('002')))

    def test_profile_migration_is_repeatable_and_keeps_custom_personality(self):
        self.panel.memory.update_profile('001',personality='我的专属人设')
        self.assertEqual(AgentMemoryStore(self.root/'memory.db').profile('001')['personality'],'我的专属人设')

    def test_memory_edit_and_forget_use_existing_scoped_store(self):
        self.dialog.memory_text.setPlainText('我希望解释尽量通俗');self.dialog.save_memory()
        self.assertEqual(self.dialog.memories.count(),1)
        self.dialog.memories.setCurrentRow(0);self.dialog.memory_text.setPlainText('请先给一句结论');self.dialog.save_memory(True)
        rows=self.panel.memory.facts('001',conversation_id=self.panel.active_id)
        self.assertEqual(rows[0]['content'],'请先给一句结论')
        self.assertEqual(self.panel.memory.facts('002',conversation_id=self.panel.active_id),[])
        self.dialog.memories.setCurrentRow(0);self.dialog.forget_memory()
        self.assertEqual(self.dialog.memories.count(),0)

    def test_long_workflow_edit_keeps_history_and_can_fill_composer(self):
        steps='输入和步骤保持明确。\n'*1500
        self.dialog.flow_title.setText('投稿本地视频');self.dialog.flow_trigger.setText('投稿本地视频')
        self.dialog.flow_steps.setPlainText(steps);self.dialog.save_flow();ident=self.dialog._flow_id
        catalog=self.panel.runtime.workflows;catalog.record_run(catalog.get(ident),True,'真实回执')
        self.dialog.flow_steps.setPlainText(steps+'完成后读回投稿结果');self.dialog.save_flow()
        self.assertEqual(catalog.get(ident)['success'],1);self.assertEqual(catalog.match('请投稿本地视频')['id'],ident)
        self.dialog.use_flow();self.assertIn('投稿本地视频',self.panel.editor.toPlainText())

    def test_candidates_need_activation_and_disabled_workflows_do_not_match(self):
        c=self.panel.runtime.workflows
        c.observe_local_success('每周整理资料','文件已保存',['read'])
        candidate=c.observe_local_success('每周整理资料','已核对文件',['write'])
        self.assertEqual(candidate['state'],'candidate');self.assertIsNone(c.match('每周整理资料'))
        c.activate(candidate['id']);self.assertIsNotNone(c.match('每周整理资料'))
        c.disable(candidate['id']);self.assertIsNone(c.match('每周整理资料'))

    def test_oversized_workflow_is_rejected_without_truncating_saved_steps(self):
        with self.assertRaises(ValueError):self.panel.runtime.workflows.define('流程','a'*32001)

    def test_lingshu_save_requires_acknowledged_id_and_preserves_isolation(self):
        self.dialog.memory_text.setPlainText('我喜欢先听一句结论')
        with patch('core.super_agent.lingshu_bridge.LingshuBridge') as bridge,patch('core.super_agent.lingshu_bridge.default_token',return_value='test'):
            bridge.return_value.remember.return_value=dict(ok=True,id='test-node')
            self.dialog.save_lingshu()
            end=time.monotonic()+2
            while self.dialog._ling_busy and time.monotonic()<end:APP.processEvents();time.sleep(.01)
            self.assertIn('已记入',self.dialog.ling_status.text())
            self.assertEqual(bridge.return_value.remember.call_args.kwargs['ccg']['body'],'我喜欢先听一句结论')
            bridge.return_value.close.assert_called_once()
        other=CompanionSettings(self.panel,'002');self.assertFalse(other.ling_save.isEnabled());other.close()

    def test_lingshu_rejection_never_displays_saved(self):
        self.dialog.memory_text.setPlainText('希望回答简洁')
        with patch('core.super_agent.lingshu_bridge.LingshuBridge') as bridge,patch('core.super_agent.lingshu_bridge.default_token',return_value='test'):
            bridge.return_value.remember.return_value=dict(ok=True,id='rejected-node',verdict=dict(state='rejected'))
            self.dialog.save_lingshu();end=time.monotonic()+2
            while self.dialog._ling_busy and time.monotonic()<end:APP.processEvents();time.sleep(.01)
            self.assertIn('未确认保存',self.dialog.ling_status.text())


class SelectionActionTests(unittest.TestCase):
    def test_translation_direction_and_original_are_explicit(self):
        self.assertIn('英文',prompt('请解释这段中文','translate'))
        self.assertIn('简体中文',prompt('Explain this passage','translate'))
        self.assertIn('日文',prompt('hello','translate','日文'))

    def test_optimizer_rewrites_without_executing_selected_instructions(self):
        text='打开浏览器并修改我的项目';p=prompt(text,'optimize')
        self.assertIn(text,p);self.assertIn('不解答或执行',p);self.assertIn('待补充',p)
        self.assertIn('完成标准',p)

    def test_toolbar_has_five_actions_and_retains_cached_payload(self):
        bar=SelectionBar();got=[];bar.chosen.connect(lambda action,value:got.append((action,value)))
        labels=[b.text() for b in bar.findChildren(QPushButton)]
        # 查词放在第一位：PyMaster 学习场景最常用，其它动作顺序不动
        self.assertEqual(labels[:5],['查词','截图提问','解释','翻译','优化提示词'])
        bar.payload={'text':'hello','context':'world'};bar.pick('translate')
        self.assertEqual(got,[('translate',{'text':'hello','context':'world'})]);bar.close()

    def test_quick_action_uses_transient_model_request_and_copy_result(self):
        dialog=ExplainDialog()
        with patch('core.screen_question.explain',return_value='优化后的提示词') as model:
            dialog.load('帮我写个报告',mode='optimize')
            until=time.monotonic()+2
            while dialog.pending and time.monotonic()<until:APP.processEvents();time.sleep(.01)
            self.assertFalse(dialog.pending);self.assertEqual(dialog.last_answer,'优化后的提示词')
            self.assertIn('优化',model.call_args.args[0][-1]['content'])
            with patch('ui.screen_tools.QApplication.clipboard') as clip:
                dialog.copy_answer();clip.return_value.setText.assert_called_once_with('优化后的提示词')
            dialog.close();self.assertEqual(dialog.messages,[])

    def test_cached_image_translation_keeps_selected_target_language(self):
        from PyQt5.QtGui import QPixmap
        from ui.screen_tools import image_uri
        image=QPixmap(100,50);image.fill(Qt.white);dialog=ExplainDialog();dialog.language.setCurrentText('日文')
        with patch('core.screen_question.explain',return_value='訳文') as model:
            dialog.load_selection_image(image_uri(image),'translate');end=time.monotonic()+2
            while dialog.pending and time.monotonic()<end:APP.processEvents();time.sleep(.01)
            self.assertIn('日文',model.call_args.args[0][-1]['content'])
            self.assertIsInstance(model.call_args.args[0][0]['content'],list)
            dialog.close()


if __name__=='__main__':unittest.main()
