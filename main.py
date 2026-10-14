import argparse
from contextlib import contextmanager
import os
import sys

from agent import Agent, Didi, LLM, RUNTIME, Store, load_env
from memory_mcp import MemoryMCP


@contextmanager
def runner_lock():
    """同一个运行目录只允许一个业务进程，避免多个窗口同时改订单状态。"""
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with (RUNTIME / 'runner.lock').open('a+b') as file:
        file.seek(0, 2)
        if file.tell() == 0:
            file.write(b'0')
            file.flush()
        file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError('已有服务使用此运行目录，请先停止另一个 console 或 wechat 窗口。') from None
        try:
            yield
        finally:
            file.seek(0)
            if os.name == 'nt':
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def main():
    # Linux 下新建配置、数据库和记忆只供运行用户访问。
    if os.name != 'nt':
        os.umask(0o077)
    parser = argparse.ArgumentParser(description='微信 → LLM → 滴滴 MCP 最小研究原型')
    parser.add_argument('mode', choices=['console', 'tools', 'login', 'wechat', 'ui', 'memory-check'])
    parser.add_argument('--ui-port', type=int, default=None, help='本机管理端口，默认 37691')
    args = parser.parse_args()
    load_env()
    port = args.ui_port if args.ui_port is not None else int(os.environ.get('ADMIN_PORT', '37691'))
    if args.mode == 'ui':
        from admin_ui import Admin, local_ui
        import threading
        with runner_lock(), MemoryMCP() as memory, local_ui(Admin(memory), port):
            threading.Event().wait()
        return
    if args.mode == 'memory-check':
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            with MemoryMCP(directory) as memory:
                item = {'kind': 'preference', 'name': '测试偏好', 'value': '优先快车',
                        'evidence': '记住，测试偏好是优先快车'}
                memory.save('check', item, item['evidence'])
            with MemoryMCP(directory) as restarted:
                if restarted.read('check', 'preference', '测试偏好')['value'] != '优先快车':
                    raise ValueError('记忆重启检查失败')
                restarted.delete('check', {'kind': 'preference', 'name': '测试偏好',
                                          'evidence': '删除测试偏好'}, '删除测试偏好')
        print('官方 Memory Server：保存、重启读取、删除检查通过。测试未修改你的实际记忆。')
        return
    if args.mode == 'login':
        from wechat import Wechat
        Wechat().login()
        return
    didi = Didi()
    if args.mode == 'tools':
        for tool in didi.tools:
            print(tool['name'])
        return
    with runner_lock():
        run_agent(args.mode, didi, port)


def run_agent(mode, didi, port=37691):
    from admin_ui import Admin, local_ui
    store = Store()
    with MemoryMCP() as memory:
        try:
            agent = Agent(didi, LLM(), store, memory=memory)
            with local_ui(Admin(memory, agent=agent, settings=agent.places.settings, mode=mode), port):
                run_session(mode, agent, store)
        finally:
            store.db.close()


def run_session(mode, agent, store):
    if mode == 'wechat':
        from wechat import run_wechat
        run_wechat(agent, store)
        return
    print(f'当前环境：{agent.didi.env}。看过叫车确认单后回复“确认”下单；production 操作真实订单。')
    print('输入 /exit 退出，/reset 重置聊天，/memory 查看长期记忆。')
    while True:
        text = input('你：').strip()
        if text == '/exit':
            break
        if not text:
            continue
        try:
            print('助手：' + agent.respond('console', text))
        except ValueError as error:
            print('处理未完成：' + str(error))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n服务已停止。')
    except (ValueError, KeyError, TypeError) as error:
        if isinstance(error, ValueError) and str(error).startswith('本地管理端口'):
            print(str(error), file=sys.stderr)
        else:
            print('启动或接口响应不符合预期。请检查 .env、权限和当前官方协议；未打印凭证。', file=sys.stderr)
        sys.exit(1)
