"""参照腾讯公开 iLink 客户端源码的文本消息适配器；需实际账号验证。"""
import base64
import hashlib
import json
import os
import secrets
import time
from urllib.parse import urlencode, urlsplit

from agent import RUNTIME, encode, request_json

AUTH_FILE = RUNTIME / 'wechat-auth.json'


def official_base(value):
    parsed = urlsplit(value)
    hostname = parsed.hostname or ''
    if (parsed.scheme != 'https' or not hostname.endswith('.weixin.qq.com') or
            parsed.username or parsed.password or parsed.port not in {None, 443} or parsed.query or parsed.fragment):
        raise ValueError('登录返回了未确认的微信服务地址，请停止并核对腾讯源码')
    return value.rstrip('/')


class Wechat:
    def __init__(self, auth=None):
        self.auth = auth or {}
        self.base = official_base(self.auth.get('baseurl') or 'https://ilinkai.weixin.qq.com')
        self.version = os.environ.get('WECHAT_CHANNEL_VERSION', '2.4.10')
        parts = [int(part) for part in self.version.split('.')]
        if len(parts) != 3 or any(part < 0 or part > 255 for part in parts):
            raise ValueError('WECHAT_CHANNEL_VERSION 必须是三个 0–255 的整数')
        self.version_number = str((parts[0] << 16) | (parts[1] << 8) | parts[2])

    def call(self, endpoint, body=None, poll=False, authenticated=True, base=None):
        headers = {'iLink-App-Id': 'bot', 'iLink-App-ClientVersion': self.version_number}
        if not poll:
            headers.update({'AuthorizationType': 'ilink_bot_token',
                            'X-WECHAT-UIN': base64.b64encode(str(secrets.randbits(32)).encode()).decode()})
        if authenticated:
            headers['Authorization'] = 'Bearer ' + self.auth['bot_token']
            body = {**(body or {}), 'base_info': {'channel_version': self.version,
                                               'bot_agent': 'WechatDidiPrototype/0.1.0'}}
        packet, _ = request_json((base or self.base) + '/ilink/bot/' + endpoint, body,
                                 headers, timeout=40 if poll or endpoint == 'getupdates' else 20)
        codes = [packet.get('ret', 0), packet.get('errcode', 0)]
        if -14 in codes:
            raise ValueError('微信会话已失效，请重新执行 login；不会继续自动重试')
        if any(codes):
            raise ValueError('微信接口返回业务错误；请检查账号状态')
        return packet

    def login(self):
        result = self.call('get_bot_qrcode?bot_type=3', {'local_token_list': []}, authenticated=False)
        qr = result.get('qrcode')
        image = result.get('qrcode_img_content')
        if not qr:
            raise ValueError('微信未返回二维码')
        print('使用自己的微信扫码并确认。二维码内容／图片地址：')
        print(image or qr)
        print('若返回内容不能显示二维码，请使用腾讯官方插件完成接入；本原型不猜测二维码格式。')
        verify_code = None
        deadline = time.time() + 240
        while time.time() < deadline:
            params = {'qrcode': qr}
            if verify_code:
                params['verify_code'] = verify_code
            result = self.call('get_qrcode_status?' + urlencode(params), poll=True, authenticated=False)
            status = result.get('status')
            if status == 'confirmed':
                if not all(result.get(key) for key in ('bot_token', 'ilink_bot_id', 'ilink_user_id')):
                    raise ValueError('授权结果缺少必要字段，不能启动消息服务')
                result['baseurl'] = official_base(result.get('baseurl') or self.base)
                RUNTIME.mkdir(parents=True, exist_ok=True)
                AUTH_FILE.write_text(encode({k: result[k] for k in ('bot_token', 'ilink_bot_id', 'ilink_user_id', 'baseurl')}), encoding='utf-8')
                try:
                    AUTH_FILE.chmod(0o600)
                except OSError:
                    pass
                print('授权凭证已保存到运行数据目录，未打印凭证。')
                return
            if status in {'expired', 'verify_code_blocked', 'binded_redirect'}:
                raise ValueError('二维码过期、验证被阻止或账号已绑定，请查看微信提示后重新操作')
            if status == 'need_verifycode':
                verify_code = input('输入微信上显示的验证码：').strip()
            if status == 'scaned_but_redirect':
                host = result.get('redirect_host')
                if not host:
                    raise ValueError('登录重定向缺少服务地址')
                self.base = official_base('https://' + host)
            time.sleep(1)
        raise ValueError('扫码授权超时，请重新执行 login')

    def send(self, incoming, text):
        token = incoming.get('context_token')
        if not token:
            raise ValueError('缺少微信回复上下文，不能猜测或替代 context_token')
        return self.call('sendmessage', {'msg': {
            'from_user_id': '', 'to_user_id': incoming['from_user_id'],
            'client_id': secrets.token_hex(16), 'message_type': 2, 'message_state': 2,
            'context_token': token, 'item_list': [{'type': 1, 'text_item': {'text': text}}]}})


