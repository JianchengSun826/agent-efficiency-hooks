import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from efficiency_hooks.adapters import normalize, encode
from efficiency_hooks.model import Decision
from efficiency_hooks.engine import Engine


class AdapterTests(unittest.TestCase):
    def payload(self, event='PostToolUse', tool='Bash', response=None):
        return {'session_id': 's', 'cwd': '/tmp', 'hook_event_name': event,
                'tool_name': tool, 'tool_use_id': 'c', 'tool_input': {'command': 'pytest'},
                'tool_response': response}

    def test_codex_nonzero_exit(self):
        e = normalize('codex', self.payload(response={'exit_code': 1, 'output': 'assertion failed'}))
        self.assertEqual(e.status, 'failure')
        self.assertTrue(e.error_signature)

    def test_codex_text_exit(self):
        e = normalize('codex', self.payload(response='Chunk ID: abc\nProcess exited with code 1\nFinal output:\nfailed'))
        self.assertEqual(e.status, 'failure')

    def test_incomplete_and_unclassified_are_unknown(self):
        for r in ({'session_id': 123, 'output': 'still running'}, 'something failed', {'output': 'success'}):
            self.assertEqual(normalize('codex', self.payload(response=r)).status, 'unknown')

    def test_claude_failure_and_interrupt(self):
        p = self.payload('PostToolUseFailure')
        p['error'] = 'Exit code 1\nassertion failed'
        self.assertEqual(normalize('claude', p).status, 'failure')
        p['is_interrupt'] = True
        self.assertEqual(normalize('claude', p).status, 'unknown')

    def test_successful_claude_read(self):
        p = self.payload(tool='Read', response={'file': {'content': 'x'}})
        self.assertEqual(normalize('claude', p).status, 'success')

    def test_successful_claude_bash_without_exit_code(self):
        p = self.payload(response={'stdout': 'passed', 'stderr': '', 'interrupted': False})
        self.assertEqual(normalize('claude', p).status, 'success')

    def test_hermes_error_and_running(self):
        p = {'session_id': 's', 'task_id': 'a', 'hook_event_name': 'post_tool_call',
             'tool_name': 'terminal', 'tool_call_id': 'c', 'args': {'command': 'pytest', 'workdir': '/tmp'},
             'status': 'success', 'result': json.dumps({'exit_code': 1, 'output': 'failed'})}
        e = normalize('hermes', p, {'hermes_local_terminal': True})
        self.assertEqual((e.status, e.agent_id), ('failure', ''))
        p['result'] = json.dumps({'exit_code': None, 'session_id': 'pty'})
        self.assertEqual(normalize('hermes', p, {'hermes_local_terminal': True}).status, 'unknown')
        p['status'] = 'blocked'
        self.assertEqual(normalize('hermes', p, {'hermes_local_terminal': True}).status, 'unknown')

    def test_hermes_builtin_read_skip(self):
        p = {'session_id': 's', 'hook_event_name': 'pre_tool_call', 'tool_name': 'read_file',
             'args': {'path': '/tmp/x'}, 'tool_call_id': 'c'}
        self.assertIsNone(normalize('hermes', p, {'cwd': '/tmp'}))
        self.assertIsNotNone(normalize('hermes', p, {'cwd': '/tmp', 'hermes_monitor_read_file': True}))

    def test_native_output_decisions(self):
        for platform in ('codex', 'claude'):
            self.assertIsNone(encode(platform, Decision()))
            self.assertEqual(encode(platform, Decision('deny', 'why'))['hookSpecificOutput']['permissionDecision'], 'deny')
            self.assertEqual(encode(platform, Decision('warn', 'why'))['hookSpecificOutput']['additionalContext'], 'why')
        self.assertEqual(encode('hermes', Decision('deny', 'why')), {'action': 'block', 'message': 'why'})
        self.assertIsNone(encode('hermes', Decision('warn', 'why')))
        self.assertEqual(encode('hermes', Decision('warn', 'why'), feedback=True), {'context': 'why'})

    def test_compaction_and_missing_identity(self):
        self.assertEqual(normalize('codex', self.payload('PostCompact')).phase, 'reset')
        self.assertIsNone(normalize('codex', {}))

    def hermes_shell(self, event, call='c', task='turn-a', result=None):
        return {'session_id': 'hermes-session', 'cwd': '/host', 'hook_event_name': event,
                'tool_name': 'terminal', 'tool_input': {'command': 'cat x', 'workdir': '/tmp'},
                'extra': {'task_id': task, 'tool_call_id': call, 'status': 'success', 'result': result}}

    def test_real_hermes_shell_envelope(self):
        p = self.hermes_shell('post_tool_call', result=json.dumps({'exit_code': 1, 'output': 'failed'}))
        e = normalize('hermes', p, {'hermes_local_terminal': True})
        self.assertEqual((e.call_id, e.cwd, e.status, e.args['command']), ('c', '/tmp', 'failure', 'cat x'))

    def test_tool_workdir_precedes_host_cwd(self):
        p = self.payload('PreToolUse')
        p['tool_input']['workdir'] = '/tmp/subproject'
        self.assertEqual(normalize('codex', p).cwd, '/tmp/subproject')

    def test_hermes_warning_survives_new_turn(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'x').write_text('hello')
            engine = Engine({}, root / 's.db')
            for i in (1, 2, 3):
                p = self.hermes_shell('pre_tool_call', call=str(i), task='turn-a')
                p['tool_input']['workdir'] = d
                engine.handle(normalize('hermes', p, {'hermes_local_terminal': True}))
                p.update(hook_event_name='post_tool_call')
                p['extra']['result'] = json.dumps({'exit_code': 0, 'output': 'hello'})
                engine.handle(normalize('hermes', p, {'hermes_local_terminal': True}))
            p = self.hermes_shell('pre_llm_call', task='turn-b')
            p['tool_name'], p['tool_input'] = None, None
            e = normalize('hermes', p, {'hermes_local_terminal': True})
            self.assertEqual(engine.handle(e).action, 'warn')

    def test_hermes_remote_and_unknown_terminal_cwd_skipped(self):
        p = self.hermes_shell('pre_tool_call')
        self.assertIsNone(normalize('hermes', p, {'hermes_local_terminal': False}))
        p['tool_input'].pop('workdir')
        self.assertIsNone(normalize('hermes', p, {'hermes_local_terminal': True}))

    def test_runner_fail_open_and_native_read_smoke(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'x').write_text('hello')
            cfg = root / 'config.json'
            cfg.write_text(json.dumps({'database': str(root / 'state.db')}))
            cmd = [sys.executable, 'hook.py', '--platform', 'claude', '--config', str(cfg)]
            bad = subprocess.run(cmd, input='bad json', text=True, capture_output=True)
            self.assertEqual((bad.returncode, bad.stdout), (0, ''))
            p = self.payload('PreToolUse', 'Read')
            p['cwd'] = d
            p['tool_input'] = {'file_path': 'x'}
            for i in (1, 2):
                p.update(tool_use_id=str(i), hook_event_name='PreToolUse')
                self.assertEqual(subprocess.run(cmd, input=json.dumps(p), text=True, capture_output=True).stdout, '')
                p.update(hook_event_name='PostToolUse', tool_response={'file': {'content': 'hello'}})
                subprocess.run(cmd, input=json.dumps(p), text=True, capture_output=True, check=True)
            p.update(tool_use_id='3', hook_event_name='PreToolUse')
            out = subprocess.run(cmd, input=json.dumps(p), text=True, capture_output=True, check=True)
            self.assertIn('additionalContext', json.loads(out.stdout)['hookSpecificOutput'])


if __name__ == '__main__':
    unittest.main()
