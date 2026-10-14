"""高德协议和打车链路回归；全部模拟网络，不消耗真实额度。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent import Agent, Store
from place_search import AMAP_TOOLS, AmapMCP, PlaceSearch, Settings
from test_agent import FakeDidi, ScriptLLM, ROUTE


class AmapTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict('os.environ', {'AMAP_API_KEY': '', 'DIDI_DEFAULT_CITY': ''})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.settings = Settings(Path(self.temp.name) / 'settings.json')
        self.settings.save('', 'fake-amap-key')
        self.places = PlaceSearch(self.settings)
        self.calls = []
        self.responses = {'maps_text_search': {'pois': [{'name': '起点', 'cityname': '北京市',
                                                        'location': '9,9'}]}}
        self.structured = False
        self.request = patch('place_search.request_json', side_effect=self.rpc).start()
        self.addCleanup(patch.stopall)

    def rpc(self, url, body, headers, **kwargs):
        self.calls.append((url, body, headers))
        method = body['method']
        if method == 'initialize':
            result = {'protocolVersion': '2025-03-26'}
        elif method == 'tools/list':
            if body['params'].get('cursor'):
                result = {'tools': [{'name': t} for t in AMAP_TOOLS if t != 'maps_text_search']}
            else:
                result = {'tools': [{'name': 'maps_text_search'}, {'name': 'maps_schema_take_taxi'}],
                          'nextCursor': 'page-2'}
        elif method == 'notifications/initialized':
            return {}, {}
        else:
            value = self.responses[body['params']['name']]
            if isinstance(value, Exception):
                raise value
            result = ({'structuredContent': value} if self.structured else
                      {'content': [{'type': 'text', 'text': json.dumps(value)}]})
        return {'jsonrpc': '2.0', 'id': body.get('id'), 'result': result}, {'Mcp-Session-Id': 'fake-session'}

    def tool_calls(self):
        return [b['params'] for _, b, _ in self.calls if b['method'] == 'tools/call']

    def test_handshake_pagination_session_reuse_and_allowlist(self):
        result = self.places.nationwide('起点')
        self.places.nationwide('终点')
        self.assertEqual(result, [{'name': '起点', 'city': '北京市', 'address': ''}])
        self.assertEqual(sum(b['method'] == 'initialize' for _, b, _ in self.calls), 1)
        self.assertEqual(self.calls[1][2]['Mcp-Session-Id'], 'fake-session')
        self.assertTrue(all(url == 'https://mcp.amap.com/mcp?key=fake-amap-key' for url, _, _ in self.calls))
        self.assertEqual(self.tool_calls()[0]['arguments'], {'keywords': '起点'})
        with self.assertRaises(ValueError):
            self.places.client.call('maps_schema_take_taxi', {})

    def test_detail_city_and_reverse_municipality_never_expose_coordinates(self):
        self.structured = True
        self.responses = {'maps_text_search': {'pois': [{'name': '起点', 'id': 'poi-1'}]},
                          'maps_search_detail': {'data': {'id': 'poi-1', 'address': '某路一号', 'location': '9,9'}},
                          'maps_regeocode': {'regeocode': {'addressComponent': {'city': [], 'province': '北京市'}}}}
        self.assertEqual(self.places.nationwide('起点'), [{'name': '起点', 'city': '北京市', 'address': '某路一号'}])
        self.assertEqual([t['name'] for t in self.tool_calls()], ['maps_text_search', 'maps_search_detail', 'maps_regeocode'])

    def test_empty_poi_falls_back_to_address_geocoding(self):
        self.responses = {'maps_text_search': {'pois': []},
                          'maps_geo': {'geocodes': [{'formatted_address': '浙江省杭州市某小区一号',
                                                   'city': '杭州市', 'location': '9,9'}]}}
        result = self.places.nationwide('某小区一号')
        self.assertEqual(result[0]['city'], '杭州市')
        self.assertEqual(self.tool_calls()[-1], {'name': 'maps_geo', 'arguments': {'address': '某小区一号'}})

    def test_unresolved_candidates_not_dropped_and_extra_calls_bounded(self):
        self.responses = {'maps_text_search': {'pois': [{'name': '某店', 'id': str(i)} for i in range(12)]},
                          'maps_search_detail': {'city': ''}}
        result = self.places.nationwide('某店')
        self.assertEqual(len(result), 12)
        self.assertEqual(len(self.tool_calls()), 6)
        self.assertTrue(all(p['city'] == '' for p in result))

    def test_unknown_city_does_not_use_default_or_create_didi_points(self):
        self.settings.save('上海市')
        self.responses = {'maps_text_search': {'pois': [{'name': '某店', 'cityname': '北京市'}, {'name': '某店'}]}}
        with StoreContext() as store:
            didi = FakeDidi()
            agent = Agent(didi, ScriptLLM(), store, places=self.places)
            state = {'points': []}
            result = agent.execute(state, 'search_places', {'keywords': '某店'})
            self.assertTrue(result['requires_address_choice'])
            self.assertEqual(len(result['candidates']), 2)
            self.assertEqual(didi.calls, [])
            self.assertEqual(state['points'], [])

    def test_changed_key_reconnects_and_clear_does_not_reuse_old_client(self):
        self.places.nationwide('起点')
        self.settings.save('', 'replacement-key')
        self.places.nationwide('起点')
        self.assertEqual(sum(b['method'] == 'initialize' for _, b, _ in self.calls), 2)
        self.assertIn('replacement-key', self.calls[-1][0])
        self.settings.save('', '')
        count = len(self.calls)
        with self.assertRaises(ValueError):
            self.places.nationwide('起点')
        self.assertEqual(len(self.calls), count)
        self.assertIsNone(self.places.client)

    def test_legacy_ditu_key_is_not_used_and_explicit_clear_overrides_env(self):
        self.settings.path.write_text(json.dumps({'default_city': '北京市', 'map_key': 'legacy-secret'}), encoding='utf-8')
        with patch.dict('os.environ', {'DIDI_MAP_KEY': 'legacy-env'}):
            self.assertEqual(self.settings.get(), {'default_city': '北京市', 'amap_key': ''})
            with self.assertRaises(ValueError):
                self.places.nationwide('起点')
        with patch.dict('os.environ', {'AMAP_API_KEY': 'env-key'}):
            self.assertEqual(self.settings.get()['amap_key'], 'env-key')
            self.settings.save('北京市', '')
            self.assertEqual(Settings(self.settings.path).get()['amap_key'], '')
        self.assertEqual(self.calls, [])

    def test_failed_call_redacts_errors_does_not_retry_and_resets_connection(self):
        self.responses['maps_text_search'] = ValueError('https://example?key=SECRET')
        with self.assertRaises(ValueError) as error:
            self.places.nationwide('起点')
        self.assertNotIn('SECRET', str(error.exception))
        self.assertEqual(len(self.tool_calls()), 1)
        self.assertIsNone(self.places.client)

    def test_provider_error_is_not_a_place(self):
        for response in ({'status': '0', 'info': 'PRIVATE'}, {'error': 'PRIVATE'}):
            self.responses['maps_text_search'] = response
            with self.assertRaises(ValueError) as error:
                self.places.nationwide('起点')
            self.assertNotIn('PRIVATE', str(error.exception))

    def test_city_free_production_flow_delivered_confirmation_query_eta_and_cancel(self):
        with StoreContext() as store:
            didi = FakeDidi('production')
            llm = ScriptLLM([[('search_places', {'keywords': '起点'}), ('search_places', {'keywords': '终点'})],
                             [('taxi_estimate', ROUTE)], [('prepare_ride', {'product_name': '测试快车'})]])
            agent = Agent(didi, llm, store, places=self.places)
            card = agent.respond('wx:user', '从起点到终点，叫测试快车', require_delivery=True)
            self.assertIn('真实叫车确认', card)
            self.assertIn('起点', card)
            self.assertIn('终点', card)
            self.assertNotIn('productCategory', card)
            self.assertFalse(any(n == 'taxi_create_order' for n, _ in didi.calls))
            agent.respond('wx:user', '确认', require_delivery=True)
            self.assertFalse(any(n == 'taxi_create_order' for n, _ in didi.calls))
            agent.mark_confirmation_presented('wx:user', card)
            self.assertIn('订单已创建', agent.respond('wx:user', '确认'))
            agent.respond('wx:user', '确认')
            self.assertEqual(sum(n == 'taxi_create_order' for n, _ in didi.calls), 1)
            didi.order.update(statusCode=1, statusText='司机已接单', map={'eta': 8})
            llm.steps = [[('taxi_query_order', {})], []]
            self.assertIn('约 8 分钟', agent.respond('wx:user', '司机多久到？'))
            llm.steps = [[('prepare_cancel', {})]]
            agent.respond('wx:user', '取消订单')
            pending = store.get('session:wx:user')['pending']
            self.assertIn('取消', agent.respond('wx:user', '确认取消 ' + pending['code']))
            self.assertEqual(sum(n == 'taxi_cancel_order' for n, _ in didi.calls), 1)


class StoreContext:
    def __enter__(self):
        self.store = Store(':memory:')
        return self.store

    def __exit__(self, *exc):
        self.store.db.close()


if __name__ == '__main__':
    unittest.main()
