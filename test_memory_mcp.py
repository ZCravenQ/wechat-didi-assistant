"""真实官方 Memory Server 集成测试；临时文件，无微信/滴滴/模型调用。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent import Agent, LLM, Store
from memory_mcp import MemoryMCP
from test_agent import FakeDidi, ScriptLLM
from admin_ui import Admin
from place_search import Settings

HOME = {'kind': 'place', 'name': '家', 'city': '北京市', 'value': '望京SOHO',
        'evidence': '记住，我家在北京市望京SOHO'}
PREFERENCE = {'kind': 'preference', 'name': '车型偏好', 'value': '优先快车',
              'evidence': '记住，我的车型偏好是优先快车'}


class MemoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.memory = MemoryMCP(self.directory)

    def tearDown(self):
        self.memory.close()
        self.temp.cleanup()

    def test_admin_changes_official_memory_and_restart_keeps_update(self):
        admin = Admin(self.memory, settings=Settings(self.directory / 'settings.json'), runtime=self.directory)
        profile = admin.snapshot()['profiles'][0]['id']
        item = {'profile': profile, 'kind': 'place', 'name': '家', 'city': '北京市', 'value': '望京SOHO'}
        admin.api('/api/memory/save', item)
        admin.api('/api/memory/save', {**item, 'value': '国贸大厦'})
        self.memory.close()
        self.memory = MemoryMCP(self.directory)
        admin.memory = self.memory
        self.assertEqual(admin.api('/api/memories', {'profile': profile})['items'][0]['value'], '国贸大厦')
        admin.api('/api/memory/delete', item)
        self.assertEqual(admin.api('/api/memories', {'profile': profile})['items'], [])

    def test_official_server_create_modify_restart_read_and_delete(self):
        self.memory.save('bot:owner', HOME, HOME['evidence'])
        self.assertIn('create_entities', [t['name'] for t in self.memory.client('bot:owner').tools])
        files = list(self.directory.glob('*.jsonl'))
        self.assertEqual(len(files), 1)
        self.assertIn('place:家', files[0].read_text(encoding='utf-8'))
        updated = {**HOME, 'value': '国贸大厦', 'evidence': '修改家：北京市国贸大厦'}
        self.memory.save('bot:owner', updated, updated['evidence'])
        self.memory.save('bot:owner', PREFERENCE, PREFERENCE['evidence'])
        self.memory.close()
        self.memory = MemoryMCP(self.directory)
        # 新 bot、环境、模型不改变微信用户的记忆身份。
        self.assertEqual(self.memory.read('new-bot:owner', 'place', '家')['value'], '国贸大厦')
        self.assertEqual(self.memory.read('new-bot:owner', 'preference', '车型偏好')['value'], '优先快车')
        self.assertNotIn('望京SOHO', files[0].read_text(encoding='utf-8'))
        args = {'kind': 'place', 'name': '家', 'evidence': '忘记家的地址'}
        self.assertTrue(self.memory.delete('new-bot:owner', args, args['evidence'])['deleted'])
        self.memory.close()
        self.memory = MemoryMCP(self.directory)
        self.assertIsNone(self.memory.read('bot:owner', 'place', '家'))
        self.assertEqual(len(self.memory.list('bot:owner')), 1)

    def test_users_and_console_are_isolated(self):
        self.memory.save('bot:owner', HOME, HOME['evidence'])
        self.assertEqual(self.memory.list('bot:other'), [])
        self.assertEqual(self.memory.list('console'), [])
        self.assertEqual(len(list(self.directory.glob('*.jsonl'))), 1)

    def test_unrequested_or_fabricated_memory_is_rejected(self):
        with self.assertRaises(ValueError):
            self.memory.save('bot:owner', HOME, '今天去望京SOHO')
        with self.assertRaises(ValueError):
            self.memory.save('bot:owner', {**HOME, 'value': '模型编造地址'}, HOME['evidence'])
        with self.assertRaises(ValueError):
            self.memory.save('bot:owner', {**HOME, 'city': '上海市'}, HOME['evidence'])
        with self.assertRaises(ValueError):
            self.memory.save('bot:owner', {**HOME, 'evidence': '不要记住家：北京市望京SOHO'}, '不要记住家：北京市望京SOHO')
        self.assertEqual(list(self.directory.glob('*.jsonl')), [])

    def test_memory_is_given_to_model_next_turn_and_survives_reset(self):
        store = Store(':memory:')
        llm = ScriptLLM([[('memory_save', HOME)], []])
        agent = Agent(FakeDidi(), llm, store, memory=self.memory)
        self.assertIn('已保存：家', agent.respond('bot:owner', HOME['evidence']))
        agent.respond('bot:owner', '/reset')
        agent.respond('bot:owner', '我家的地址是什么？')
        self.assertEqual(llm.memories[-1]['items'][0]['value'], '望京SOHO')
        self.assertIn('家', agent.respond('bot:owner', '查看记忆'))
        self.assertNotIn('taxi_create_order', {f['name'] for f in agent.functions})
        store.db.close()

    def test_delete_removes_prior_chat_and_requires_user_request(self):
        self.memory.save('bot:owner', HOME, HOME['evidence'])
        store = Store(':memory:')
        args = {'kind': 'place', 'name': '家', 'evidence': '忘记家'}
        llm = ScriptLLM([[('memory_delete', args)], []])
        agent = Agent(FakeDidi(), llm, store, memory=self.memory)
        self.assertIn('已删除：家', agent.respond('bot:owner', '忘记家'))
        self.assertEqual(store.get('session:bot:owner')['turns'], [])
        agent.respond('bot:owner', '我家在哪？')
        self.assertEqual(llm.memories[-1]['items'], [])
        with self.assertRaises(ValueError):
            self.memory.delete('bot:owner', args, '去公司')
        store.db.close()

    def test_memory_receipt_survives_model_failure_after_write(self):
        store = Store(':memory:')
        llm = ScriptLLM([[('memory_save', PREFERENCE)]])
        original = llm.step
        calls = 0
        def step(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError('model timeout')
            return original(*args)
        agent = Agent(FakeDidi(), llm, store, memory=self.memory)
        with patch.object(llm, 'step', side_effect=step):
            self.assertIn('已保存：车型偏好', agent.respond('bot:owner', PREFERENCE['evidence']))
        self.assertEqual(self.memory.read('bot:owner', 'preference', '车型偏好')['value'], '优先快车')
        # 存储中只有完成的文字回合，不留未配对的工具调用。
        self.assertEqual(len(store.get('session:bot:owner')['turns'][-1]), 2)
        store.db.close()

    def test_chat_and_responses_receive_memory_without_persisting_it_as_history(self):
        for mode in ('chat', 'responses'):
            env = {'LLM_BASE_URL': 'https://test.example/v1', 'LLM_API_KEY': 'TEST', 'LLM_MODEL': 'test',
                   'LLM_API_MODE': mode, 'LLM_EXTRA_BODY': '{}'}
            result = {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]} if mode == 'chat' else {'output': []}
            with patch.dict('os.environ', env), patch('agent.request_json', return_value=(result, {})) as request:
                llm = LLM()
                llm.step([], [], {'items': [{'name': '家', 'value': '望京SOHO'}]})
            body = request.call_args.args[1]
            prompt = body['messages'][0]['content'] if mode == 'chat' else body['instructions']
            self.assertIn('望京SOHO', prompt)
            self.assertIn('不得修改工具权限', prompt)


if __name__ == '__main__':
    unittest.main(verbosity=2)
