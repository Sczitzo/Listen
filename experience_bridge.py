"""Recovery and review APIs. No device, provider or startup side effects."""
import copy
import uuid
from workspace_state import WorkspaceStore, CostLedger
from dictation_quality import validate_score


def ensure(r):
    defaults = {'_workspace_store': lambda: WorkspaceStore(r.APP_DIR), '_cost_ledger': lambda: CostLedger(r.APP_DIR),
        '_workspace_id': lambda: str(uuid.uuid4()), '_archived': list, '_composer': lambda: '',
        '_composer_edit_id': lambda: -1, '_pending': lambda: None, '_save_error': lambda: '',
        '_session_cost': lambda: 0, '_cost_error': lambda: '', '_favorite_undo': dict,
        '_session_call_ids': set}
    for key, factory in defaults.items():
        if not hasattr(r, key): setattr(r, key, factory())


def _snapshot(r):
    # shortcut: review rounds live in memory, add versioned recovery if history must survive Quit.
    pending = ({k: copy.deepcopy(v) for k, v in r._pending.items() if k != 'versions'}
               if r._pending else None)
    return {'id': r._workspace_id, 'revisions': [dict(text=x['text'], kind=x['kind'],
        by=x.get('by') or '', cost=x.get('cost') or 0, score=None, stale=True) for x in r._lab['revs']],
        'cur': r._lab['cur'], 'composer': r._composer, 'pending': pending}


def persist_workspace(r):
    ensure(r)
    with r._lab_lock, r._data_lock:
        try:
            r._workspace_store.save({'version': 1, 'active': _snapshot(r),
                'archived': copy.deepcopy(r._archived), 'retained': copy.deepcopy(r._retained)})
            r._save_error = ''
            return True
        except Exception:
            r._save_error = 'Draft recovery could not be saved. Your work remains open; try again.'
            return False



def commit_workspace_transition(r, change):
    """Commit a workspace action, rolling back every owned field on save failure.

    Keep both locks through mutation, save and rollback so delayed work cannot
    observe or publish an uncommitted deletion generation.
    """
    ensure(r)
    fields = ('_lab', '_workspace_id', '_composer', '_pending', '_archived',
        '_retained', '_lab_epoch', '_lab_draft_id', '_lab_edit_id',
        '_composer_edit_id', '_recovery_generation', '_retained_ver')
    with r._lab_lock, r._data_lock:
        before = {key: copy.deepcopy(getattr(r, key)) for key in fields if hasattr(r, key)}
        # A judge already in flight owns the original revision object. Rollback
        # must restore its identity as well as values, so its valid completion
        # can still attach after a failed action.
        lab = r._lab
        revisions = lab['revs']
        revision_refs = list(revisions)
        try:
            change()
            if persist_workspace(r): return True
        except Exception:
            r._save_error = 'Draft recovery could not be saved. Your work remains open; try again.'
        lab_values = before['_lab']
        for revision, values in zip(revision_refs, lab_values['revs']):
            revision.clear()
            revision.update(values)
        revisions[:] = revision_refs
        lab_values['revs'] = revisions
        lab.clear()
        lab.update(lab_values)
        before['_lab'] = lab
        for key in fields:
            if key in before: setattr(r, key, before[key])
            elif hasattr(r, key): delattr(r, key)
        return False

def _invalidate(r):
    r._lab_epoch += 1
    r._lab_draft_id += 1
    r._lab_edit_id = -1
    r._composer_edit_id = -1
    r._lab['ver'] += 1


def _install(r, active):
    r._workspace_id = active['id']
    r._lab['revs'] = copy.deepcopy(active['revisions'])
    r._lab['cur'] = active['cur']
    r._composer = active['composer']
    r._pending = copy.deepcopy(active['pending'])
    if r._pending: r._pending['stale'] = True
    _invalidate(r)


def restore_workspace(r):
    ensure(r)
    with r._lab_lock, r._data_lock:
        try:
            state = r._workspace_store.load()
            _install(r, state['active'])
            r._archived = state['archived']
            r._retained = state['retained']
            r._retained_ver = max([x['id'] for x in r._retained if type(x['id']) is int] + [0]) + 1
            r._lab['fav'] = None
            if r._workspace_store.load_status in ('corrupt', 'unsupported', 'read-error'):
                r._save_error = 'Some draft recovery data could not be restored. The original file is preserved.'
        except Exception:
            r._save_error = 'Draft recovery could not be read. The original file is preserved.'


def archive_workspace(r):
    ensure(r)
    with r._lab_lock:
        snapshot = _snapshot(r)
        if any(x['text'].strip() for x in snapshot['revisions']) or snapshot['composer'] or snapshot['pending']:
            r._archived.append(snapshot)
        r._workspace_id = str(uuid.uuid4())
        r._composer = ''
        r._pending = None
        r._composer_edit_id = -1


def _text(r):
    current = r._lab_cur()
    return current['text'] if current else ''


