import tempfile
import unittest
import concurrent.futures
import time
from dataclasses import replace
from pathlib import Path

from efficiency_hooks.engine import Engine
from efficiency_hooks.model import Event


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'a.py').write_text('value = 1\n')
        self.cfg = {'mode': 'warn', 'verification': [{'command': 'python3 -m unittest', 'inputs': ['a.py'], 'complete': True}]}
        self.engine = Engine(self.cfg, self.root / 'state.db')

    def event(self, call, phase='before', tool='Read', args=None, status='unknown', agent=''):
        return Event('harness', 'session', agent, str(call), phase, str(self.root), tool,
                     args or {'file_path': 'a.py'}, status, 'same error')

    def complete(self, call, tool='Read', args=None, status='success'):
        e = self.event(call, tool=tool, args=args)
        self.engine.handle(e)
        return self.engine.handle(replace(e, phase='after', status=status))

    def test_warns_once_after_two_successful_reads(self):
        self.complete(1)
        self.complete(2)
        self.assertEqual(self.engine.handle(self.event(3)).action, 'warn')
        self.assertEqual(self.engine.handle(self.event(4)).action, 'allow')

    def test_changed_file_allows_reread(self):
        self.complete(1)
        self.complete(2)
        (self.root / 'a.py').write_text('value = 2\n')
        self.assertEqual(self.engine.handle(self.event(3)).action, 'allow')

    def test_failed_read_does_not_count(self):
        self.complete(1, status='failure')
        self.complete(2, status='failure')
        self.assertEqual(self.engine.handle(self.event(3)).action, 'allow')

    def test_file_changed_during_read_not_cached(self):
        for call in (1, 2):
            e = self.event(call)
            self.engine.handle(e)
            (self.root / 'a.py').write_text(str(call))
            self.engine.handle(replace(e, phase='after', status='success'))
        self.assertEqual(self.engine.handle(self.event(3)).action, 'allow')

    def test_duplicate_completion_and_agents_isolated(self):
        self.complete(1)
        self.engine.handle(self.event(1, phase='after', status='success'))
        self.assertEqual(self.engine.handle(self.event(2)).action, 'allow')
        self.complete(3)
        self.assertEqual(self.engine.handle(self.event(4, agent='child')).action, 'allow')

    def test_failures_warn_and_changes_reset(self):
        args = {'command': 'python3 -m unittest'}
        self.complete(1, 'Bash', args, 'failure')
        self.complete(2, 'Bash', args, 'failure')
        self.assertEqual(self.engine.handle(self.event(3, tool='Bash', args=args)).action, 'warn')
        (self.root / 'a.py').write_text('fixed')
        self.assertEqual(self.engine.handle(self.event(4, tool='Bash', args=args)).action, 'allow')

    def test_success_resets_failure_streak(self):
        args = {'command': 'python3 -m unittest'}
        self.complete(1, 'Bash', args, 'failure')
        self.complete(2, 'Bash', args, 'success')
        self.complete(3, 'Bash', args, 'failure')
        self.assertEqual(self.engine.handle(self.event(4, tool='Bash', args=args)).action, 'allow')

    def test_enforcement_has_escape(self):
        self.cfg['mode'] = 'enforce'
        self.engine = Engine(self.cfg, self.root / 'state.db')
        self.complete(1)
        self.complete(2)
        self.assertEqual(self.engine.handle(self.event(3)).action, 'deny')
        self.assertEqual(self.engine.handle(self.event(4)).action, 'allow')

    def test_incomplete_dependency_coverage_never_denies(self):
        self.cfg['mode'] = 'enforce'
        self.cfg['verification'][0]['complete'] = False
        self.engine = Engine(self.cfg, self.root / 'state.db')
        args = {'command': 'python3 -m unittest'}
        self.complete(1, 'Bash', args, 'failure')
        self.complete(2, 'Bash', args, 'failure')
        self.assertEqual(self.engine.handle(self.event(3, tool='Bash', args=args)).action, 'warn')

    def test_reset_clears_read_memory(self):
        self.complete(1)
        self.complete(2)
        self.engine.handle(self.event('reset', phase='reset'))
        self.assertEqual(self.engine.handle(self.event(3)).action, 'allow')

    def test_shell_operators_are_not_reads(self):
        for command in ['cat a.py | head', 'cat $(echo a.py)', 'cat a.py; true', 'cat *.py']:
            args = {'command': command}
            self.complete(command + '1', 'Bash', args)
            self.complete(command + '2', 'Bash', args)
            self.assertEqual(self.engine.handle(self.event(command + '3', tool='Bash', args=args)).action, 'allow')

    def test_simple_shell_read_detected(self):
        args = {'command': "sed -n '1,2p' a.py"}
        self.complete(1, 'Bash', args)
        self.complete(2, 'Bash', args)
        self.assertEqual(self.engine.handle(self.event(3, tool='Bash', args=args)).action, 'warn')

    def test_feedback_queue_once(self):
        self.complete(1)
        self.complete(2)
        e = replace(self.event(3), platform='hermes')
        # Hermes has separate scope, so establish its own read history.
        for n in (4, 5):
            x = replace(e, call_id=str(n))
            self.engine.handle(x)
            self.engine.handle(replace(x, phase='after', status='success'))
        self.assertEqual(self.engine.handle(e).action, 'warn')
        self.assertEqual(self.engine.handle(replace(e, phase='feedback')).action, 'warn')
        self.assertEqual(self.engine.handle(replace(e, phase='feedback')).action, 'allow')

    def test_reset_clears_all_session_directories(self):
        self.complete(1)
        self.complete(2)
        self.engine.handle(replace(self.event('reset', phase='reset'), cwd='/tmp', agent_id='other'))
        self.assertEqual(self.engine.handle(self.event(3)).action, 'allow')

    def test_hermes_feedback_ignores_working_directory(self):
        e = replace(self.event(1), platform='hermes')
        for n in (1, 2):
            x = replace(e, call_id=str(n))
            self.engine.handle(x)
            self.engine.handle(replace(x, phase='after', status='success'))
        self.engine.handle(replace(e, call_id='3'))
        self.assertEqual(self.engine.handle(replace(e, phase='feedback', cwd='/tmp')).action, 'warn')

    def test_expiry_restores_read(self):
        self.complete(1)
        self.complete(2)
        with self.engine.store.transaction(time.time()) as db:
            db.execute('UPDATE state SET time=0')
        self.assertEqual(self.engine.handle(self.event(3)).action, 'allow')

    def test_parallel_processes_do_not_corrupt_state(self):
        with concurrent.futures.ProcessPoolExecutor(max_workers=3) as pool:
            args = [(str(self.root), self.cfg, n) for n in range(9)]
            self.assertEqual(list(pool.map(parallel_read, args)), ['allow'] * 9)
        self.assertEqual(self.engine.stats()['events'], 18)

    def test_missing_and_large_dependencies_do_not_deny(self):
        self.cfg['mode'] = 'enforce'
        for path in ('missing', 'large'):
            if path == 'large':
                (self.root / path).write_bytes(b'x' * 1048577)
            self.cfg['verification'][0]['inputs'] = [path]
            self.engine = Engine(self.cfg, self.root / 'state.db')
            args = {'command': 'python3 -m unittest'}
            self.complete(path + '1', 'Bash', args, 'failure')
            self.complete(path + '2', 'Bash', args, 'failure')
            self.assertEqual(self.engine.handle(self.event(path + '3', tool='Bash', args=args)).action, 'allow')


def parallel_read(args):
    root, config, n = args
    engine = Engine(config, Path(root) / 'state.db')
    event = Event('harness', f'parallel-{n}', '', str(n), 'before', root, 'Read', {'file_path': 'a.py'})
    decision = engine.handle(event)
    engine.handle(replace(event, phase='after', status='success'))
    return decision.action


if __name__ == '__main__':
    unittest.main()
