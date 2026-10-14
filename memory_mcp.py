"""对接官方 Memory Server 的薄适配层：存储及 MCP 均由官方组件负责。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time
from agent import RUNTIME

ROOT = Path(__file__).resolve().parent
KINDS = {'place', 'preference'}
FUNCTIONS = [
    {'name': 'memory_list', 'description': '读取当前用户的全部常用地点和偏好。',
     'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'memory_read', 'description': '读取当前用户指定名称的地点或偏好。',
     'parameters': {'type': 'object', 'properties': {
         'kind': {'type': 'string', 'enum': sorted(KINDS)}, 'name': {'type': 'string'}},
                    'required': ['kind', 'name'], 'additionalProperties': False}},
    {'name': 'memory_save', 'description': '按本条用户明确要求保存或修改地点/偏好；同名覆盖。内容和城市原样摘录用户 evidence。',
     'parameters': {'type': 'object', 'properties': {
         'kind': {'type': 'string', 'enum': sorted(KINDS)}, 'name': {'type': 'string'},
         'value': {'type': 'string', 'description': '用户提供的完整地点地址或偏好原文。'},
         'city': {'type': 'string', 'description': '地点必填，用户明确提供的城市原文。'},
         'evidence': {'type': 'string', 'description': '本条用户要求保存/修改的原文，包含内容。'}},
                    'required': ['kind', 'name', 'value', 'evidence'], 'additionalProperties': False}},
    {'name': 'memory_delete', 'description': '按本条用户要求删除指定地点或偏好。',
     'parameters': {'type': 'object', 'properties': {
         'kind': {'type': 'string', 'enum': sorted(KINDS)}, 'name': {'type': 'string'},
         'evidence': {'type': 'string', 'description': '本条用户明确要求删除此项的原文。'}},
                    'required': ['kind', 'name', 'evidence'], 'additionalProperties': False}},
]


def text_field(value, name, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\x00' in value:
        raise ValueError(f'{name}必须是 1–{limit} 字的文字')
    return value.strip()


class OfficialMemoryClient:
    def __init__(self, file_path):
        node = shutil.which('node')
        entry = ROOT / 'node_modules/@modelcontextprotocol/server-memory/dist/index.js'
        if not node or not entry.exists():
            raise ValueError('记忆组件未安装：请安装 Node.js 18+，在项目目录运行 npm ci')
        file_path = Path(file_path).resolve()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        # 不把模型、滴滴和微信凭证交给记忆子进程。
        env = {key: value for key, value in os.environ.items() if key.upper() in {
            'PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'LANG'}}
        self.process = subprocess.Popen([node, str(ROOT / 'memory_bridge.mjs'), str(file_path)],
                                        cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.closed = False
        self.responses = queue.Queue()
        self.counter = 0
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.tools = self.rpc('list_tools').get('tools', [])
        except Exception:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                self.responses.put(json.loads(line))
        except (ValueError, OSError):
            pass
        finally:
            self.responses.put(None)

    def rpc(self, method, params=None):
        if self.closed:
            raise ValueError('记忆组件连接已关闭，本次操作未重试')
        self.counter += 1
        try:
            self.process.stdin.write(json.dumps({'id': self.counter, 'method': method, 'params': params}, ensure_ascii=False) + '\n')
            self.process.stdin.flush()
            packet = self.responses.get(timeout=75)
            if not packet or packet.get('id') != self.counter or packet.get('error'):
                self.close()
                raise ValueError('记忆组件调用失败，本次操作未重试；请检查 npm 依赖')
            return packet['result']
        except (OSError, queue.Empty):
            self.close()
            raise ValueError('记忆组件响应中断或超时，本次操作未重试') from None

    def call(self, name, args):
        result = self.rpc('call', {'name': name, 'arguments': args})
        if result.get('isError'):
            raise ValueError('官方记忆工具返回错误，本次操作未重试')
        if isinstance(result.get('structuredContent'), dict):
            return result['structuredContent']
        for item in result.get('content', []):
            if item.get('type') == 'text':
                try:
                    return json.loads(item['text'])
                except (ValueError, KeyError):
                    continue
        raise ValueError('官方记忆工具没有返回可识别的数据')

    def close(self):
        if self.closed:
            return
        try:
            if self.process.poll() is None:
                self.process.stdin.write(json.dumps({'id': -1, 'method': 'close'}) + '\n')
                self.process.stdin.flush()
                self.process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        finally:
            self.closed = True
            for pipe in (self.process.stdin, self.process.stdout):
                try:
                    pipe.close()
                except OSError:
                    pass


class MemoryMCP:
    """一个已验证微信用户一个官方服务器/文件，模型不能指定用户或路径。"""
    def __init__(self, directory=None, client_factory=OfficialMemoryClient):
        self.directory = Path(directory or RUNTIME / 'memories').resolve()
        self.client_factory = client_factory
        self.clients = {}

    @staticmethod
    def owner(session):
        # 微信重新绑定 bot 后，同一用户的文件仍可复用；console 是独立测试身份。
        return 'wechat:' + session.split(':', 1)[1] if ':' in session else 'console:' + session

    def client(self, session):
        owner = self.owner(session)
        old = self.clients.get(owner)
        if old is None or old.closed:
            path = self.directory / (hashlib.sha256(owner.encode()).hexdigest() + '.jsonl')
            self.clients[owner] = self.client_factory(path)
        return self.clients[owner]

    @staticmethod
    def records(graph):
        records = []
        if not isinstance(graph, dict):
            return records
        for entity in graph.get('entities', []):
            versions = []
            for observation in entity.get('observations', []):
                try:
                    item = json.loads(observation)
                    if (isinstance(item, dict) and item.get('kind') in KINDS and
                            isinstance(item.get('name'), str) and isinstance(item.get('value'), str) and
                            isinstance(item.get('revision'), int)):
                        versions.append(item)
                except (ValueError, TypeError):
                    continue
            if versions:
                records.append(max(versions, key=lambda item: item['revision']))
        return records

    def list(self, session):
        return self.records(self.client(session).call('read_graph', {}))

    def read(self, session, kind, name):
        if kind not in KINDS:
            raise ValueError('记忆类别必须是 place 或 preference')
        name = text_field(name, '名称', 60)
        graph = self.client(session).call('open_nodes', {'names': [kind + ':' + name]})
        items = self.records(graph)
        return items[0] if items else None

    def context(self, session, text):
        items = self.list(session)
        items.sort(key=lambda item: (item['name'] in text, item['kind'] == 'preference', item.get('updated', 0)), reverse=True)
        selected, budget = [], 8000
        for item in items:
            value = {key: item.get(key, '') for key in ('kind', 'name', 'city', 'value')}
            length = len(json.dumps(value, ensure_ascii=False))
            if len(selected) < 20 and length <= budget:
                selected.append(value)
                budget -= length
        return {'items': selected, 'total': len(items), 'truncated': len(selected) < len(items)}

    @staticmethod
    def evidence(args, user_text, deleting=False):
        quote = text_field(args.get('evidence'), '用户原文', 1000)
        pattern = r'忘记|删除|移除|清除|不要再记' if deleting else r'记住|记一下|记下来|保存|更新|修改|改成|以后|默认|偏好|习惯'
        if not isinstance(user_text, str) or quote not in user_text or not re.search(pattern, quote):
            raise ValueError('请根据本条用户明确的记忆保存/修改/删除要求操作')
        if not deleting and re.search(r'不要(?:再)?(?:记|保存)|别(?:记|保存)|不用(?:记|保存)', quote):
            raise ValueError('用户没有授权保存这项记忆')
        return quote

    def save(self, session, args, user_text):
        kind = args.get('kind')
        if kind not in KINDS:
            raise ValueError('记忆类别必须是 place 或 preference')
        name = text_field(args.get('name'), '名称', 60)
        value = text_field(args.get('value'), '内容', 300)
        quote = self.evidence(args, user_text)
        if value not in quote:
            raise ValueError('记忆内容必须来自本条用户原文，不可推测')
        city = text_field(args.get('city'), '城市', 40) if kind == 'place' else ''
        if kind == 'place' and (city not in quote or name not in quote):
            raise ValueError('保存地点需要用户明确提供别名、城市和地址')
        client = self.client(session)
        key = kind + ':' + name
        old_graph = client.call('open_nodes', {'names': [key]})
        old = self.records(old_graph)
        if not old and len(self.list(session)) >= 50:
            raise ValueError('已达到 50 项记忆，请先删除不需要的记录')
        record = {'kind': kind, 'name': name, 'value': value, 'city': city,
                  'revision': old[0]['revision'] + 1 if old else 1, 'updated': time.time()}
        observation = json.dumps(record, ensure_ascii=False, separators=(',', ':'))
        if old_graph.get('entities'):
            client.call('add_observations', {'observations': [{'entityName': key, 'contents': [observation]}]})
            previous = [o for e in old_graph['entities'] for o in e.get('observations', [])]
            if previous:
                # 先添加再清理；中途失败仍能用最新 revision 恢复，不会先删空旧地址。
                client.call('delete_observations', {'deletions': [{'entityName': key, 'observations': previous}]})
        else:
            client.call('create_entities', {'entities': [{'name': key, 'entityType': kind, 'observations': [observation]}]})
        verified = self.read(session, kind, name)
        if verified != record:
            raise ValueError('记忆写入未核实，请查看记忆后再决定是否重试')
        return record

    def delete(self, session, args, user_text):
        kind = args.get('kind')
        if kind not in KINDS:
            raise ValueError('记忆类别必须是 place 或 preference')
        name = text_field(args.get('name'), '名称', 60)
        quote = self.evidence(args, user_text, deleting=True)
        if name not in quote:
            raise ValueError('删除要求需要指明记忆名称')
        old = self.read(session, kind, name)
        self.client(session).call('delete_entities', {'entityNames': [kind + ':' + name]})
        if self.read(session, kind, name) is not None:
            raise ValueError('记忆删除未核实，请查看记忆')
        return {'deleted': old is not None, 'kind': kind, 'name': name}

    def close(self):
        for client in self.clients.values():
            client.close()
        self.clients.clear()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
