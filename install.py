#!/usr/bin/env python3
"""Reversible native hook registration. Does not bypass host trust or consent."""
import argparse
import copy
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from efficiency_hooks.recognize import digest

SOURCE = Path(__file__).resolve().parent


def load_yaml(text):
    try:
        from ruamel.yaml import YAML
    except ImportError:
        raise ValueError('Hermes automatic installation requires ruamel.yaml; install requirements-install.txt first') from None
    yaml = YAML()
    yaml.preserve_quotes = True
    value = yaml.load(text)
    return {} if value is None else value


def dump_yaml(value):
    from ruamel.yaml import YAML
    yaml = YAML()
    yaml.preserve_quotes = True
    out = io.StringIO()
    yaml.dump(value, out)
    return out.getvalue()


@dataclass
class Plan:
    target: str
    path: Path
    original: str | None
    rendered: str
    additions: dict
    runtime: Path
    config_path: Path
    defaults: dict
    uninstall: bool


def _owned(handler, command):
    if not isinstance(handler, dict) or handler.get('type', 'command') != 'command':
        return False
    try:
        actual = shlex.split(handler.get('command', ''))
        expected = shlex.split(command)
        return len(actual) == len(expected) and actual[1:] == expected[1:]
    except (ValueError, TypeError):
        return False


def _merge(obj, additions, command, hermes, uninstall):
    if not isinstance(obj, dict):
        raise ValueError('configuration must be a mapping')
    hooks = obj.setdefault('hooks', {})
    if not isinstance(hooks, dict):
        raise ValueError('existing hooks must be a mapping')
    for event, groups in additions.items():
        old = hooks.get(event, [])
        if not isinstance(old, list):
            raise ValueError(f'existing hooks.{event} must be a list')
        cleaned = []
        for group in old:
            if not isinstance(group, dict):
                raise ValueError(f'existing hooks.{event} contains a non-mapping')
            if hermes:
                if not _owned(group, command):
                    cleaned.append(group)
            else:
                handlers = group.get('hooks')
                if not isinstance(handlers, list):
                    raise ValueError(f'existing hooks.{event} group must have a hooks list')
                remaining = [h for h in handlers if not _owned(h, command)]
                if remaining:
                    clone = copy.deepcopy(group)
                    clone['hooks'] = remaining
                    cleaned.append(clone)
        if not uninstall:
            cleaned.extend(groups)
        if cleaned:
            hooks[event] = cleaned
        else:
            hooks.pop(event, None)
    if not hooks:
        obj.pop('hooks', None)
    return obj


def prepare(target, scope, home, project, uninstall=False, hermes_profile=None, interpreter=None):
    home, project = Path(home).resolve(), Path(project).resolve()
    targets = ['codex', 'claude', 'hermes'] if target == 'all' else [target]
    if scope == 'project' and 'hermes' in targets:
        raise ValueError('Hermes uses profiles, not project hook scope; install it separately with --hermes-profile')
    interpreter = str(Path(interpreter or sys.executable).resolve())
    runtime = home / '.local/share/agent-efficiency/runtime'
    plans = []
    for name in targets:
        base = home if scope == 'user' else project
        if name == 'codex':
            path = base / '.codex/hooks.json'
        elif name == 'claude':
            path = base / '.claude/settings.json'
        elif name == 'hermes':
            path = Path(hermes_profile).expanduser().resolve() / 'config.yaml' if hermes_profile else home / '.hermes/config.yaml'
        else:
            raise ValueError('unknown target')
        config_path = home / '.local/share/agent-efficiency/configs' / (name + '-' + digest(str(path))[:12] + '.json')
        command = shlex.join([interpreter, str(runtime / 'hook.py'), '--platform', name, '--config', str(config_path)])
        handler = {'type': 'command', 'command': command, 'timeout': 3}
        if name == 'hermes':
            additions = {event: [{'command': command, 'timeout': 3, 'fail_closed': False}]
                         for event in ('pre_tool_call', 'post_tool_call', 'pre_llm_call', 'on_session_start', 'on_session_end', 'on_session_reset')}
            additions['pre_tool_call'][0]['matcher'] = '^(terminal|read_file)$'
            additions['post_tool_call'][0]['matcher'] = '^(terminal|read_file)$'
        else:
            matcher = '^(Bash|Read|read_file|file_read)$'
            additions = {event: [{'matcher': matcher, 'hooks': [dict(handler)]}]
                         for event in ('PreToolUse', 'PostToolUse')}
            if name == 'claude':
                additions['PostToolUseFailure'] = [{'matcher': matcher, 'hooks': [dict(handler)]}]
            additions['SessionStart'] = [{'hooks': [dict(handler)]}]
            additions['PostCompact'] = [{'hooks': [dict(handler)]}]
        original = path.read_text() if path.exists() else None
        try:
            obj = load_yaml(original or '') if name == 'hermes' else json.loads(original or '{}')
        except (ValueError, TypeError) as exc:
            raise ValueError(f'{name}: cannot safely parse existing config ({type(exc).__name__}); for Hermes check requirements-install.txt') from None
        obj = _merge(obj, additions, command, name == 'hermes', uninstall)
        rendered = dump_yaml(obj) if name == 'hermes' else json.dumps(obj, indent=2, ensure_ascii=False) + '\n'
        defaults = {'mode': 'warn', 'ttl_seconds': 600, 'read_threshold': 2, 'retry_threshold': 2,
                    'database': str(home / '.local/state/agent-efficiency/state.db'), 'verification': [],
                    'hermes_monitor_read_file': False}
        if name == 'hermes':
            defaults['cwd'] = str(project)
            terminal = obj.get('terminal', {})
            defaults['hermes_local_terminal'] = isinstance(terminal, dict) and terminal.get('backend') == 'local'
        plans.append(Plan(name, path, original, rendered, additions, runtime, config_path, defaults, uninstall))
    return plans


