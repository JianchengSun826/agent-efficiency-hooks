import hashlib
import json
import re
import shlex
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def fingerprint(paths, cwd, max_bytes=1048576):
    """Bounded literal-file fingerprints. No globbing, directory walks or commands."""
    if not isinstance(paths, list) or not 1 <= len(paths) <= 64:
        return None
    result, total = [], 0
    for name in paths:
        if not isinstance(name, str) or not name:
            return None
        p = (Path(cwd) / name).resolve()
        try:
            before = p.stat()
            if not p.is_file() or before.st_size > max_bytes:
                return None
            total += before.st_size
            if total > 4 * max_bytes:
                return None
            with p.open('rb') as f:
                content = f.read(max_bytes + 1)
            after = p.stat()
            if len(content) > max_bytes or (before.st_mtime_ns, before.st_size, before.st_ino) != (after.st_mtime_ns, after.st_size, after.st_ino):
                return None
            result.append((str(p), hashlib.sha256(content).hexdigest()))
        except OSError:
            return None
    return digest(result)


def read_spec(tool, args):
    if tool in ('Read', 'read_file', 'file_read'):
        path = args.get('file_path', args.get('path'))
        if not isinstance(path, str) or not path:
            return None
        # Retain all range parameters; unsupported parameters prevent enforcement by misidentification.
        allowed = {'file_path', 'path', 'offset', 'limit', 'start_line', 'end_line', 'line_start', 'line_end'}
        if set(args) - allowed:
            return None
        return path, {k: v for k, v in args.items() if k not in ('file_path', 'path')}
    if tool not in ('Bash', 'terminal', 'exec_command'):
        return None
    command = args.get('command', args.get('cmd'))
    if not isinstance(command, str) or any(x in command for x in ('|', '&', ';', '$', '`', '<', '>', '\n', '*', '?', '[', '(', ')')):
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if len(words) == 2 and words[0] == 'cat' and not words[1].startswith('-'):
        return words[1], {'shell_range': 'all'}
    if len(words) == 4 and words[0] in ('head', 'tail') and words[1] == '-n' and words[2].isdigit() and not words[3].startswith('-'):
        return words[3], {'shell_range': words[:3]}
    if len(words) == 4 and words[:2] == ['sed', '-n'] and re.fullmatch(r'\d+(,\d+)?p', words[2]) and not words[3].startswith('-'):
        return words[3], {'shell_range': words[:3]}
    return None


def operation(event, config):
    spec = read_spec(event.tool, event.args)
    if spec:
        path, extent = spec
        version = fingerprint([path], event.cwd)
        if version:
            return {'kind': 'read', 'base': digest(['read', str((Path(event.cwd) / path).resolve()), extent]),
                    'version': version, 'complete': True}
    if event.tool not in ('Bash', 'terminal', 'exec_command'):
        return None
    command = event.args.get('command', event.args.get('cmd'))
    for rule in config.get('verification', []):
        if isinstance(rule, dict) and command == rule.get('command') and isinstance(command, str):
            version = fingerprint(rule.get('inputs'), event.cwd)
            if version:
                return {'kind': 'retry', 'base': digest(['retry', command]), 'version': version,
                        'complete': rule.get('complete') is True}
    return None
