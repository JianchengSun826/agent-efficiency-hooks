import json
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from install import prepare, apply_plans, load_yaml, capabilities


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hook test ')
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.project = self.home / 'project space'
        self.project.mkdir()

    def plans(self, target='claude', uninstall=False):
        return prepare(target, 'user', self.home, self.project, uninstall=uninstall)

    def test_dry_run_does_not_write(self):
        plans = self.plans()
        apply_plans(plans, dry_run=True)
        self.assertFalse((self.home / '.claude').exists())
        self.assertFalse((self.home / '.local').exists())

    def test_preserves_existing_hooks_and_is_idempotent(self):
        file = self.home / '.claude/settings.json'
        file.parent.mkdir()
        original = {'env': {'SOMETHING': 'private'}, 'hooks': {'PreToolUse': [{'hooks': [{'type': 'command', 'command': 'echo existing'}]}]}}
        file.write_text(json.dumps(original))
        apply_plans(self.plans())
        first = json.loads(file.read_text())
        apply_plans(self.plans())
        self.assertEqual(json.loads(file.read_text()), first)
        self.assertEqual(first['env'], original['env'])
        self.assertEqual(first['hooks']['PreToolUse'][0], original['hooks']['PreToolUse'][0])
        command = first['hooks']['PreToolUse'][1]['hooks'][0]['command']
        self.assertTrue(Path(shlex.split(command)[1]).is_file())
        self.assertTrue(list(file.parent.glob('settings.json.agent-efficiency-backup-*')))

    def test_uninstall_only_removes_owned_hooks(self):
        apply_plans(self.plans())
        file = self.home / '.claude/settings.json'
        obj = json.loads(file.read_text())
        obj['hooks']['PreToolUse'].append({'hooks': [{'type': 'command', 'command': 'echo agent-efficiency unrelated'}]})
        file.write_text(json.dumps(obj))
        apply_plans(self.plans(uninstall=True))
        obj = json.loads(file.read_text())
        self.assertEqual(obj['hooks']['PreToolUse'], [{'hooks': [{'type': 'command', 'command': 'echo agent-efficiency unrelated'}]}])

    def test_invalid_configuration_aborts_before_any_write(self):
        file = self.home / '.claude/settings.json'
        file.parent.mkdir()
        file.write_text('{bad json')
        with self.assertRaises(ValueError):
            self.plans('all')
        self.assertFalse((self.home / '.codex').exists())

    def test_codex_and_project_scope(self):
        plans = prepare('codex', 'project', self.home, self.project)
        apply_plans(plans)
        obj = json.loads((self.project / '.codex/hooks.json').read_text())
        self.assertIn('PostCompact', obj['hooks'])
        self.assertNotIn('PostToolUseFailure', obj['hooks'])

    def test_hermes_comment_preserving_merge(self):
        profile = self.home / '.hermes'
        profile.mkdir()
        file = profile / 'config.yaml'
        file.write_text('# keep this comment\nmodel: example\nhooks:\n  post_tool_call:\n    - command: echo existing\n')
        plans = self.plans('hermes')
        apply_plans(plans)
        self.assertIn('# keep this comment', file.read_text())
        obj = load_yaml(file.read_text())
        self.assertEqual(obj['hooks']['post_tool_call'][0]['command'], 'echo existing')
        self.assertIn('pre_llm_call', obj['hooks'])
        apply_plans(self.plans('hermes', uninstall=True))
        obj = load_yaml(file.read_text())
        self.assertEqual(obj['hooks']['post_tool_call'], [{'command': 'echo existing'}])

    def test_configure_without_overwriting_on_reinstall(self):
        apply_plans(self.plans())
        config = next((self.home / '.local/share/agent-efficiency/configs').glob('*.json'))
        obj = json.loads(config.read_text())
        obj['mode'] = 'observe'
        config.write_text(json.dumps(obj))
        apply_plans(self.plans())
        self.assertEqual(json.loads(config.read_text())['mode'], 'observe')

    def test_interpreter_change_does_not_duplicate_registration(self):
        apply_plans(self.plans())
        plans = prepare('claude', 'user', self.home, self.project, interpreter='/usr/local/bin/python3')
        apply_plans(plans)
        obj = json.loads((self.home / '.claude/settings.json').read_text())
        self.assertEqual(len(obj['hooks']['PreToolUse']), 1)

    def test_stale_preview_refuses_write(self):
        plans = self.plans()
        file = self.home / '.claude/settings.json'
        file.parent.mkdir()
        file.write_text('{}')
        with self.assertRaises(ValueError):
            apply_plans(plans)
        self.assertEqual(file.read_text(), '{}')

    def test_broken_cli_reported_without_leaking_stderr(self):
        script = self.home / 'broken-codex'
        script.write_text('#!/bin/sh\necho private-details >&2\nexit 1\n')
        script.chmod(0o700)
        with patch('install.shutil.which', return_value=str(script)):
            report = capabilities('codex')[0]
        self.assertFalse(report['version_command_ok'])
        self.assertNotIn('private-details', json.dumps(report))

    def test_configuration_batch_failure_rolls_back(self):
        codex = self.home / '.codex/hooks.json'
        claude = self.home / '.claude/settings.json'
        codex.parent.mkdir()
        claude.parent.mkdir()
        codex.write_text('{}')
        claude.write_text('{}')
        plans = prepare('codex', 'user', self.home, self.project) + self.plans()
        import install
        original = install._atomic
        def fail_second(path, text):
            if path.resolve() == claude.resolve():
                raise OSError('simulated disk error')
            original(path, text)
        with patch('install._atomic', side_effect=fail_second):
            with self.assertRaises(OSError):
                apply_plans(plans)
        self.assertEqual(codex.read_text(), '{}')
        self.assertEqual(claude.read_text(), '{}')


if __name__ == '__main__':
    unittest.main()