def _atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.agent-efficiency-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            os.chmod(name, path.stat().st_mode & 0o777)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def apply_plans(plans, dry_run=False):
    if dry_run:
        return
    # Preflight the entire batch, so stale previews do not overwrite concurrent edits.
    for plan in plans:
        current = plan.path.read_text() if plan.path.exists() else None
        if current != plan.original:
            raise ValueError('configuration changed since preparation; run installer again')
    installed = [p for p in plans if not p.uninstall]
    if installed:
        runtime = installed[0].runtime
        runtime.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE / 'hook.py', runtime / 'hook.py')
        shutil.copytree(SOURCE / 'efficiency_hooks', runtime / 'efficiency_hooks', dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for p in installed:
            if not p.config_path.exists():
                _atomic(p.config_path, json.dumps(p.defaults, indent=2) + '\n')
    written = []
    try:
        for plan in plans:
            if plan.uninstall and plan.original is None:
                continue
            if plan.original == plan.rendered:
                continue
            if plan.original is not None:
                backup = plan.path.with_name(plan.path.name + '.agent-efficiency-backup-' + str(time.time_ns()))
                _atomic(backup, plan.original)
            _atomic(plan.path, plan.rendered)
            written.append(plan)
    except Exception:
        for plan in reversed(written):
            if plan.original is None:
                plan.path.unlink(missing_ok=True)
            else:
                _atomic(plan.path, plan.original)
        raise


def capabilities(target):
    reports = []
    for name in (['codex', 'claude', 'hermes'] if target == 'all' else [target]):
        binary = shutil.which(name)
        report = {'target': name, 'executable_found': bool(binary), 'live_hook_verified': False}
        if binary and name in ('codex', 'claude'):
            try:
                result = subprocess.run([binary, '--version'], capture_output=True, text=True, timeout=5)
                report['version_command_ok'] = result.returncode == 0
                match = __import__('re').search(r'\d+\.\d+\.\d+', result.stdout)
                report['version'] = match[0] if match else 'unknown'
            except (OSError, subprocess.TimeoutExpired):
                report['version'] = 'unknown'
                report['version_command_ok'] = False
        if name == 'hermes':
            source = Path.home() / '.hermes/hermes-agent/agent/shell_hooks.py'
            report['local_shell_hook_source_found'] = source.is_file()
        reports.append(report)
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=['codex', 'claude', 'hermes', 'all'], required=True)
    parser.add_argument('--scope', choices=['user', 'project'], default='user')
    parser.add_argument('--project', type=Path, default=Path.cwd(), help='project root; also Hermes local execution root')
    parser.add_argument('--home', type=Path, default=Path.home(), help='override for isolated installation tests')
    parser.add_argument('--hermes-profile', type=Path, help='profile directory containing config.yaml')
    parser.add_argument('--python', dest='interpreter', help='absolute stable Python 3.10+ interpreter path')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--uninstall', action='store_true')
    parser.add_argument('--check', action='store_true', help='report installed executables; does not start model sessions')
    args = parser.parse_args()
    if args.check:
        print(json.dumps(capabilities(args.target), indent=2))
        return 0
    try:
        plans = prepare(args.target, args.scope, args.home, args.project, args.uninstall,
                        args.hermes_profile, args.interpreter)
        # Print only this package's additions and destinations, never existing config.
        for p in plans:
            print(json.dumps({'target': p.target, 'destination': str(p.path), 'guard_config': str(p.config_path),
                              'operation': 'uninstall' if p.uninstall else 'install', 'registration': p.additions}, indent=2))
        apply_plans(plans, args.dry_run)
        print('Preview only; no files written.' if args.dry_run else
              'Registrations updated. Restart the host and complete its native hook trust/consent flow. Live activation is not verified.')
        return 0
    except (ValueError, OSError) as exc:
        # Only controlled ValueErrors are printed; OS errors can contain unrelated paths.
        print(str(exc) if isinstance(exc, ValueError) else 'Installation failed; configuration writes were rolled back.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
