"""Exercise deployment orchestration with a fake Docker CLI; never touch the daemon."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[2]
FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['DOCKER_CALLS'], 'a') as stream:
    stream.write(json.dumps(args) + '\n')
failure = os.environ.get('DOCKER_FAILURE', '')
if args[0] == 'info':
    sys.exit(1 if failure == 'daemon' else 0)
if args[0] == 'inspect':
    service = args[-1].removesuffix('-container')
    if failure == 'health' and service == 'db':
        print('running|unhealthy')
    else:
        print('running|' + ('healthy' if service in ('db', 'api') else 'none'))
    sys.exit(0)
assert args.pop(0) == 'compose'
https = False
while args and args[0] in ('--project-directory', '--file', '--env-file', '--profile'):
    if args[:2] == ['--profile', 'https']:
        https = True
    args = args[2:]
action, *options = args
if action == 'config' and options == ['--services']:
    print('db\nmailpit\nmigrate\napi\nworker\nfrontend')
    if https:
        print('https')
if action == 'run' and 'migrate' in options and failure == 'migration':
    print('simulated migration failure', file=sys.stderr)
    sys.exit(9)
if action == 'run' and 'initial-period' in options and failure == 'dates':
    print('simulated initial date validation failure', file=sys.stderr)
    sys.exit(10)
if action == 'run' and options[-3:] == ['https', 'nginx', '-t'] and failure == 'tls':
    print('simulated invalid certificate', file=sys.stderr)
    sys.exit(11)
if action == 'exec' and any('http://frontend/api/v1/terms' in value for value in options) and failure == 'http':
    sys.exit(1)
if action == 'ps':
    if '--quiet' in options:
        print(options[-1] + '-container')
    else:
        print('service status summary')
if action == 'port':
    print({('frontend', '80'): '[::]:19080\n0.0.0.0:19080', ('https', '443'): '[::]:18443',
           ('api', '8000'): '127.0.0.1:18000', ('mailpit', '8025'): '0.0.0.0:18025'}[tuple(options)])
'''


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / 'project with spaces'
        self.project.mkdir()
        for filename in ('deploy.sh', 'compose.yaml', '.env.example'):
            shutil.copy2(SOURCE / filename, self.project / filename)
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        docker = self.bin / 'docker'
        docker.write_text(FAKE_DOCKER)
        docker.chmod(0o755)
        self.call_log = self.base / 'docker-calls.jsonl'
        self.env = {**os.environ, 'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                    'DOCKER_CALLS': str(self.call_log)}

    def run_deploy(self, *args, failure='', cwd=None):
        return subprocess.run([str(self.project / 'deploy.sh'), *args],
                              cwd=cwd or self.base, env={**self.env, 'DOCKER_FAILURE': failure},
                              text=True, capture_output=True, timeout=15)

    def calls(self):
        return [json.loads(line) for line in self.call_log.read_text().splitlines()] if self.call_log.exists() else []

    def test_first_deployment_creates_private_env_and_checks_proxy(self):
        result = self.run_deploy('--scenario', 'awaiting')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.project / '.env').read_text(), (self.project / '.env.example').read_text())
        self.assertEqual((self.project / '.env').stat().st_mode & 0o777, 0o600)
        calls = self.calls()
        validation = next(i for i, c in enumerate(calls) if 'initial-period' in c and '--check' in c)
        migration = next(i for i, c in enumerate(calls) if 'run' in c and 'migrate' in c)
        stopping = next(i for i, c in enumerate(calls) if 'stop' in c)
        seeding = next(i for i, c in enumerate(calls) if 'app.seed' in c)
        applying = next(i for i, c in enumerate(calls) if 'initial-period' in c and 'exec' in c)
        self.assertLess(validation, stopping)
        self.assertLess(stopping, migration)
        self.assertLess(migration, seeding)
        self.assertLess(seeding, applying)
        self.assertIn('awaiting', calls[seeding])
        self.assertTrue(any('exec' in c and '-c' in c and 'http://frontend/api/v1/terms' in c[-1] for c in calls))
        for url in ('http://localhost:19080', 'http://localhost:18000/docs', 'http://localhost:18025'):
            self.assertIn(url, result.stdout)

    def test_repeat_deploy_preserves_env_and_reruns_migration_without_seed(self):
        marker = self.base / 'should-not-execute'
        env_content = f'KEEP_ME=secret-sentinel\nUNTRUSTED=$(touch "{marker}")\n'
        (self.project / '.env').write_text(env_content)
        for _ in range(2):
            result = self.run_deploy('--skip-seed', '--no-build')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('secret-sentinel', result.stdout + result.stderr)
        self.assertEqual((self.project / '.env').read_text(), env_content)
        self.assertFalse(marker.exists())
        calls = self.calls()
        self.assertEqual(sum('run' in c and 'migrate' in c for c in calls), 2)
        self.assertEqual(sum('exec' in c and 'initial-period' in c for c in calls), 2)
        self.assertFalse(any('app.seed' in c or 'build' in c for c in calls))
        self.assertFalse(any(set(c) & {'down', 'prune', 'volume', 'volumes', '-v'} for c in calls))

    def test_custom_env_resolves_relative_to_invoking_directory(self):
        result = self.run_deploy('--env-file', 'config with spaces.env', '--skip-seed')
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.base / 'config with spaces.env'
        self.assertTrue(target.is_file())
        self.assertFalse((self.project / '.env').exists())
        config = next(c for c in self.calls() if 'config' in c)
        self.assertEqual(config[config.index('--env-file') + 1], str(target))

    def test_migration_failure_stops_before_application_start(self):
        result = self.run_deploy(failure='migration')
        self.assertEqual(result.returncode, 9)
        self.assertIn('資料庫 migration', result.stderr)
        calls = self.calls()
        self.assertFalse(any('app.seed' in c or '--force-recreate' in c for c in calls))
        self.assertTrue(any('ps' in c and '--all' in c for c in calls))

    def test_database_failure_keeps_existing_application_running(self):
        result = self.run_deploy(failure='health')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unhealthy', result.stderr)
        self.assertFalse(any('stop' in c or 'run' in c for c in self.calls()))

    def test_frontend_failure_does_not_report_success(self):
        result = self.run_deploy('--timeout', '1', failure='http')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('代理未能', result.stderr)
        self.assertNotIn('部署完成。', result.stdout)

    def test_https_deployment_uses_one_compose_file_and_reports_port(self):
        tls = self.project / '.tls'
        tls.mkdir()
        for filename in ('server.crt', 'server.key'):
            (tls / filename).touch()
        result = self.run_deploy('--skip-seed', '--https')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('https://localhost:18443', result.stdout)
        config = next(call for call in self.calls() if 'config' in call)
        self.assertEqual(config.count('--file'), 1)
        self.assertEqual(config[config.index('--profile') + 1], 'https')
        calls = self.calls()
        validate = next(i for i, call in enumerate(calls) if call[-3:] == ['https', 'nginx', '-t'])
        stop = next(i for i, call in enumerate(calls) if 'stop' in call)
        self.assertLess(validate, stop)
        self.assertTrue(any('up' in call and call[-1] == 'https' for call in self.calls()))

    def test_missing_tls_files_fail_before_restarting_services(self):
        result = self.run_deploy('--https')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('.tls/server.crt', result.stderr)
        self.assertFalse(any('stop' in call or 'up' in call for call in self.calls()))

    def test_invalid_tls_files_keep_the_running_application(self):
        tls = self.project / '.tls'
        tls.mkdir()
        for filename in ('server.crt', 'server.key'):
            (tls / filename).write_text('invalid')
        result = self.run_deploy('--https', failure='tls')
        self.assertEqual(result.returncode, 11)
        self.assertIn('驗證 HTTPS 憑證與代理設定', result.stderr)
        self.assertFalse(any('stop' in call or 'up' in call for call in self.calls()))

    def test_invalid_initial_dates_keep_the_running_application(self):
        (self.project / '.env').write_text('INITIAL_START_AT=invalid\nINITIAL_END_AT=2026-11-30T23:59:00+08:00\n')
        result = self.run_deploy('--skip-seed', failure='dates')
        self.assertEqual(result.returncode, 10)
        self.assertIn('確認初選時段', result.stderr)
        self.assertFalse(any('stop' in call or ('run' in call and 'migrate' in call) for call in self.calls()))

    def test_daemon_failure_creates_no_environment_file(self):
        result = self.run_deploy(failure='daemon')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Docker socket', result.stderr)
        self.assertFalse((self.project / '.env').exists())
        self.assertFalse(any('build' in c or 'up' in c for c in self.calls()))

    def test_help_and_invalid_arguments_do_not_call_docker(self):
        self.assertEqual(self.run_deploy('--help').returncode, 0)
        for args in [('--scenario',), ('--env-file',), ('--timeout',), ('--scenario', 'invalid'),
                     ('--timeout', '0'), ('--timeout', '99999999999999999999'), ('--unknown',)]:
            with self.subTest(args=args):
                self.assertNotEqual(self.run_deploy(*args).returncode, 0)
        self.assertEqual(self.calls(), [])


if __name__ == '__main__':
    unittest.main()