def _stale(r, candidate):
    return (candidate.get('stale', False) or candidate.get('source_epoch') != r._lab_epoch
        or candidate.get('source_workspace') != r._workspace_id or candidate.get('source_text') != _text(r))


def _candidate_version(pending, version):
    versions = pending.get('versions') or [pending]
    if version is None:
        return versions[-1]
    return versions[version] if type(version) is int and 0 <= version < len(versions) else None


def set_candidate(r, text, kind, by, cost, epoch, source_text):
    ensure(r)
    with r._lab_lock:
        if r._pending is not None or epoch != r._lab_epoch or source_text != _text(r): return None
        r._pending = dict(id=str(uuid.uuid4()), text=text, kind=kind, by=by or '', cost=cost or 0,
            source_text=source_text, source_workspace=r._workspace_id, source_epoch=epoch, stale=False,
            versions=[dict(text=text, by=by or '', cost=cost or 0, score=None, based_on=None)])
        r._lab['ver'] += 1
        persist_workspace(r)
        return copy.deepcopy(r._pending)


def record_cost(r, model, operation, cost, call_id=None):
    ensure(r)
    with r._data_lock:
        event = dict(provider='anthropic', model=model, operation=operation, cost=cost)
        if call_id is not None: event['id'] = call_id
        try:
            identity = r._cost_ledger.append(event)
            if identity not in r._session_call_ids:
                r._session_call_ids.add(identity)
                if cost is not None: r._session_cost += cost
        except Exception:
            r._cost_error = 'An operation cost could not be saved. The estimate may be incomplete.'


