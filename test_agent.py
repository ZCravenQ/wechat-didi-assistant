"""仅使用本地模拟，不请求网络，不产生订单、消息或模型费用。"""
import io
import json
import os
import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from agent import Agent, Didi, LLM, MCPError, Store, encode, request_json
from wechat import Wechat, official_base, process_message
from main import runner_lock


class FakeDidi:
    def __init__(self, env='sandbox'):
        self.env = env
        self.tools = [{'name': name, 'description': 'mock', 'inputSchema': {'type': 'object', 'properties': {}}}
                      for name in ['maps_textsearch', 'taxi_estimate', 'taxi_generate_ride_app_link',
                                   'maps_direction_driving', 'taxi_query_order', 'taxi_create_order', 'taxi_cancel_order']]
        self.calls = []
        self.fail_create = False
        self.fail_cancel = False
        self.order = {}
        self.query_result = None
        self.cancel_result = {'success': True}

    def call(self, name, args, *, confirmed=False):
        if name in {'taxi_create_order', 'taxi_cancel_order'} and not confirmed:
            raise ValueError('unconfirmed')
        self.calls.append((name, args))
        if name == 'maps_textsearch':
            lng = 116.1 if args['keywords'] == '起点' else 116.2
            return {'content': [{'type': 'text', 'text': encode([{'display_name': args['keywords'],
                                                                'location': {'lng': lng, 'lat': 39.1}}])}]}
        if name == 'taxi_estimate':
            return {'structuredContent': {'traceId': 'trace-test', 'items': [
                {'productCategory': '1', 'productName': '测试快车', 'priceText': '25'}]}}
        if name == 'taxi_create_order':
            if self.fail_create:
                raise ValueError('timeout')
            self.order = {'orderId': 'mock-order', 'statusCode': 0, 'statusText': '匹配中'}
            return {'structuredContent': dict(self.order)}
        if name == 'taxi_query_order':
            if self.query_result is None and not args.get('order_id') and (
                    self.order.get('statusCode') == 5 or self.order.get('statusText') == '已取消'):
                return {'structuredContent': {}}
            return {'structuredContent': self.query_result if self.query_result is not None else dict(self.order)}
        if name == 'taxi_cancel_order':
            if self.fail_cancel:
                raise ValueError('timeout')
            if self.cancel_result.get('success') is True:
                self.order['statusText'] = '已取消'
            return {'structuredContent': self.cancel_result}
        if name == 'maps_direction_driving':
            return {'content': [{'type': 'text', 'text': '{"duration":1800,"unit":"seconds"}'}]}
        return {'content': [{'type': 'text', 'text': 'mock link'}]}


class ScriptLLM:
    mode = 'chat'

    def __init__(self, steps=None):
        self.steps = list(steps or [])
        self.histories = []
        self.memories = []

    def step(self, history, functions, memory=None):
        self.histories.append(json.loads(encode(history)))
        self.memories.append(json.loads(encode(memory)))
        calls = self.steps.pop(0) if self.steps else []
        if not calls:
            return [{'role': 'assistant', 'content': '这是本地测试结果。'}], [], '这是本地测试结果。'
        parsed = [{'id': f'call-{len(self.histories)}-{i}', 'name': name, 'arguments': encode(args)}
                  for i, (name, args) in enumerate(calls)]
        message = {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': c['id'], 'type': 'function', 'function': {'name': c['name'], 'arguments': c['arguments']}}
            for c in parsed]}
        return [message], parsed, ''

    tool_result = LLM.tool_result


ROUTE = {'from_lng': '116.1', 'from_lat': '39.1', 'from_name': '起点',
         'to_lng': '116.2', 'to_lat': '39.1', 'to_name': '终点'}


