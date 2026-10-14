#!/usr/bin/env python3
"""JSON hook runner. Normal events have no stdout; failures never block work."""
import argparse
import json
import sys
from pathlib import Path

from efficiency_hooks.adapters import encode, normalize
from efficiency_hooks.engine import Engine


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--platform', choices=['codex', 'claude', 'hermes', 'harness'], default='harness')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--stats', action='store_true')
    parser.add_argument('--reset', action='store_true')
    options = parser.parse_args()
    try:
        config = json.loads(options.config.read_text()) if options.config else {}
        if not isinstance(config, dict):
            raise ValueError('configuration must be an object')
        database = config.get('database', str(Path.home() / '.local/state/agent-efficiency/state.db'))
        engine = Engine(config, database)
        if options.stats:
            print(json.dumps(engine.stats()))
            return 0
        if options.reset:
            engine.reset()
            return 0
        text = sys.stdin.read(1048577)
        if len(text) > 1048576:
            return 0
        event = normalize(options.platform, json.loads(text), config)
        if event is None:
            return 0
        decision = engine.handle(event)
        output = encode(options.platform, decision, event.phase == 'feedback')
        if output is not None:
            print(json.dumps(output))
        return 0
    except Exception:
        # No payload/exception text: arguments and outputs can contain secrets.
        print('agent-efficiency: skipped hook after input, configuration or state error', file=sys.stderr)
        return 0


if __name__ == '__main__':
    sys.exit(main())