def process_message(client, agent, store, incoming, *, received_at=None):
    """每条消息只执行一次 Agent。投递不确定时停留在待检查状态，不重放动作。"""
    owner = client.auth['ilink_user_id']
    if incoming.get('message_type') != 1 or incoming.get('from_user_id') != owner or incoming.get('group_id'):
        return 'ignored'
    text = '\n'.join(item.get('text_item', {}).get('text', '') for item in incoming.get('item_list', [])
                     if item.get('type') == 1).strip()
    if not text or not incoming.get('context_token'):
        return 'ignored'
    message_id = incoming.get('message_id')
    if message_id is None:
        # 仅用于缺少 message_id 的兼容情况；包含 seq/time，避免合并同文不同消息。
        message_id = hashlib.sha256(encode(incoming).encode()).hexdigest()
    key = 'inbox:' + client.auth['ilink_bot_id'] + ':' + owner + ':' + str(message_id)
    if store.get(key):
        return 'duplicate'
    store.put(key, {'status': 'processing', 'time': time.time()})
    session = client.auth['ilink_bot_id'] + ':' + owner
    try:
        if hasattr(agent, 'mark_confirmation_presented'):
            received = received_at if received_at is not None else time.time()
            timestamp = incoming.get('create_time_ms')
            message_time = min(timestamp / 1000, received) if isinstance(timestamp, (int, float)) and timestamp > 0 else received
            answer = agent.respond(session, text, require_delivery=True, message_time=message_time)
        else:
            answer = agent.respond(session, text)
    except Exception:
        # 不把 URL、密钥、协议响应正文或调用堆栈发给微信用户。
        answer = '本轮处理失败，没有自动重试。请检查服务配置后重新发送需求。'
    store.put(key, {'status': 'ready', 'answer': answer, 'time': time.time()})
    try:
        client.send(incoming, answer)
    except Exception:
        store.put(key, {'status': 'delivery_unknown', 'answer': answer, 'time': time.time()})
        return 'delivery_unknown'
    store.put(key, {'status': 'delivered', 'time': time.time()})
    if hasattr(agent, 'mark_confirmation_presented'):
        agent.mark_confirmation_presented(session, answer)
    return 'delivered'


def run_wechat(agent, store):
    if not AUTH_FILE.exists():
        raise ValueError('请先执行 login 完成微信扫码授权')
    client = Wechat(json.loads(AUTH_FILE.read_text(encoding='utf-8')))
    cursor_key = 'cursor:' + client.auth['ilink_bot_id']
    cursor = store.get(cursor_key, '')
    print(f'文本消息服务已启动，环境：{agent.didi.env}，仅处理扫码授权者的 ClawBot 私聊。')
    print('看过叫车确认单后回复“确认”下单；production 操作真实订单。长期记忆由官方 Memory Server 保存。')
    failures = 0
    while True:
        try:
            packet = client.call('getupdates', {'get_updates_buf': cursor})
            failures = 0
        except ValueError as error:
            # 失效立即停止；一般网络失败最多 3 次，不无限挂住。
            failures += 1
            if '失效' in str(error) or failures >= 3:
                raise
            time.sleep(2 ** failures)
            continue
        received_at = time.time()
        for incoming in packet.get('msgs', []):
            status = process_message(client, agent, store, incoming, received_at=received_at)
            if status == 'delivery_unknown':
                print('有回复投递结果不确定，请检查 .runtime 状态；未重放 Agent 或下单。')
        if packet.get('get_updates_buf'):
            cursor = packet['get_updates_buf']
            store.put(cursor_key, cursor)
        if not packet.get('msgs'):
            time.sleep(1)