def scenario():
    return [[('maps_textsearch', {'keywords': '起点', 'city': '北京市'}),
             ('maps_textsearch', {'keywords': '终点', 'city': '北京市'})],
            [('taxi_estimate', ROUTE)], [('prepare_ride', {'product_category': '1'})], []]


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.didi = FakeDidi()
        self.llm = ScriptLLM(scenario())
        self.agent = Agent(self.didi, self.llm, self.store)

    def pending(self, session='a'):
        answer = self.agent.respond(session, '北京，从起点到终点，请选择测试快车叫车')
        return self.store.get('session:' + session)['pending'], answer

    def test_estimate_confirm_create_once(self):
        pending, answer = self.pending()
        self.assertIn('25 元', answer)
        self.assertNotIn('taxi_create_order', [name for name, _ in self.didi.calls])
        result = self.agent.respond('a', '确认叫车 ' + pending['code'])
        self.assertIn('mock-order', result)
        self.agent.respond('a', '确认叫车 ' + pending['code'])
        self.assertEqual(sum(n == 'taxi_create_order' for n, _ in self.didi.calls), 1)

    def test_foreign_session_and_wrong_code(self):
        pending, _ = self.pending()
        self.agent.respond('b', '确认叫车 ' + pending['code'])
        self.agent.respond('a', '确认叫车 FFFFFF' if pending['code'] != 'FFFFFF' else '确认叫车 000000')
        self.assertFalse(any(n == 'taxi_create_order' for n, _ in self.didi.calls))

    def test_expired_confirmation(self):
        pending, _ = self.pending()
        with patch('agent.time.time', return_value=pending['expires'] + 1):
            self.assertIn('过期', self.agent.respond('a', '确认叫车 ' + pending['code']))
        self.assertFalse(any(n == 'taxi_create_order' for n, _ in self.didi.calls))

    def test_uncertain_request_is_not_retried(self):
        pending, _ = self.pending()
        self.didi.fail_create = True
        self.agent.respond('a', '确认叫车 ' + pending['code'])
        self.agent.respond('a', '确认叫车 ' + pending['code'])
        self.assertEqual(sum(n == 'taxi_create_order' for n, _ in self.didi.calls), 1)

    def test_production_creates_only_after_exact_confirmation(self):
        self.didi.env = 'production'
        self.agent = Agent(self.didi, self.llm, self.store)
        pending, answer = self.pending()
        self.assertIn('真实叫车确认', answer)
        self.assertIn('回复“确认”下单', answer)
        self.assertNotIn(pending['code'], answer)
        self.assertNotIn('这是本地测试结果', answer)
        self.assertFalse(any(n == 'taxi_create_order' for n, _ in self.didi.calls))
        self.assertIn('订单已创建', self.agent.respond('a', '确认叫车 ' + pending['code']))
        self.agent.respond('a', '确认叫车 ' + pending['code'])
        self.assertEqual(sum(n == 'taxi_create_order' for n, _ in self.didi.calls), 1)

    def pending_error(self):
        answer = self.agent.respond('a', '北京，从起点到终点，请叫车')
        return None, answer

    def test_mutating_tools_are_not_exposed_and_cannot_be_injected(self):
        names = {f['name'] for f in self.agent.functions}
        self.assertNotIn('taxi_create_order', names)
        self.assertNotIn('taxi_cancel_order', names)
        with self.assertRaises(ValueError):
            self.agent.execute({}, 'taxi_create_order', {'estimate_trace_id': 'forged'})

    def test_coordinates_must_have_search_provenance(self):
        with self.assertRaises(ValueError):
            self.agent.execute({'points': []}, 'taxi_estimate', ROUTE)
        numeric = {**ROUTE, 'from_lng': 116.1}
        with self.assertRaises(ValueError):
            self.agent.execute({'points': [['116.1', '39.1'], ['116.2', '39.1']]}, 'taxi_estimate', numeric)

    def test_all_tool_results_are_returned_with_original_ids(self):
        self.pending()
        next_history = self.llm.histories[1]
        calls = next_history[1]['tool_calls']
        replies = next_history[2:4]
        self.assertEqual({c['id'] for c in calls}, {r['tool_call_id'] for r in replies})

    def test_new_message_invalidates_pending_even_if_model_fails(self):
        pending, _ = self.pending()
        with patch.object(self.llm, 'step', side_effect=ValueError('timeout')):
            with self.assertRaises(ValueError):
                self.agent.respond('a', '换个目的地')
        self.agent.respond('a', '确认叫车 ' + pending['code'])
        self.assertFalse(any(n == 'taxi_create_order' for n, _ in self.didi.calls))

    def create(self):
        pending, _ = self.pending()
        self.agent.respond('a', '确认叫车 ' + pending['code'])

    def cancel_pending(self):
        self.llm.steps = [[('prepare_cancel', {})], []]
        answer = self.agent.respond('a', '取消刚才的订单')
        return self.store.get('session:a').get('pending'), answer

    def test_cancel_queries_then_confirms_once(self):
        self.create()
        pending, answer = self.cancel_pending()
        self.assertIn('mock-order', answer)
        self.assertFalse(any(n == 'taxi_cancel_order' for n, _ in self.didi.calls))
        # 叫车确认码不能替代取消确认。
        self.agent.respond('a', '确认叫车 ' + pending['code'])
        self.assertFalse(any(n == 'taxi_cancel_order' for n, _ in self.didi.calls))
        self.assertIn('订单已取消', self.agent.respond('a', '确认取消 ' + pending['code']))
        self.agent.respond('a', '确认取消 ' + pending['code'])
        self.assertEqual(sum(n == 'taxi_cancel_order' for n, _ in self.didi.calls), 1)
        self.assertFalse(self.store.get(self.agent.account_key)['active'])

    def test_production_cancel(self):
        self.didi.env = 'production'
        self.agent = Agent(self.didi, self.llm, self.store)
        self.create()
        pending, answer = self.cancel_pending()
        self.assertIn('真实订单取消确认', answer)
        self.assertIn('订单已取消', self.agent.respond('a', '确认取消 ' + pending['code']))

    def test_cancel_rechecks_status_and_does_not_cancel_in_transit(self):
        self.create()
        pending, _ = self.cancel_pending()
        self.didi.order.update({'statusCode': 4, 'statusText': '行程中'})
        self.assertIn('未执行取消', self.agent.respond('a', '确认取消 ' + pending['code']))
        self.assertFalse(any(n == 'taxi_cancel_order' for n, _ in self.didi.calls))

    def test_cancel_requires_known_eligible_status(self):
        for status in [4, 5, 99, None]:
            self.didi.order = {'orderId': 'order', 'statusCode': status}
            with self.assertRaises(ValueError):
                self.agent.execute({}, 'prepare_cancel', {})

    def test_cancel_wrong_session_expiry_and_unknown_result(self):
        self.create()
        pending, _ = self.cancel_pending()
        self.agent.respond('b', '确认取消 ' + pending['code'])
        with patch('agent.time.time', return_value=pending['expires'] + 1):
            self.assertIn('过期', self.agent.respond('a', '确认取消 ' + pending['code']))
        self.assertFalse(any(n == 'taxi_cancel_order' for n, _ in self.didi.calls))
        self.didi.fail_cancel = True
        self.assertIn('结果不确定', self.agent.respond('a', '确认取消 ' + pending['code']))
        self.agent.respond('a', '确认取消 ' + pending['code'])
        self.assertEqual(sum(n == 'taxi_cancel_order' for n, _ in self.didi.calls), 1)
        self.assertEqual(self.store.get(self.agent.account_key)['mutation']['action'], 'cancel')

    def test_explicit_cancel_failure_is_not_reported_as_success(self):
        self.create()
        pending, _ = self.cancel_pending()
        self.didi.cancel_result = {'success': False}
        answer = self.agent.respond('a', '确认取消 ' + pending['code'])
        self.assertIn('取消未成功', answer)
        self.assertNotIn('订单已取消', answer)
        self.assertTrue(self.store.get(self.agent.account_key)['active'])

    def test_uncertain_create_blocks_new_order_across_reset_and_sessions(self):
        pending, _ = self.pending()
        self.didi.fail_create = True
        self.agent.respond('a', '确认叫车 ' + pending['code'])
        self.agent.respond('a', '/reset')
        self.llm.steps = scenario()
        answer = self.agent.respond('b', '从起点到终点，叫车')
        self.assertIsNone(self.store.get('session:b').get('pending'))
        self.assertEqual(sum(n == 'taxi_create_order' for n, _ in self.didi.calls), 1)
        # 没有 orderId 的响应不能清除不确定操作。
        self.agent.execute({}, 'taxi_query_order', {})
        self.assertIn('mutation', self.store.get(self.agent.account_key))

    def test_query_reconciles_unknown_create_but_active_order_still_blocks_new(self):
        pending, _ = self.pending()
        self.didi.fail_create = True
        self.agent.respond('a', '确认叫车 ' + pending['code'])
        self.didi.order = {'orderId': 'found-order', 'statusCode': 1, 'statusText': '司机已接单'}
        self.agent.execute({}, 'taxi_query_order', {})
        account = self.store.get(self.agent.account_key)
        self.assertNotIn('mutation', account)
        self.assertTrue(account['active'])

    def test_preflight_blocks_order_created_in_app(self):
        pending, _ = self.pending()
        self.didi.order = {'orderId': 'app-order', 'statusCode': 1}
        self.assertIn('未再次下单', self.agent.respond('a', '确认叫车 ' + pending['code']))
        self.assertFalse(any(n == 'taxi_create_order' for n, _ in self.didi.calls))

    def test_eta_is_fresh_and_stage_specific(self):
        for code, label in [(1, '上车点'), (4, '目的地')]:
            self.didi.order = {'orderId': 'mock-order', 'statusCode': code, 'map': {'eta': '7'}}
            self.llm.steps = [[('taxi_query_order', {})], []]
            answer = self.agent.respond('a', '预计多久到？')
            self.assertIn(label, answer)
            self.assertIn('7 分钟', answer)
            tool = self.llm.histories[-1][-1]
            self.assertIn('7', tool['content'])
        self.didi.order = {'orderId': 'mock-order', 'statusCode': 1}
        result = self.agent.execute({}, 'taxi_query_order', {})
        self.assertIn('暂未提供预计', result['order_summary'])

    def test_driving_route_requires_search_coordinates(self):
        args = {'origin': '116.1,39.1', 'destination': '116.2,39.1'}
        with self.assertRaises(ValueError):
            self.agent.execute({}, 'maps_direction_driving', args)
        result = self.agent.execute({'points': [['116.1', '39.1'], ['116.2', '39.1']]}, 'maps_direction_driving', args)
        self.assertIn('1800', result['content'][0]['text'])

    def test_missing_create_order_id_remains_uncertain(self):
        pending, _ = self.pending()
        original = self.didi.call
        def call(name, args, **kwargs):
            if name == 'taxi_create_order':
                return {'content': [{'type': 'text', 'text': 'ok'}]}
            return original(name, args, **kwargs)
        with patch.object(self.didi, 'call', side_effect=call):
            self.assertIn('结果不确定', self.agent.respond('a', '确认叫车 ' + pending['code']))
        self.assertIn('mutation', self.store.get(self.agent.account_key))

    def test_restart_preserves_uncertain_action(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'state.sqlite'
            store = Store(path)
            agent = Agent(self.didi, ScriptLLM(scenario()), store)
            agent.respond('a', '叫车')
            code = store.get('session:a')['pending']['code']
            self.didi.fail_create = True
            agent.respond('a', '确认叫车 ' + code)
            store.db.close()
            reopened = Store(path)
            agent = Agent(self.didi, ScriptLLM(scenario()), reopened)
            agent.respond('a', '确认叫车 ' + code)
            self.assertIn('mutation', reopened.get(agent.account_key))
            self.assertEqual(sum(n == 'taxi_create_order' for n, _ in self.didi.calls), 1)
            reopened.db.close()

    def test_atomic_claim_can_only_consume_pending_once(self):
        pending, _ = self.pending()
        self.assertIsNotNone(self.store.claim_action('session:a', pending, self.agent.account_key))
        self.assertIsNone(self.store.claim_action('session:a', pending, self.agent.account_key))

    def test_historical_query_cannot_replace_active_order(self):
        self.create()
        self.didi.query_result = {'orderId': 'old', 'statusCode': 5}
        self.agent.execute({}, 'taxi_query_order', {'order_id': 'old'})
        self.assertEqual(self.store.get(self.agent.account_key)['order']['orderId'], 'mock-order')
        self.assertTrue(self.store.get(self.agent.account_key)['active'])

    def test_changing_didi_key_invalidates_confirmations_and_isolates_ledger(self):
        pending, _ = self.pending()
        self.didi.identity = 'another-key'
        changed = Agent(self.didi, ScriptLLM(), self.store)
        changed.respond('a', '确认叫车 ' + pending['code'])
        self.assertFalse(any(n == 'taxi_create_order' for n, _ in self.didi.calls))
        self.assertNotEqual(changed.account_key, self.agent.account_key)

    def test_changing_environment_invalidates_confirmation(self):
        pending, _ = self.pending()
        self.didi.env = 'production'
        changed = Agent(self.didi, ScriptLLM(), self.store)
        changed.respond('a', '确认叫车 ' + pending['code'])
        self.assertFalse(any(n == 'taxi_create_order' for n, _ in self.didi.calls))

    def test_plain_confirmation_creates_once_and_card_is_concise(self):
        pending, answer = self.pending()
        self.assertEqual(answer.count('出发地：'), 1)
        self.assertIn('目的地：终点', answer)
        self.assertIn('车型：测试快车', answer)
        self.assertIn('预估价：25 元', answer)
        self.assertNotIn(pending['code'], answer)
        self.assertEqual(len(answer.splitlines()), 6)
        self.assertIn('订单已创建', self.agent.respond('a', '确认'))
        self.agent.respond('a', '确认')
        self.assertEqual(sum(n == 'taxi_create_order' for n, _ in self.didi.calls), 1)

    def test_plain_confirmation_requires_valid_current_card(self):
        self.agent.respond('a', '确认')
        self.assertFalse(any(n == 'taxi_create_order' for n, _ in self.didi.calls))
        pending, _ = self.pending()
        self.agent.respond('b', '确认')
        with patch('agent.time.time', return_value=pending['expires'] + 1):
            self.assertIn('过期', self.agent.respond('a', '确认'))
        self.assertFalse(any(n == 'taxi_create_order' for n, _ in self.didi.calls))

    def test_plain_confirmation_does_not_cancel(self):
        self.create()
        self.cancel_pending()
        self.agent.respond('a', '确认')
        self.assertFalse(any(n == 'taxi_cancel_order' for n, _ in self.didi.calls))

    def test_undelivered_card_cannot_authorize_order(self):
        answer = self.agent.respond('a', '北京，从起点到终点，叫车', require_delivery=True)
        self.assertIn('尚未确认送达', self.agent.respond('a', '确认', require_delivery=True))
        self.assertFalse(any(n == 'taxi_create_order' for n, _ in self.didi.calls))
        self.agent.mark_confirmation_presented('a', answer)
        self.assertIn('订单已创建', self.agent.respond('a', '确认', require_delivery=True))

    def test_confirmation_before_card_is_not_authorization(self):
        self.pending()
        sent_at = self.store.get('session:a')['pending']['presented_at']
        self.assertIn('早于', self.agent.respond('a', '确认', message_time=sent_at - 1))
        self.assertFalse(any(n == 'taxi_create_order' for n, _ in self.didi.calls))

    def test_changed_route_invalidates_plain_confirmation(self):
        self.pending()
        self.agent.respond('a', '先不叫车了，改目的地')
        self.agent.respond('a', '确认')
        self.assertFalse(any(n == 'taxi_create_order' for n, _ in self.didi.calls))

    def test_new_confirmation_after_failed_create_cannot_repeat_order(self):
        self.pending()
        self.didi.fail_create = True
        self.assertIn('结果不确定', self.agent.respond('a', '确认'))
        self.agent.respond('a', '确认')
        self.assertEqual(sum(n == 'taxi_create_order' for n, _ in self.didi.calls), 1)

    def test_cancellation_uncertainty_clears_only_when_target_is_terminal(self):
        self.create()
        pending, _ = self.cancel_pending()
        self.didi.fail_cancel = True
        self.agent.respond('a', '确认取消 ' + pending['code'])
        self.agent.execute({}, 'taxi_query_order', {})
        self.assertIn('mutation', self.store.get(self.agent.account_key))
        self.didi.order['statusText'] = '已取消'
        self.agent.execute({}, 'taxi_query_order', {})
        self.assertNotIn('mutation', self.store.get(self.agent.account_key))

    def test_query_uses_saved_order_id_to_observe_completion(self):
        self.create()
        self.didi.order.update({'statusCode': 5, 'statusText': '行程完成'})
        self.agent.execute({}, 'taxi_query_order', {})
        self.assertEqual(self.didi.calls[-1], ('taxi_query_order', {'order_id': 'mock-order'}))
        self.assertFalse(self.store.get(self.agent.account_key)['active'])

    def test_definitive_expired_quote_error_requires_new_confirmation(self):
        pending, _ = self.pending()
        original = self.didi.call
        def call(name, args, **kwargs):
            if name == 'taxi_create_order':
                raise MCPError(-32021)
            return original(name, args, **kwargs)
        with patch.object(self.didi, 'call', side_effect=call):
            self.assertIn('明确拒绝', self.agent.respond('a', '确认叫车 ' + pending['code']))
        self.assertNotIn('mutation', self.store.get(self.agent.account_key))
        self.assertIsNone(self.store.get('session:a').get('pending'))


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.client = Wechat({'bot_token': 'TEST', 'ilink_bot_id': 'bot', 'ilink_user_id': 'owner'})
        self.msg = {'message_type': 1, 'message_id': 1, 'from_user_id': 'owner', 'context_token': 'CTX',
                    'item_list': [{'type': 1, 'text_item': {'text': '查价'}}]}
        self.calls = []

        class Echo:
            def respond(inner, session, text):
                self.calls.append((session, text))
                return 'reply'
        self.agent = Echo()

    def test_duplicate_messages_do_not_run_llm_twice(self):
        with patch.object(self.client, 'send') as send:
            self.assertEqual(process_message(self.client, self.agent, self.store, self.msg), 'delivered')
            self.assertEqual(process_message(self.client, self.agent, self.store, self.msg), 'duplicate')
            self.assertEqual(send.call_count, 1)
        self.assertEqual(len(self.calls), 1)

    def test_unauthorized_user_and_group_are_ignored(self):
        self.assertEqual(process_message(self.client, self.agent, self.store, {**self.msg, 'from_user_id': 'other'}), 'ignored')
        self.assertEqual(process_message(self.client, self.agent, self.store, {**self.msg, 'group_id': 'group'}), 'ignored')
        self.assertEqual(len(self.calls), 0)

    def test_reply_preserves_context_token(self):
        with patch('wechat.request_json', return_value=({'ret': 0}, {})) as request:
            self.client.send(self.msg, 'reply')
            self.assertEqual(request.call_args.args[1]['msg']['context_token'], 'CTX')
            self.assertEqual(request.call_args.args[1]['msg']['to_user_id'], 'owner')

    def test_unknown_delivery_does_not_repeat_side_effects(self):
        with patch.object(self.client, 'send', side_effect=ValueError('timeout')):
            self.assertEqual(process_message(self.client, self.agent, self.store, self.msg), 'delivery_unknown')
            self.assertEqual(process_message(self.client, self.agent, self.store, self.msg), 'duplicate')
        self.assertEqual(len(self.calls), 1)

    def test_cannot_send_without_context(self):
        with self.assertRaises(ValueError):
            self.client.send({**self.msg, 'context_token': ''}, 'reply')

    def test_login_endpoint_validation(self):
        self.assertEqual(official_base('https://ilinkai.weixin.qq.com'), 'https://ilinkai.weixin.qq.com')
        for bad in ['http://ilinkai.weixin.qq.com', 'https://weixin.qq.com.evil.example', 'https://evil.example',
                    'https://user:secret@ilinkai.weixin.qq.com']:
            with self.assertRaises(ValueError):
                official_base(bad)

    def test_wechat_production_create_query_cancel_and_duplicate_confirmations(self):
        didi = FakeDidi('production')
        llm = ScriptLLM(scenario())
        agent = Agent(didi, llm, self.store)
        def message(identifier, text):
            return {**self.msg, 'message_id': identifier,
                    'item_list': [{'type': 1, 'text_item': {'text': text}}]}
        with patch.object(self.client, 'send') as send:
            process_message(self.client, agent, self.store, message(10, '从起点到终点，快车叫车'))
            pending = self.store.get('session:bot:owner')['pending']
            self.assertIn('真实叫车确认', send.call_args.args[1])
            confirm = message(11, '确认')
            process_message(self.client, agent, self.store, confirm)
            self.assertIn('订单已创建', send.call_args.args[1])
            process_message(self.client, agent, self.store, confirm)
            process_message(self.client, agent, self.store, message(12, '确认'))
            self.assertEqual(sum(n == 'taxi_create_order' for n, _ in didi.calls), 1)
            didi.order.update({'statusCode': 1, 'map': {'eta': '6'}})
            llm.steps = [[('taxi_query_order', {})], []]
            process_message(self.client, agent, self.store, message(13, '司机还有多久到？'))
            self.assertIn('6 分钟', send.call_args.args[1])
            llm.steps = [[('prepare_cancel', {})], []]
            process_message(self.client, agent, self.store, message(14, '取消订单'))
            pending = self.store.get('session:bot:owner')['pending']
            self.assertIn('真实订单取消确认', send.call_args.args[1])
            cancel = message(15, '确认取消 ' + pending['code'])
            process_message(self.client, agent, self.store, cancel)
            self.assertIn('订单已取消', send.call_args.args[1])
            process_message(self.client, agent, self.store, cancel)
            self.assertEqual(sum(n == 'taxi_cancel_order' for n, _ in didi.calls), 1)

    def test_wechat_confirmation_from_same_received_batch_is_rejected(self):
        didi = FakeDidi('production')
        agent = Agent(didi, ScriptLLM(scenario()), self.store)
        received = 1.0
        with patch.object(self.client, 'send') as send:
            process_message(self.client, agent, self.store, {**self.msg, 'message_id': 30}, received_at=received)
            confirm = {**self.msg, 'message_id': 31,
                       'item_list': [{'type': 1, 'text_item': {'text': '确认'}}]}
            process_message(self.client, agent, self.store, confirm, received_at=received)
            self.assertIn('早于', send.call_args.args[1])
        self.assertFalse(any(n == 'taxi_create_order' for n, _ in didi.calls))

    def test_failed_wechat_card_delivery_leaves_order_unconfirmed(self):
        didi = FakeDidi('production')
        agent = Agent(didi, ScriptLLM(scenario()), self.store)
        with patch.object(self.client, 'send', side_effect=ValueError('timeout')):
            process_message(self.client, agent, self.store, self.msg)
        with patch.object(self.client, 'send') as send:
            confirm = {**self.msg, 'message_id': 2,
                       'item_list': [{'type': 1, 'text_item': {'text': '确认'}}]}
            process_message(self.client, agent, self.store, confirm)
            self.assertIn('尚未确认送达', send.call_args.args[1])
        self.assertFalse(any(n == 'taxi_create_order' for n, _ in didi.calls))

    def test_runner_lock_blocks_second_process_and_releases(self):
        with TemporaryDirectory() as directory, patch('main.RUNTIME', Path(directory)):
            with runner_lock():
                with self.assertRaises(ValueError):
                    with runner_lock():
                        pass
            with runner_lock():
                pass


class WireTests(unittest.TestCase):
    def test_production_mcp_mutations_require_explicit_confirmed_path(self):
        responses = [({'id': 1, 'result': {}}, {}), ({}, {}),
                     ({'id': 3, 'result': {'tools': [{'name': 'taxi_create_order'}, {'name': 'taxi_cancel_order'}]}}, {})]
        with patch.dict(os.environ, {'DIDI_MCP_KEY': 'TEST', 'DIDI_ENV': 'production'}), \
                patch('agent.request_json', side_effect=responses) as request:
            client = Didi()
        self.assertNotIn('mcp-servers-sandbox', request.call_args.args[0])
        for name in ['taxi_create_order', 'taxi_cancel_order']:
            with self.assertRaises(ValueError):
                client.call(name, {})
            with patch.object(client, 'rpc', return_value={'structuredContent': {'ok': True}}) as rpc:
                client.call(name, {}, confirmed=True)
                self.assertEqual(rpc.call_args.args[0], 'tools/call')

    def test_mcp_session_negotiation(self):
        responses = [({'id': 1, 'result': {'protocolVersion': '2024-11-05'}}, {'Mcp-Session-Id': 'session'}),
                     ({}, {}), ({'id': 3, 'result': {'tools': []}}, {})]
        with patch.dict(os.environ, {'DIDI_MCP_KEY': 'TEST', 'DIDI_ENV': 'sandbox'}), \
                patch('agent.request_json', side_effect=responses) as request:
            client = Didi()
        self.assertEqual(client.protocol, '2024-11-05')
        self.assertIn('mcp-servers-sandbox', request.call_args.args[0])
        self.assertEqual(request.call_args.args[2]['Mcp-Session-Id'], 'session')

    def test_sse_ignores_notifications_and_matches_response_id(self):
        class Response(io.BytesIO):
            headers = {'Content-Type': 'text/event-stream'}
        data = b'data: {"method":"notifications/progress"}\n\ndata: {"id":7,"result":{"ok":true}}\n\n'
        class Opener:
            def open(self, req, timeout):
                return Response(data)
        with patch('urllib.request.build_opener', return_value=Opener()):
            result, _ = request_json('https://test.example/mcp', {'id': 7}, rpc_id=7)
        self.assertTrue(result['result']['ok'])

    def test_chat_adapter_preserves_tool_call_and_reasoning(self):
        config = {'LLM_BASE_URL': 'https://test.example/v1', 'LLM_API_KEY': 'TEST', 'LLM_MODEL': 'test',
                  'LLM_API_MODE': 'chat', 'LLM_EXTRA_BODY': '{}'}
        message = {'role': 'assistant', 'content': None, 'reasoning_content': 'reasoning',
                   'tool_calls': [{'id': 'c1', 'type': 'function',
                                   'function': {'name': 'maps_textsearch', 'arguments': '{}'}}]}
        with patch.dict(os.environ, config), patch('agent.request_json', return_value=({'choices': [{'message': message}]}, {})):
            llm = LLM()
            saved, calls, _ = llm.step([], [])
        self.assertEqual(saved[0]['reasoning_content'], 'reasoning')
        self.assertEqual(llm.tool_result(calls[0]['id'], {})['tool_call_id'], 'c1')

    def test_responses_adapter_preserves_reasoning_and_call_id(self):
        config = {'LLM_BASE_URL': 'https://test.example/v1', 'LLM_API_KEY': 'TEST', 'LLM_MODEL': 'test',
                  'LLM_API_MODE': 'responses', 'LLM_EXTRA_BODY': '{}'}
        output = [{'type': 'reasoning', 'id': 'r1', 'summary': []},
                  {'type': 'function_call', 'call_id': 'c1', 'name': 'maps_textsearch', 'arguments': '{}'}]
        with patch.dict(os.environ, config), patch('agent.request_json', return_value=({'output': output}, {})):
            llm = LLM()
            saved, calls, _ = llm.step([], [])
        self.assertEqual(saved, output)
        self.assertEqual(llm.tool_result(calls[0]['id'], {})['call_id'], 'c1')


if __name__ == '__main__':
    unittest.main(verbosity=2)