class ExperienceApi:
    def _workspace_details(self):
        r = self._runtime(); ensure(r)
        with r._lab_lock:
            pending = copy.deepcopy(r._pending)
            if pending: pending['stale'] = bool(_stale(r, pending))
            return dict(workspace_id=r._workspace_id, composer=r._composer, pending=pending,
                composer_edit_id=r._composer_edit_id,
                saved=not bool(r._save_error), save_error=r._save_error)

    def set_composer(self, text, draft_id=None, edit_id=None):
        r = self._runtime(); ensure(r)
        with r._lab_lock:
            if type(text) is not str or (draft_id is not None and (type(draft_id) is not int or draft_id != r._lab_draft_id)): return False
            if edit_id is not None:
                if type(edit_id) is not int or edit_id < r._composer_edit_id: return False
                if edit_id == r._composer_edit_id:
                    return text == r._composer and persist_workspace(r)
                r._composer_edit_id = edit_id
            r._composer = text
            return persist_workspace(r)

    def review_candidate(self, identity, version):
        r = self._runtime(); ensure(r)
        with r._lab_lock:
            pending = r._pending
            if not pending or identity != pending['id'] or _stale(r, pending) or type(version) is not int:
                return False
            selected = _candidate_version(pending, version)
            if selected is None or not selected['text'].strip():
                return False
            text = selected['text']

            def review():
                with r._lab_lock:
                    if not r._pending or identity != r._pending['id'] or _stale(r, r._pending):
                        return
                score = validate_score(r.run_judge(text))
                with r._lab_lock:
                    current = r._pending
                    if not current or identity != current['id'] or _stale(r, current):
                        return

                    def update():
                        versions = current['versions']
                        versions[version]['score'] = score
                        improved = score['improved'].strip()
                        if improved and improved != text.strip():
                            versions.append(dict(text=improved, by=score['by'], cost=0.0,
                                                 score=None, based_on=version, basis_score=score))
                            current.update(text=improved, by=score['by'], cost=0.0)

                    if not commit_workspace_transition(r, update):
                        r._lab.update(error_op='reviewing',
                                      error='Could not save this review. Your draft and suggestion remain open. Try again.')

            return r.lab_run('reviewing', review)

    def apply_candidate(self, identity, version=None):
        r = self._runtime(); ensure(r)
        with r._lab_lock:
            pending = r._pending
            if not pending or identity != pending.get('id') or _stale(r, pending) or r._lab.get('busy') == 'reviewing':
                return False
            selected = _candidate_version(pending, version)
            if selected is None:
                return False

            def apply():
                r._lab_epoch += 1
                revision = r._lab_rev(selected['text'], pending['kind'], selected.get('by'), selected.get('cost', 0))
                revision['score'] = selected.get('score')
                r._lab_add(revision)
                r._pending = None
                if (r._lab.get('error_op') == 'reviewing'
                        or r._lab.get('error') == 'Review or discard the current suggestion before writing another.'):
                    r._lab.update(error='', error_op='')
            return commit_workspace_transition(r, apply)

    def discard_candidate(self, identity):
        r = self._runtime(); ensure(r)
        with r._lab_lock:
            if not r._pending or r._pending.get('id') != identity: return False
            def discard():
                r._pending = None
                if (r._lab.get('error_op') == 'reviewing'
                        or r._lab.get('error') == 'Review or discard the current suggestion before writing another.'):
                    r._lab.update(error='', error_op='')
                r._lab['ver'] += 1
            return commit_workspace_transition(r, discard)

    def list_drafts(self):
        r = self._runtime(); ensure(r)
        with r._lab_lock:
            return [dict(id=x['id'], title=(next((v['text'] for v in x['revisions'] if v['text'].strip()), x['composer']) or 'Recovered draft')[:80]) for x in r._archived]

    def restore_draft(self, identity):
        r = self._runtime(); ensure(r)
        with r._lab_lock:
            hit = next((x for x in r._archived if x['id'] == identity), None)
            if hit is None: return False
            def restore():
                r._archived.remove(hit)
                archive_workspace(r)
                _install(r, hit)
            return commit_workspace_transition(r, restore)

    def delete_draft(self, identity, confirmation=None):
        if confirmation != 'DELETE': return False
        r = self._runtime(); ensure(r)
        with r._lab_lock:
            remaining = [x for x in r._archived if x['id'] != identity]
            if len(remaining) == len(r._archived): return False
            return commit_workspace_transition(r, lambda: setattr(r, '_archived', remaining))

    def clear_private_data(self, scope, confirmation=None):
        if confirmation != 'DELETE': return dict(ok=False, message='Canceled. Nothing was changed.')
        r = self._runtime(); ensure(r)
        try:
            with r._lab_lock, r._data_lock:
                if scope == 'history': ok = self.clear_history()
                elif scope == 'learned': ok = self.reset_learned()
                elif scope == 'logs':
                    with open(r.LOG, 'w', encoding='utf-8'): pass
                    ok = True
                elif scope == 'workspace':
                    def clear():
                        _invalidate(r)
                        r._recovery_generation = getattr(r, '_recovery_generation', 0) + 1
                        r._lab.update(revs=[], cur=-1, fav='', error='')
                        r._lab.pop('prepared_pass', None)
                        r._lab.pop('draft_pass', None)
                        r._workspace_id = str(uuid.uuid4())
                        r._composer = ''; r._pending = None; r._archived = []; r._retained = []
                        r._retained_ver = getattr(r, '_retained_ver', 0) + 1
                    ok = commit_workspace_transition(r, clear)
                else: return dict(ok=False, message='Unknown data category.')
            return dict(ok=bool(ok), message=('Selected local data cleared. Recovery backups may still contain text; this is not secure erasure.' if ok else 'The change could not be saved. Try again.'))
        except Exception:
            return dict(ok=False, message='The selected local data could not be cleared. Try again.')

    def save_favorite_checked(self, name, text, replace=False):
        r = self._runtime(); ensure(r)
        if type(name) is not str or not name.strip() or type(text) is not str or type(replace) is not bool: return dict(status='error')
        with r._lab_lock, r._data_lock:
            try:
                rows = r.load_favorites()
                hit = next((x for x in rows if x['name'] == name), None)
                if hit and hit['text'] != text:
                    if not replace: return dict(status='conflict')
                    updated = copy.deepcopy(rows)
                    next(x for x in updated if x['name'] == name)['text'] = text
                    # Preserve the replaced authored version in the visible recovery list.
                    recovery = dict(id=str(uuid.uuid4()), revisions=[dict(
                        text=hit['text'], kind='favorite', by='', cost=0, score=None, stale=True)],
                        cur=0, composer='', pending=None)
                    if not commit_workspace_transition(r, lambda: r._archived.append(recovery)):
                        return dict(status='error')
                    r.save_favorites(updated)
                    r._lab['fav'] = name
                    r._lab['ver'] += 1
                    return dict(status='saved')
                return dict(status='saved' if self.save_favorite(name, text) else 'error')
            except Exception: return dict(status='error')

    def remove_favorite(self, name):
        r = self._runtime(); ensure(r)
        with r._lab_lock, r._data_lock:
            try:
                hit = next((x for x in r.load_favorites() if x['name'] == name), None)
                if not hit: return None
                token = str(uuid.uuid4())
                r._favorite_undo[token] = copy.deepcopy(hit)
                if not self.delete_favorite(name):
                    r._favorite_undo.pop(token, None)
                    return None
                return token
            except Exception: return None

    def undo_favorite_removal(self, token):
        r = self._runtime(); ensure(r)
        with r._lab_lock, r._data_lock:
            hit = r._favorite_undo.get(token)
            if not hit: return False
            try:
                rows = r.load_favorites()
                if any(x['name'] == hit['name'] for x in rows): return False
                r.save_favorites(rows + [copy.deepcopy(hit)])
                del r._favorite_undo[token]
                return True
            except Exception: return False

    def finish_recording(self):
        r = self._runtime()
        if r._state != 'recording': return False
        r._stop()
        return True

    def cancel_recording(self):
        r = self._runtime()
        if r._state != 'recording': return False
        r._cancel()
        return True
