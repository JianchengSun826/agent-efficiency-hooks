import json
import os
import re
from dataclasses import asdict

from .model import Event


def _response(value):
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else value
        except ValueError:
            return value
    return value


def _status(platform, payload, tool):
    event = payload.get('hook_event_name')
    if platform == 'claude' and event == 'PostToolUseFailure':
        return ('unknown', '') if payload.get('is_interrupt') else ('failure', str(payload.get('error', '')))
    value = _response(payload.get('result') if platform == 'hermes' else payload.get('tool_response'))
    if platform == 'hermes' and payload.get('status') in ('blocked', 'cancelled', 'timeout'):
        return 'unknown', ''
    if isinstance(value, dict):
        if value.get('interrupted') or value.get('status') in ('running', 'background', 'blocked', 'unchanged', 'timeout') or value.get('dedup'):
            return 'unknown', ''
        # Do not mistake a successful terminal dispatch for a successful command.
        code = value.get('exit_code', value.get('exitCode'))
        if type(code) is int:
            error = str(value.get('error') or value.get('stderr') or value.get('output') or f'exit {code}')
            return ('success', '') if code == 0 else ('failure', error)
        if value.get('session_id') or value.get('sessionId') or value.get('session_id') == 0:
            return 'unknown', ''
        if value.get('isError') is True or value.get('error'):
            return 'failure', str(value.get('error') or value.get('content') or 'tool error')
    if tool in ('Bash', 'terminal', 'exec_command'):
        if platform == 'claude' and event == 'PostToolUse' and isinstance(value, dict) and 'stdout' in value:
            return 'success', ''
        if isinstance(value, str):
            # Codex model-facing unified-exec envelope; avoid fuzzy text classification.
            match = re.search(r'^Process exited with code (-?\d+)\s*$', value, re.M)
            if match:
                code = int(match[1])
                text = value.split('Final output:', 1)[-1].strip()
                return ('success', '') if code == 0 else ('failure', text or f'exit {code}')
        return 'unknown', ''
    if platform == 'claude' and event == 'PostToolUse':
        return 'success', ''
    if platform == 'hermes':
        if payload.get('status') == 'error':
            return 'failure', str(payload.get('error_message') or 'tool error')
        if payload.get('status') == 'success':
            return 'success', ''
    if platform == 'codex' and isinstance(value, dict) and 'isError' in value:
        return 'success', ''
    return 'unknown', ''


def normalize(platform, payload, config=None):
    config = config or {}
    if not isinstance(payload, dict) or not isinstance(payload.get('session_id'), str) or not payload['session_id']:
        return None
    if platform == 'harness':
        try:
            return Event(**payload)
        except (TypeError, ValueError):
            return None
    if platform == 'hermes' and isinstance(payload.get('extra'), dict):
        # Hermes shell wire is distinct from its Python callback kwargs.
        payload = {**payload['extra'], **payload}
    name = payload.get('hook_event_name')
    phases = {'PreToolUse': 'before', 'PostToolUse': 'after', 'PostToolUseFailure': 'after',
              'PostCompact': 'reset', 'SessionStart': 'reset',
              'pre_tool_call': 'before', 'post_tool_call': 'after', 'pre_llm_call': 'feedback',
              'on_session_start': 'reset', 'on_session_end': 'reset', 'on_session_reset': 'reset'}
    phase = phases.get(name)
    if phase is None:
        return None
    args = (payload.get('tool_input', payload.get('args')) if platform == 'hermes' else payload.get('tool_input')) or {}
    if not isinstance(args, dict):
        return None
    tool = payload.get('tool_name', '')
    if platform == 'hermes' and tool == 'read_file' and not config.get('hermes_monitor_read_file', False):
        return None
    # Hermes shell cwd is the HOST process cwd, not its persistent terminal cwd.
    # Do not fingerprint a remote backend or guess a persistent session directory.
    if platform == 'hermes' and tool == 'terminal' and phase in ('before', 'after'):
        if config.get('hermes_local_terminal') is not True or not isinstance(args.get('workdir'), str) or not os.path.isabs(args['workdir']):
            return None
    cwd = args.get('workdir') or args.get('cwd') or payload.get('cwd') or config.get('cwd')
    if not cwd:
        if phase in ('before', 'after'):
            return None
        cwd = os.getcwd()
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return None
    # Hermes task_id rotates each turn; session_id already isolates child sessions.
    agent = payload.get('agent_id', '')
    call = payload.get('tool_call_id', '') if platform == 'hermes' else payload.get('tool_use_id', '')
    status, error = _status(platform, payload, tool) if phase == 'after' else ('unknown', '')
    return Event(platform, payload['session_id'], str(agent), str(call), phase, cwd, tool, args, status, error)


def encode(platform, decision, feedback=False):
    if platform == 'harness':
        return asdict(decision)
    if decision.action == 'allow':
        return None
    if platform == 'hermes':
        if decision.action == 'deny':
            return {'action': 'block', 'message': decision.reason}
        return {'context': decision.reason} if feedback else None
    output = {'hookEventName': 'PreToolUse'}
    if decision.action == 'deny':
        output.update(permissionDecision='deny', permissionDecisionReason=decision.reason)
    else:
        output['additionalContext'] = decision.reason
    return {'hookSpecificOutput': output}
