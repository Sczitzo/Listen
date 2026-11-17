"""Local workspace recovery and content-free estimated operation costs.

Workspace clear replaces current state; recovery backups are not securely erased.
Cost totals are estimates recorded by callers, not provider billing truth.
"""
import datetime
import json
import math
import os
import re
import threading
import uuid
from pathlib import Path

from safe_storage import atomic_write_json

_LOCK = threading.RLock()


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _empty_active():
    return {'id': str(uuid.uuid4()), 'revisions': [], 'cur': -1,
            'composer': '', 'pending': None}


def _empty():
    return {'version': 1, 'active': _empty_active(), 'archived': [], 'retained': []}


def _plain(value, depth=0):
    """Copy bounded JSON values, rejecting objects and non-finite numbers."""
    if depth > 20:
        raise ValueError('Pending state is too deeply nested')
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is list:
        return [_plain(v, depth + 1) for v in value]
    if type(value) is dict and all(type(k) is str for k in value):
        return {k: _plain(v, depth + 1) for k, v in value.items()}
    raise ValueError('Invalid pending state')


def _active(value):
    if type(value) is not dict or type(value.get('id')) is not str or not value['id']:
        return None
    result = _empty_active()
    result['id'] = value['id']
    revisions = value.get('revisions')
    original_indices = []
    if type(revisions) is list:
        for index, row in enumerate(revisions):
            if type(row) is not dict or type(row.get('text')) is not str:
                continue
            result['revisions'].append({'text': row['text'],
                'kind': row.get('kind') if type(row.get('kind')) is str else 'raw',
                'by': row.get('by') if type(row.get('by')) is str else '',
                'cost': row.get('cost') if _number(row.get('cost')) else None,
                'score': None, 'stale': True})
            original_indices.append(index)
    current = value.get('cur')
    result['cur'] = original_indices.index(current) if type(current) is int and current in original_indices else len(original_indices) - 1
    if type(value.get('composer')) is str:
        result['composer'] = value['composer']
    if type(value.get('pending')) is dict:
        try:
            result['pending'] = _plain(value['pending'])
        except ValueError:
            pass
    return result


def _validated(value):
    result = _empty()
    if type(value) is not dict or type(value.get('version')) is not int or value['version'] != 1:
        return result
    result['active'] = _active(value.get('active')) or result['active']
    if type(value.get('archived')) is list:
        result['archived'] = [clean for row in value['archived'] if (clean := _active(row)) is not None]
    if type(value.get('retained')) is list:
        result['retained'] = [{k: row[k] for k in ('id', 'text', 'message')}
            for row in value['retained'] if type(row) is dict and
            type(row.get('id')) in (str, int) and all(type(row.get(k)) is str for k in ('text', 'message'))]
    return result


def _strict_active(active):
    if (type(active) is not dict or type(active.get('id')) is not str
            or not active['id'] or type(active.get('revisions')) is not list
            or type(active.get('cur')) is not int
            or type(active.get('composer')) is not str
            or 'pending' not in active
            or (active['pending'] is not None and type(active['pending']) is not dict)):
        raise ValueError('Workspace active state has an invalid shape')
    if not (-1 <= active['cur'] < len(active['revisions'])) or (active['revisions'] and active['cur'] == -1):
        raise ValueError('Workspace current revision is invalid')
    for row in active['revisions']:
        if (type(row) is not dict or type(row.get('text')) is not str
                or type(row.get('kind')) is not str or type(row.get('by')) is not str
                or 'cost' not in row or (row['cost'] is not None and not _number(row['cost']))):
            raise ValueError('Workspace revision has an invalid shape')
    if active['pending'] is not None:
        _plain(active['pending'])


def _strict(state):
    if type(state) is not dict or type(state.get('version')) is not int or state['version'] != 1:
        raise ValueError('Workspace state must have version 1')
    _strict_active(state.get('active'))
    if type(state.get('archived')) is not list or type(state.get('retained')) is not list:
        raise ValueError('Workspace archives and retained items must be lists')
    for active in state['archived']:
        _strict_active(active)
    for row in state['retained']:
        if (type(row) is not dict or type(row.get('id')) not in (str, int)
                or any(type(row.get(k)) is not str for k in ('text', 'message'))):
            raise ValueError('Workspace retained item has an invalid shape')


