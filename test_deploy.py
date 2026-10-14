"""Server runtime isolation checks; no network or real credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


class DeploymentTests(unittest.TestCase):
    def test_all_personal_state_uses_external_runtime_directory(self):
        with TemporaryDirectory() as directory:
            runtime = Path(directory) / 'nested' / 'data'
            config = Path(directory) / 'server.env'
            config.write_text('DEPLOY_TEST_VALUE=from-file\n', encoding='utf-8')
            code = '''
import json
from agent import RUNTIME, Store, load_env
from memory_mcp import MemoryMCP
from place_search import Settings
from wechat import AUTH_FILE
from main import runner_lock
import os
load_env()
with runner_lock():
    store = Store()
    store.put('check', {'ok': True})
    store.db.close()
print(json.dumps([str(RUNTIME), str(MemoryMCP().directory), str(Settings().path), str(AUTH_FILE), os.environ['DEPLOY_TEST_VALUE']]))
'''
            run = subprocess.run([sys.executable, '-X', 'utf8', '-c', code], check=True, capture_output=True,
                                 text=True, encoding='utf-8', env={**os.environ, 'APP_RUNTIME_DIR': str(runtime),
                                                                'APP_ENV_FILE': str(config)})
            values = json.loads(run.stdout)
            self.assertEqual(values, [str(runtime.resolve()), str(runtime / 'memories'),
                                     str(runtime / 'admin-settings.json'), str(runtime / 'wechat-auth.json'), 'from-file'])
            self.assertTrue((runtime / 'state.sqlite').exists())
            self.assertTrue((runtime / 'runner.lock').exists())

    def test_environment_overrides_config_file(self):
        with TemporaryDirectory() as directory:
            config = Path(directory) / 'config'
            config.write_text('DEPLOY_TEST_VALUE=file\n', encoding='utf-8')
            run = subprocess.run([sys.executable, '-c',
                                  'from agent import load_env; import os; load_env(); print(os.environ["DEPLOY_TEST_VALUE"])'],
                                 check=True, capture_output=True, text=True,
                                 env={**os.environ, 'APP_ENV_FILE': str(config), 'DEPLOY_TEST_VALUE': 'environment'})
            self.assertEqual(run.stdout.strip(), 'environment')


if __name__ == '__main__':
    unittest.main()
