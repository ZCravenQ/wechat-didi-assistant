"""全国地点检索只用于确定城市；打车坐标仍由打车 MCP 实时搜索获得。"""
import json
import os
import threading
from pathlib import Path
from urllib.parse import urlencode

from agent import RUNTIME, request_json

AMAP_TOOLS = {'maps_text_search', 'maps_search_detail', 'maps_regeocode', 'maps_geo'}
MUNICIPALITIES = {'北京市', '天津市', '上海市', '重庆市'}


class Settings:
    def __init__(self, path=None):
        self.path = Path(path or RUNTIME / 'admin-settings.json')
        self.lock = threading.RLock()

    def get(self):
        with self.lock:
            data = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}
            return {'default_city': data.get('default_city', os.environ.get('DIDI_DEFAULT_CITY', '')),
                    'amap_key': data.get('amap_key', os.environ.get('AMAP_API_KEY', ''))}

    def save(self, city, key=None):
        if not isinstance(city, str) or len(city) > 40 or '\x00' in city:
            raise ValueError('默认城市最多 40 字')
        if key is not None and (not isinstance(key, str) or len(key) > 500 or any(c.isspace() for c in key)):
            raise ValueError('地图 Key 格式无效')
        with self.lock:
            data = self.get()
            data['default_city'] = city.strip()
            if key is not None:
                data['amap_key'] = key
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix('.tmp')
            temp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            temp.replace(self.path)


class AmapMCP:
    """高德官方 Streamable HTTP，只允许地点查询，不暴露高德打车工具。"""
    def __init__(self, key):
        self.url = 'https://mcp.amap.com/mcp?' + urlencode({'key': key})
        self.counter, self.session, self.protocol = 0, None, '2025-03-26'
        result = self.rpc('initialize', {'protocolVersion': self.protocol, 'capabilities': {},
                                        'clientInfo': {'name': 'wechat-didi-places', 'version': '0.1.0'}})
        self.protocol = result.get('protocolVersion', self.protocol)
        self.rpc('notifications/initialized', notify=True)
        self.tools = {}
        cursor = None
        for _ in range(10):
            page = self.rpc('tools/list', {'cursor': cursor} if cursor else {})
            self.tools.update({t['name']: t for t in page.get('tools', []) if t.get('name') in AMAP_TOOLS})
            cursor = page.get('nextCursor')
            if not cursor:
                break
        else:
            raise ValueError('高德 MCP 工具列表分页异常')
        if 'maps_text_search' not in self.tools:
            raise ValueError('高德 MCP 未提供关键词搜索工具，请核对 Key 权限和服务版本')

    def rpc(self, method, params=None, notify=False):
        self.counter += 1
        body = {'jsonrpc': '2.0', 'method': method, 'params': params or {}}
        if not notify:
            body['id'] = self.counter
        headers = {'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': self.protocol}
        if self.session:
            headers['Mcp-Session-Id'] = self.session
        try:
            packet, received = request_json(self.url, body, headers, timeout=30, rpc_id=body.get('id'))
        except ValueError:
            raise ValueError('高德 MCP 连接失败，请检查 Web服务 Key、网络、权限及额度；未自动重试') from None
        for key, value in received.items():
            if key.lower() == 'mcp-session-id':
                self.session = value
        if not isinstance(packet, dict) or 'error' in packet:
            raise ValueError('高德 MCP 请求失败，请检查 Key、权限及额度')
        if not notify and packet.get('id') != body['id']:
            raise ValueError('高德 MCP 响应 ID 不匹配')
        result = packet.get('result', {})
        if not isinstance(result, dict):
            raise ValueError('高德 MCP 返回格式不兼容')
        return result

    def call(self, name, args):
        if name not in AMAP_TOOLS or name not in self.tools:
            raise ValueError('高德 MCP 未提供该地点查询工具')
        result = self.rpc('tools/call', {'name': name, 'arguments': args})
        if result.get('isError'):
            raise ValueError('高德地点查询失败，请检查 Key 权限、额度或细化地址')
        data = result.get('structuredContent')
        if data is None:
            for part in result.get('content', []):
                if part.get('type') == 'text':
                    try:
                        data = json.loads(part.get('text', ''))
                    except (ValueError, TypeError):
                        continue
                    break
        if not isinstance(data, (dict, list)):
            raise ValueError('高德地点查询没有返回可解析的数据')
        if isinstance(data, dict) and (str(data.get('status', '1')) == '0' or data.get('error')):
            raise ValueError('高德地点查询失败，请检查 Key 权限及额度')
        return data


def records(data):
    """兼容 MCP 文本 JSON、结构化结果和 WebService 的常见封装。"""
    if isinstance(data, list):
        return [p for p in data if isinstance(p, dict)]
    if isinstance(data, dict):
        for key in ('pois', 'geocodes', 'results', 'data', 'result', 'regeocode', 'addressComponent'):
            if key in data and isinstance(data[key], (dict, list)):
                return records(data[key])
        return [data]
    return []


def string(data, *keys):
    return next((data[k].strip() for k in keys if isinstance(data.get(k), str) and data[k].strip()), '')


def city_of(data):
    city = string(data, 'cityname', 'city')
    # 高德直辖市的 city 可能是空数组；只使用其实际返回的省级名称。
    province = string(data, 'pname', 'province')
    return city or (province if province in MUNICIPALITIES else '')


class PlaceSearch:
    def __init__(self, settings=None):
        self.settings = settings or Settings()
        self.client, self.key = None, None
        self.lock = threading.RLock()

    def nationwide(self, keywords, city_hint=''):
        with self.lock:
            key = self.settings.get()['amap_key']
            if not key or key == 'replace-me':
                self.client, self.key = None, None
                raise ValueError('请在本地管理界面配置高德 Web服务 Key 开启全国地点搜索，或设置默认城市；再发送出发地和目的地。')
            try:
                if self.client is None or self.key != key:
                    self.client, self.key = AmapMCP(key), key
                return self._search(keywords, city_hint)
            except ValueError:
                # 下次查询重新握手；本次不重复请求，避免额外消耗额度。
                self.client, self.key = None, None
                raise

    def _search(self, keywords, city_hint):
        args = {'keywords': keywords}
        if city_hint:
            args['city'] = city_hint
        pois = records(self.client.call('maps_text_search', args))
        if not pois and 'maps_geo' in self.client.tools:
            args = {'address': keywords, **({'city': city_hint} if city_hint else {})}
            pois = records(self.client.call('maps_geo', args))
        result, remaining = [], 5  # 每个地点最多追加 5 次详情/逆地理查询。
        for poi in pois[:20]:
            detail = dict(poi)
            city = city_of(detail)
            if not city and remaining and detail.get('id') and 'maps_search_detail' in self.client.tools:
                remaining -= 1
                found = records(self.client.call('maps_search_detail', {'id': detail['id']}))
                if found:
                    detail.update(found[0])
                    city = city_of(detail)
            location = string(detail, 'location')
            if not city and remaining and location and 'maps_regeocode' in self.client.tools:
                remaining -= 1
                found = records(self.client.call('maps_regeocode', {'location': location}))
                if found:
                    city = city_of(found[0])
            # 未解析出城市的候选也必须保留，不能过滤后误判为“唯一城市”。
            result.append({'name': string(detail, 'name') or keywords, 'city': city,
                           'address': string(detail, 'formatted_address', 'address_all', 'address')})
        # 不向模型提供高德坐标、POI ID 或原始响应，打车坐标只来自滴滴。
        return result
