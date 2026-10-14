"""仅绑定回环地址的本地管理界面；不提供下单/取消订单入口。"""
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import threading
from urllib.parse import parse_qs, urlsplit

from agent import RUNTIME, Store
from place_search import Settings


class Admin:
    def __init__(self, memory, *, agent=None, settings=None, runtime=None, mode='ui'):
        self.memory, self.agent = memory, agent
        self.settings = settings or Settings()
        self.runtime = Path(runtime or RUNTIME)
        self.mode = mode
        self.lock = agent.lock if agent else threading.RLock()

    def sessions(self):
        sessions = {'console'}
        path = self.runtime / 'wechat-auth.json'
        if path.exists():
            auth = json.loads(path.read_text(encoding='utf-8'))
            if auth.get('ilink_bot_id') and auth.get('ilink_user_id'):
                sessions.add(auth['ilink_bot_id'] + ':' + auth['ilink_user_id'])
        if (self.runtime / 'state.sqlite').exists():
            with self.store() as store:
                sessions.update(key[len('session:'):] for key, in store.db.execute(
                    "SELECT key FROM state WHERE key LIKE 'session:%'"))
        return {hashlib.sha256(s.encode()).hexdigest(): s for s in sorted(sessions)}

    @contextmanager
    def store(self):
        self.runtime.mkdir(parents=True, exist_ok=True)
        store = Store(self.runtime / 'state.sqlite')
        try:
            yield store
        finally:
            store.db.close()

    def snapshot(self):
        settings = self.settings.get()
        with self.store() as store:
            accounts = [json.loads(value) for value, in store.db.execute(
                "SELECT value FROM state WHERE key LIKE 'account:%'")]
        return {'mode': self.mode, 'environment': self.agent.didi.env if self.agent else os.environ.get('DIDI_ENV', 'sandbox'),
                'model': os.environ.get('LLM_MODEL', '未设置'),
                'configured': {name: bool(os.environ.get(name)) and os.environ.get(name) != 'replace-me'
                               for name in ('LLM_API_KEY', 'DIDI_MCP_KEY')},
                'default_city': settings['default_city'], 'amap_key_configured': bool(settings['amap_key']) and settings['amap_key'] != 'replace-me',
                'profiles': [{'id': key, 'label': '控制台' if value == 'console' else '微信用户 ' + key[:8]}
                             for key, value in self.sessions().items()],
                'orders': [{'active': bool(a.get('active')), 'uncertain': bool(a.get('mutation')),
                            'status': a.get('order', {}).get('statusText', '未记录订单')}
                           for a in accounts]}

    def session(self, ident):
        session = self.sessions().get(ident)
        if session is None:
            raise ValueError('请选择已有用户')
        return session

    def api(self, path, data):
        if path == '/api/status':
            return self.snapshot()
        if path == '/api/settings':
            self.settings.save(data.get('default_city', ''), data.get('amap_key'))
            return {'message': '设置已保存，下一次地点查询生效。'}
        if path not in {'/api/memories', '/api/memory/save', '/api/memory/delete'}:
            raise ValueError('未提供此管理操作')
        with self.lock:
            session = self.session(data.get('profile'))
            if path == '/api/memories':
                return {'items': self.memory.list(session)}
            if path == '/api/memory/save':
                args = {k: data.get(k, '') for k in ('kind', 'name', 'value', 'city')}
                evidence = '保存 ' + args['name'] + ' ' + args['city'] + ' ' + args['value']
                self.memory.save(session, {**args, 'evidence': evidence}, evidence)
            else:
                evidence = '删除 ' + str(data.get('name', ''))
                self.memory.delete(session, {**data, 'evidence': evidence}, evidence)
            # 管理员编辑后移除同一记忆所有关联会话的旧上下文及旧确认。
            with self.store() as store:
                for candidate in self.sessions().values():
                    if self.memory.owner(candidate) == self.memory.owner(session):
                        key = 'session:' + candidate
                        state = store.get(key, {})
                        state.update({'turns': [], 'points': []})
                        state.pop('pending', None)
                        state.pop('quote', None)
                        store.put(key, state)
            return {'message': '记忆已保存。' if path.endswith('/save') else '记忆已删除。'}


def make_server(admin, port):
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError('管理端口应在 1024–65535 之间')
    token = secrets.token_urlsafe(32)
    allowed_hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
    allowed_origins = {'http://' + host for host in allowed_hosts}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # 不记录地址、记忆和 Key。

        def send(self, status, data, html=False):
            raw = data.encode('utf-8') if html else json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', ('text/html' if html else 'application/json') + '; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', f"default-src 'self'; script-src 'nonce-{token}'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def dispatch(self, posting=False):
            if self.headers.get('Host') not in allowed_hosts:
                return self.send(403, {'error': '仅允许本机访问'})
            path = urlsplit(self.path)
            if path.path == '/' and not posting:
                html = (Path(__file__).parent / 'admin.html').read_text(encoding='utf-8').replace('__TOKEN__', token)
                return self.send(200, html, True)
            if (self.headers.get('X-Admin-Token') != token or
                    self.headers.get('Origin', next(iter(allowed_origins))) not in allowed_origins):
                return self.send(403, {'error': '请从本机管理页面操作'})
            try:
                if posting:
                    if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                        raise ValueError('只接受 JSON 请求')
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 16384:
                        raise ValueError('请求大小无效')
                    data = json.loads(self.rfile.read(length))
                    if not isinstance(data, dict):
                        raise ValueError('请求必须是对象')
                    if path.path not in {'/api/settings', '/api/memory/save', '/api/memory/delete'}:
                        raise ValueError('未提供此管理操作')
                else:
                    if path.path not in {'/api/status', '/api/memories'}:
                        return self.send(404, {'error': '页面不存在'})
                    data = {k: v[0] for k, v in parse_qs(path.query).items()}
                self.send(200, admin.api(path.path, data))
            except (ValueError, KeyError, TypeError):
                self.send(400, {'error': '操作未完成。请检查输入或记忆组件；必要时刷新页面核对结果。'})
            except Exception:
                self.send(500, {'error': '管理操作失败，请检查本地服务；未打印凭证。'})

        def do_GET(self):
            self.dispatch()

        def do_POST(self):
            self.dispatch(True)

    class LocalServer(HTTPServer):
        allow_reuse_address = False

        def server_bind(self):
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

    try:
        server = LocalServer(('127.0.0.1', port), Handler)
    except OSError:
        raise ValueError(f'本地管理端口 {port} 被占用或不可用；请用 --ui-port 更换端口。') from None
    server.timeout = 1
    return server


@contextmanager
def local_ui(admin, port):
    server = make_server(admin, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f'本地管理界面：http://127.0.0.1:{port} （仅本机访问）')
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