class WorkspaceStore:
    """Defensively restored workspace, with no automatic archive expiry."""
    def __init__(self, directory):
        self.path = Path(directory) / 'workspace.json'
        self.load_status = 'new'

    def load(self):
        with _LOCK:
            try:
                with self.path.open(encoding='utf-8') as stream:
                    raw = json.load(stream)
            except FileNotFoundError:
                self.load_status = 'missing'
                return _empty()
            except (ValueError, UnicodeError):
                self.load_status = 'corrupt'
                return _empty()
            except OSError:
                self.load_status = 'read-error'
                return _empty()
            self.load_status = 'ok'
            if type(raw) is dict and type(raw.get('version')) is int and raw['version'] != 1:
                self.load_status = 'unsupported'
            else:
                try:
                    _strict(raw)
                except ValueError:
                    self.load_status = 'corrupt'
            return _validated(raw)

    def save(self, state):
        _strict(state)
        with _LOCK:
            clean = _validated(state)
            try:
                with self.path.open('rb') as original:
                    original.read()
            except FileNotFoundError:
                pass
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.path, clean)

    def clear(self):
        """Reset workspace; does not securely erase recovery backups."""
        self.save(_empty())


def _event(value):
    if type(value) is not dict:
        raise ValueError('Cost event must be a dictionary')
    if any(type(value.get(k)) is not str or not value[k].strip() for k in ('provider', 'model', 'operation')):
        raise ValueError('Provider, model and operation must be nonempty strings')
    cost = value.get('cost')
    if cost is not None and not _number(cost):
        raise ValueError('Cost must be finite and nonnegative, or unknown')
    return {k: value[k] for k in ('provider', 'model', 'operation')} | {'cost': cost}


class CostLedger:
    """Append-only estimates independent of workspace clearing; no prompt content."""
    def __init__(self, directory):
        self.path = Path(directory) / 'costs.jsonl'
        self.last_skipped = 0

    def append(self, event):
        clean = _event(event)
        event_id = event.get('id', str(uuid.uuid4()))
        if type(event_id) is not str:
            raise ValueError('Event ID must be a UUID string')
        try:
            event_id = str(uuid.UUID(event_id))
        except ValueError as error:
            raise ValueError('Event ID must be a UUID string') from error
        clean.update(id=event_id, ts=datetime.datetime.now(datetime.timezone.utc).isoformat())
        encoded = json.dumps(clean, ensure_ascii=False, allow_nan=False).encode('utf-8') + b'\n'
        with _LOCK:
            for existing in self.entries():
                if existing['id'] == event_id:
                    if _event(existing) != _event(clean):
                        raise ValueError('Event ID already belongs to a different event')
                    return event_id
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('a+b') as stream:
                stream.seek(0, os.SEEK_END)
                if stream.tell():
                    stream.seek(-1, os.SEEK_END)
                    if stream.read(1) != b'\n':
                        stream.write(b'\n')
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        return clean['id']

    def entries(self):
        result = []
        with _LOCK:
            self.last_skipped = 0
            try:
                with self.path.open('rb') as stream:
                    for line in stream:
                        try:
                            row = json.loads(line.decode('utf-8'))
                            clean = _event(row)
                            if type(row.get('id')) is not str or type(row.get('ts')) is not str:
                                raise ValueError('Invalid ledger identity')
                            uuid.UUID(row['id'])
                            timestamp = datetime.datetime.fromisoformat(row['ts'])
                            if timestamp.tzinfo is None:
                                raise ValueError('Ledger timestamp lacks timezone')
                            clean.update(id=row['id'], ts=timestamp.astimezone(datetime.timezone.utc).isoformat())
                            result.append(clean)
                        except (ValueError, TypeError, UnicodeError, OverflowError):
                            self.last_skipped += 1
            except FileNotFoundError:
                pass
        return result

    def summary(self, month):
        """Group estimates by the caller machine's local calendar month."""
        if type(month) is not str or re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', month) is None:
            raise ValueError('Month must be YYYY-MM')
        rows = [row for row in self.entries()
                if datetime.datetime.fromisoformat(row['ts']).astimezone().strftime('%Y-%m') == month]
        return {'amount': math.fsum(row['cost'] for row in rows if row['cost'] is not None),
                'operations': len(rows), 'unknown': sum(row['cost'] is None for row in rows),
                'unreadable': self.last_skipped}
