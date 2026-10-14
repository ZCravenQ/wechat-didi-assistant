"""离线测试本地 UI、城市解析和报价隐藏；不使用真实凭证。"""
import json
import re
import socket
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from admin_ui import Admin, local_ui, make_server
from agent import Agent, Store
from place_search import PlaceSearch, Settings
from test_agent import FakeDidi, ScriptLLM, ROUTE


def amap_response(data):
    def respond(url, body, headers, **kwargs):
        method = body['method']
        if method == 'initialize':
            result = {'protocolVersion': '2025-03-26'}
        elif method == 'tools/list':
            result = {'tools': [{'name': 'maps_text_search'}]}
        else:
            result = {'content': [{'type': 'text', 'text': json.dumps(data)}]}
        return {'jsonrpc': '2.0', 'id': body.get('id'), 'result': result}, {}
    return respond


class MemoryFake:
    from memory_mcp import MemoryMCP
    owner = staticmethod(MemoryMCP.owner)

    def __init__(self):
        self.items = []

    def list(self, session):
        return list(self.items)

    def save(self, session, args, text):
        self.items = [i for i in self.items if (i['kind'], i['name']) != (args['kind'], args['name'])]
        self.items.append({k: args[k] for k in ('kind', 'name', 'value', 'city')})

    def delete(self, session, args, text):
        self.items = [i for i in self.items if (i['kind'], i['name']) != (args['kind'], args['name'])]


class PlacesTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict('os.environ', {'DIDI_DEFAULT_CITY': '', 'AMAP_API_KEY': ''})
        self.env_patch.start()
        self.temp = TemporaryDirectory()
        self.settings = Settings(Path(self.temp.name) / 'settings.json')
        self.places = PlaceSearch(self.settings)
        self.store = Store(':memory:')
        self.didi = FakeDidi()
        self.agent = Agent(self.didi, ScriptLLM(), self.store, places=self.places)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()
        self.env_patch.stop()

    def test_default_city_requires_only_two_place_names(self):
        self.settings.save('北京市')
        state = {'points': []}
        result = self.agent.execute(state, 'search_places', {'keywords': '起点'})
        self.agent.execute(state, 'search_places', {'keywords': '终点'})
        self.assertEqual(result['search_city'], '北京市')
        self.assertEqual(len(state['points']), 2)
        self.assertEqual(self.didi.calls[0][1], {'keywords': '起点', 'city': '北京市'})
        self.agent.execute(state, 'taxi_estimate', ROUTE)

    def test_nationwide_city_then_mcp_coordinate_provenance(self):
        self.settings.save('上海市', 'test-key')
        response = {'status': 10000, 'results': [{'name': '起点', 'city': '北京市',
                     'address_all': '完整地址', 'location': {'lng': 9, 'lat': 9}}]}
        state = {'points': []}
        with patch('place_search.request_json', side_effect=amap_response(response)) as request:
            result = self.agent.execute(state, 'search_places', {'keywords': '起点'})
        self.assertEqual(request.call_args.args[1]['params'],
                         {'name': 'maps_text_search', 'arguments': {'keywords': '起点', 'city': '上海市'}})
        self.assertEqual(result['search_city'], '北京市')
        self.assertEqual(self.didi.calls[0][1]['city'], '北京市')
        self.assertEqual(state['points'], [['116.1', '39.1']])
        self.assertNotIn('"lng":9', str(result))

    def test_multiple_cities_must_choose_address_before_coordinates(self):
        self.settings.save('', 'test-key')
        response = {'status': '10000', 'results': [{'name': '国贸', 'city': city,
                                                  'location': {'lng': 9, 'lat': 9}}
                                                 for city in ('北京市', '深圳市')]}
        state = {'points': []}
        with patch('place_search.request_json', side_effect=amap_response(response)):
            result = self.agent.execute(state, 'search_places', {'keywords': '国贸'})
        self.assertTrue(result['requires_address_choice'])
        self.assertNotIn('location', str(result))
        self.assertEqual(state['points'], [])
        self.assertEqual(self.didi.calls, [])

    def test_no_configuration_and_no_result_never_guess_city(self):
        with self.assertRaisesRegex(ValueError, '管理界面'):
            self.agent.execute({}, 'search_places', {'keywords': '起点'})
        self.settings.save('', 'test-key')
        with patch('place_search.request_json', side_effect=amap_response({'pois': []})):
            result = self.agent.execute({}, 'search_places', {'keywords': '起点'})
        self.assertFalse(result['requires_address_choice'])
        self.assertEqual(self.didi.calls, [])

    def test_remembered_place_city_and_explicit_city_override_default(self):
        self.settings.save('上海市')
        memory = MemoryFake()
        memory.items = [{'kind': 'place', 'name': '家', 'value': '起点', 'city': '北京市'}]
        self.agent.memory = memory
        result = self.agent.execute({}, 'search_places', {'keywords': '家'}, session='bot:owner')
        self.assertEqual(result['search_city'], '北京市')
        self.assertEqual(self.didi.calls[-1][1]['keywords'], '起点')
        self.agent.execute({}, 'search_places', {'keywords': '终点', 'city': '杭州市'})
        self.assertEqual(self.didi.calls[-1][1]['city'], '杭州市')

    def test_quote_output_hides_codes_and_named_selection_still_creates(self):
        steps = [[('maps_textsearch', {'keywords': '起点', 'city': '北京市'}),
                  ('maps_textsearch', {'keywords': '终点', 'city': '北京市'})],
                 [('taxi_estimate', ROUTE)], [], [('prepare_ride', {'product_name': '测试快车'})]]
        self.agent.llm = ScriptLLM(steps)
        quote = self.agent.respond('console', '从起点到终点的价格？')
        self.assertIn('测试快车：25 元', quote)
        self.assertNotIn('productCategory', quote)
        self.assertNotIn('品类', quote)
        outputs = [m['content'] for m in self.agent.llm.histories[-1] if m.get('role') == 'tool']
        self.assertNotIn('productCategory', outputs[-1])
        self.assertNotIn('trace-test', outputs[-1])
        state = self.store.get('session:console')
        self.agent.execute(state, 'taxi_generate_ride_app_link', {**ROUTE, 'product_name': '测试快车'})
        self.assertEqual(self.didi.calls[-1][1]['product_category'], '1')
        self.assertNotIn('product_name', self.didi.calls[-1][1])
        self.assertIn('回复“确认”', self.agent.respond('console', '选择测试快车叫车'))
        self.assertIn('订单已创建', self.agent.respond('console', '确认'))
        self.assertEqual([a['product_category'] for n, a in self.didi.calls if n == 'taxi_create_order'], ['1'])

    def test_settings_restart_and_blank_key_preservation(self):
        self.settings.save('北京市', 'secret-map-key')
        restarted = Settings(self.settings.path)
        restarted.save('杭州市')
        self.assertEqual(restarted.get(), {'default_city': '杭州市', 'amap_key': 'secret-map-key'})
        restarted.save('', '')
        self.assertEqual(restarted.get()['amap_key'], '')


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict('os.environ', {'DIDI_DEFAULT_CITY': '', 'AMAP_API_KEY': ''})
        self.env_patch.start()
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(self.root / 'settings.json')
        self.memory = MemoryFake()
        self.admin = Admin(self.memory, settings=self.settings, runtime=self.root)
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        self.port = sock.getsockname()[1]
        sock.close()
        self.context = local_ui(self.admin, self.port)
        self.server = self.context.__enter__()
        self.base = 'http://127.0.0.1:' + str(self.port)
        with urlopen(self.base) as response:
            self.html = response.read().decode()
            self.assertIn('frame-ancestors', response.headers['Content-Security-Policy'])
        self.token = re.search("const token='([^']+)'", self.html)[1]

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.temp.cleanup()
        self.env_patch.stop()

    def call(self, path, data=None, headers=None):
        req = Request(self.base + path, data=None if data is None else json.dumps(data).encode(),
                      headers={'X-Admin-Token': self.token, 'Content-Type': 'application/json', **(headers or {})})
        with urlopen(req) as response:
            return json.loads(response.read())

    def test_loopback_settings_and_secret_redaction(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        self.call('/api/settings', {'default_city': '北京市', 'amap_key': 'private-key'})
        with patch.dict('os.environ', {'LLM_API_KEY': 'private-llm', 'DIDI_MCP_KEY': 'private-didi'}):
            status = self.call('/api/status')
        self.assertEqual(status['default_city'], '北京市')
        self.assertTrue(status['amap_key_configured'])
        self.assertNotIn('private-', json.dumps(status))

    def test_cross_site_and_dns_rebinding_requests_rejected(self):
        with self.assertRaises(HTTPError) as e:
            urlopen(self.base + '/api/status')
        self.assertEqual(e.exception.code, 403)
        for headers in ({'Origin': 'https://evil.example'}, {'Host': 'evil.example'}, {'X-Admin-Token': 'wrong'}):
            with self.assertRaises(HTTPError) as e:
                self.call('/api/settings', {'default_city': '北京市'}, headers)
            self.assertEqual(e.exception.code, 403)
        self.assertEqual(self.settings.get()['default_city'], '')

    def test_ui_crud_clears_stale_context_and_pending(self):
        with self.admin.store() as store:
            store.put('session:console', {'turns': [['old']], 'points': [[1, 2]], 'pending': {'old': True}, 'quote': {}})
        profile = self.call('/api/status')['profiles'][0]['id']
        record = {'profile': profile, 'kind': 'place', 'name': '<img src=x>', 'value': '地点', 'city': '北京市'}
        self.call('/api/memory/save', record)
        self.assertEqual(self.call('/api/memories?profile=' + profile)['items'][0]['name'], record['name'])
        with self.admin.store() as store:
            state = store.get('session:console')
        self.assertEqual(state['turns'], [])
        self.assertNotIn('pending', state)
        self.assertIn('textContent=item.name', self.html.replace("(item.kind==='place'?'地点 · ':'偏好 · ')+", ''))
        self.call('/api/memory/delete', record)
        self.assertEqual(self.call('/api/memories?profile=' + profile)['items'], [])
        with self.assertRaises(HTTPError):
            self.call('/api/memory/save', {**record, 'profile': '../../other-user'})

    def test_ui_does_not_expose_order_mutations(self):
        with self.assertRaises(HTTPError):
            self.call('/api/taxi_create_order', {})
        with self.assertRaises(HTTPError):
            self.call('/api/memory/save', [])
        with self.assertRaisesRegex(ValueError, '被占用'):
            make_server(self.admin, self.port)


if __name__ == '__main__':
    unittest.main()
