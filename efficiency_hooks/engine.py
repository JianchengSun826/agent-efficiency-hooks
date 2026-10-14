import time
from pathlib import Path

from .model import Decision
from .recognize import digest, operation
from .store import Store


class Engine:
    def __init__(self, config, database):
        self.config = config
        self.mode = config.get('mode', 'warn')
        if self.mode not in ('observe', 'warn', 'enforce'):
            raise ValueError('invalid mode')
        self.store = Store(database, max(1, int(config.get('ttl_seconds', 600))),
                           max(100, int(config.get('max_records', 5000))))

    def handle(self, event):
        if not event.session_id or not isinstance(event.args, dict):
            return Decision()
        session_scope = digest([event.platform, event.session_id]) + ':'
        scope = session_scope + digest([event.agent_id, str(Path(event.cwd).resolve())])
        queue_scope = session_scope + digest(['feedback', event.agent_id])
        now = time.time()
        with self.store.transaction(now) as db:
            self.store.count(db, 'events')
            if event.phase == 'reset':
                db.execute('DELETE FROM state WHERE scope LIKE ?', (session_scope + '%',))
                return Decision()
            if event.phase == 'feedback':
                value = self.store.get(db, queue_scope, 'feedback')
                db.execute('DELETE FROM state WHERE scope=? AND key=?', (queue_scope, 'feedback'))
                return Decision('warn', '\n'.join(value)) if value else Decision()
            if event.phase not in ('before', 'after') or not event.call_id:
                return Decision()
            pending_key = 'call:' + digest(event.call_id)
            prior = self.store.get(db, scope, pending_key)
            if prior is not None:
                if event.phase == 'before' or prior.get('done'):
                    return Decision()
            op = operation(event, self.config)
            if event.phase == 'after':
                if prior is None:
                    return Decision()
                self.store.put(db, scope, pending_key, {'done': True}, now)
                if not op or op != prior.get('op') or event.status not in ('success', 'failure'):
                    return Decision()
                record_key = 'op:' + op['base']
                record = self.store.get(db, scope, record_key) or {}
                signature = digest(event.error_signature) if op['kind'] == 'retry' else ''
                same = record.get('version') == op['version'] and record.get('signature') == signature
                n = record.get('count', 0) if same else 0
                if op['kind'] == 'read':
                    if event.status != 'success':
                        return Decision()
                    n += 1
                elif event.status == 'success':
                    n = 0
                elif not event.error_signature:
                    return Decision()
                else:
                    n += 1
                self.store.put(db, scope, record_key, {'version': op['version'], 'signature': signature,
                    'count': n, 'notified': record.get('notified', False) if same and n else False,
                    'denied_at': record.get('denied_at', 0) if same and n else 0}, now)
                return Decision()
            if not op:
                return Decision()
            self.store.put(db, scope, pending_key, {'op': op}, now)
            record = self.store.get(db, scope, 'op:' + op['base'])
            threshold = max(2, int(self.config.get('read_threshold' if op['kind'] == 'read' else 'retry_threshold', 2)))
            if not record or record.get('version') != op['version'] or record.get('count', 0) < threshold:
                return Decision()
            self.store.count(db, 'repeat_candidates')
            if self.mode == 'observe':
                return Decision()
            cooldown = max(30, int(self.config.get('deny_cooldown_seconds', 300)))
            deny = self.mode == 'enforce' and op['complete'] and now - record.get('denied_at', 0) >= cooldown
            if not deny and record.get('notified'):
                return Decision()
            reason = ('Efficiency hook: this exact file range was read successfully twice and is unchanged. '
                      'Use the prior result or search a different range; reset the guard if context was lost.'
                      if op['kind'] == 'read' else
                      'Efficiency hook: this configured check failed repeatedly with the same error and unchanged inputs. '
                      'Inspect the failure or change the experiment before repeating it.')
            record['notified'] = True
            if deny:
                record['denied_at'] = now
                self.store.put(db, scope, pending_key, {'done': True}, now)
            self.store.put(db, scope, 'op:' + op['base'], record, now)
            self.store.count(db, 'denials' if deny else 'warnings')
            if event.platform == 'hermes' and not deny:
                queue = self.store.get(db, queue_scope, 'feedback') or []
                self.store.put(db, queue_scope, 'feedback', list(dict.fromkeys(queue + [reason]))[:2], now)
            return Decision('deny' if deny else 'warn', reason)

    def stats(self):
        with self.store.transaction(time.time()) as db:
            return dict(db.execute('SELECT name,count FROM metrics'))

    def reset(self):
        with self.store.transaction(time.time()) as db:
            db.execute('DELETE FROM state')
