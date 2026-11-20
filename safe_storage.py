"""Non-destructive readers and atomic replacement for local application data."""
import base64
import copy
import datetime
import hashlib
import io
import json
import math
import os
import tempfile

from PIL import Image

from dictation_quality import CRITERIA


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def atomic_write_json(path, data):
    """Serialize before touching disk; replace only after a complete flushed write."""
    encoded = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
    # Preserve the pre-repair original once, including malformed/partial content.
    # Exclusive creation prevents later saves from replacing this recovery copy.
    if os.path.exists(path):
        with open(path, 'rb') as original:
            original_bytes = original.read()
        recovery = str(path) + '.recovery'
        try:
            previous = json.loads(original_bytes)
            damaged = type(previous) is not type(data)
        except (ValueError, UnicodeError):
            damaged = True
        if damaged:
            recovery = str(path) + '.damaged-' + hashlib.sha256(original_bytes).hexdigest() + '.recovery'
        try:
            with open(recovery, 'xb') as backup:
                backup.write(original_bytes)
                backup.flush()
                os.fsync(backup.fileno())
        except FileExistsError:
            pass
    directory = os.path.dirname(os.path.abspath(path))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory, prefix='.save-', suffix='.tmp', delete=False) as stream:
            temporary = stream.name
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def load_json(path, default):
    """Validate the outer shape only; domain validators handle nested content."""
    try:
        with open(path, encoding='utf-8') as stream:
            value = json.load(stream)
        if type(value) is type(default):
            return value
    except (OSError, ValueError, UnicodeError):
        pass
    return copy.deepcopy(default)


def _history_app(value):
    name = value.get('name') if isinstance(value, dict) else None
    if (not isinstance(name, str) or not name or len(name) > 80
            or name != name.strip() or not name.isprintable() or any(c in name for c in '/\\:')):
        return None
    app = {'name': name}
    icon = value.get('icon')
    if isinstance(icon, str) and 0 < len(icon) <= 4608:
        try:
            data = base64.b64decode(icon, validate=True)
            if data.startswith(b'\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR\x00\x00\x00\x20\x00\x00\x00\x20'):
                with Image.open(io.BytesIO(data), formats=['PNG']) as image:
                    image.verify()
                app['icon'] = icon
        except Exception:
            pass
    return app


def read_history(path, limit=500, *, include_app=True):
    rows, skipped = [], 0
    try:
        with open(path, 'rb') as stream:
            for line in stream:
                try:
                    row = json.loads(line.decode('utf-8'))
                    if isinstance(row, dict):
                        row = dict(row)
                        row.setdefault('raw', '')
                        row.setdefault('kind', 'dictate')
                    if not isinstance(row, dict) or any(not isinstance(row.get(k), str) for k in ('text', 'raw', 'ts', 'kind')):
                        raise ValueError('Invalid history shape')
                    datetime.datetime.fromisoformat(row['ts'])
                    row = dict(row)
                    for key in ('secs', 'latency', 'cost'):
                        if not _number(row.get(key)):
                            row[key] = 0
                    if not include_app:
                        row.pop('app', None)
                    rows.append(row)
                except (ValueError, UnicodeError, TypeError, OverflowError):
                    skipped += 1
    except OSError:
        pass
    rows = rows[-limit:] if limit > 0 else []
    if include_app:
        for row in rows:
            app = _history_app(row.pop('app', None))
            if app:
                row['app'] = app
    return rows, skipped


def valid_setting(key, value):
    if key == 'hotkey':
        from hotkey_bindings import valid_keyboard_binding
        from controller_hotkey import controller_binding
        return valid_keyboard_binding(value) or controller_binding(value)
    if key == 'speech_model':
        from speech_runtime import SPEECH_MODEL_IDS
        return isinstance(value, str) and value in SPEECH_MODEL_IDS
    prompt_routes = {'anthropic', 'openai', 'gemini', 'local'}
    options = {'style': {'tighten', 'clean'},
               'target': {'claude_code', 'general'}, 'judge': prompt_routes, 'engine': prompt_routes,
               'judge_model': {'claude-haiku-5-5', 'claude-sonnet-5-5', 'claude-opus-5-5'}}
    if key in options:
        return isinstance(value, str) and value in options[key]
    if key in ('cleanup', 'learn', 'cloud_enabled', 'live_preview'):
        return type(value) is bool
    if key == 'local_model':
        return isinstance(value, str) and bool(value.strip()) and len(value) <= 200
    if key in ('engine_local_model', 'judge_local_model'):
        return value == '' or valid_setting('local_model', value)
    if key == 'vocab':
        return isinstance(value, str)
    if key == 'microphone':
        return value is None or isinstance(value, str)
    return False


def validated_settings(data, defaults):
    result = copy.deepcopy(defaults)
    if isinstance(data, dict):
        result.update({k: v for k, v in data.items() if k in defaults and valid_setting(k, v)})
    return result


def validated_favorites(data):
    result = []
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, dict) or any(not isinstance(row.get(k), str) for k in ('name', 'text')):
            continue
        clean = dict(row)
        clean['ts'] = row.get('ts') if isinstance(row.get('ts'), str) else ''
        clean['uses'] = row.get('uses', 0) if _number(row.get('uses', 0)) else 0
        result.append(clean)
    return result


def validated_learned(data, defaults):
    result = copy.deepcopy(defaults)
    if not isinstance(data, dict):
        return result
    result['dismissed'] = [v for v in data.get('dismissed', []) if isinstance(v, str)] if isinstance(data.get('dismissed'), list) else []
    for key in ('replacements', 'removed_headings'):
        value = data.get(key)
        if isinstance(value, dict):
            result[key] = {k: v for k, v in value.items() if isinstance(k, str) and _number(v)}
    kept = data.get('kept')
    if isinstance(kept, list):
        result['kept'] = [dict(v, tags=v.get('tags', v.get('xml', False))) for v in kept if isinstance(v, dict) and _number(v.get('words')) and type(v.get('tags', v.get('xml', False))) is bool][-200:]
    criteria = data.get('criteria')
    if isinstance(criteria, dict):
        result['criteria'] = {k: list(v) for k, v in criteria.items() if k in CRITERIA and isinstance(v, list) and len(v) == 2 and all(_number(n) for n in v) and v[1] > 0 and v[0] <= 5 * v[1]}
    return result
