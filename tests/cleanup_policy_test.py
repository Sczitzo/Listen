"""Cooldown and warm-up ownership must not leak across requests or models."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cleanup_policy import CleanupPolicy

clock = [1000.0]
p = CleanupPolicy(clock=lambda: clock[0])
assert p.available('chosen')
old = p.claim_warm('chosen', interval=60)
assert old is not None
assert p.is_warming('chosen') and not p.is_warming('other')
assert p.claim_warm('chosen', interval=60) is None
assert p.claim_warm('other', interval=60) is None, 'Only one warm request at a time'
p.timed_out('chosen')
assert not p.is_warming('chosen'), 'Old warming hid a newer failure'
assert not p.available('chosen') and p.available('other')
p.finish_warm(old, success=True)
assert not p.available('chosen'), 'Old warm success erased newer failure'
assert p.claim_warm('chosen', interval=60) is None
clock[0] += 30
assert p.available('chosen')
new = p.claim_warm('chosen', interval=60)
assert new is not None
p.finish_warm(old, success=False)
assert p.claim_warm('other', interval=60) is None, 'Stale completion released newer owner'
p.finish_warm(new, success=True)
assert not p.is_warming('chosen')
assert p.claim_warm('chosen', interval=60) is None
clock[0] += 59
assert p.claim_warm('chosen', interval=60) is None
clock[0] += 1
token = p.claim_warm('chosen', interval=60)
assert token is not None
p.finish_warm(token, success=False)
token = p.claim_warm('chosen', interval=60)
assert token is None, 'Failing warm-up retried on every worker tick'
clock[0] += 30
p.succeeded('chosen')
assert p.available('chosen') and p.claim_warm('chosen', interval=240) is None
clock[0] += 239
assert p.claim_warm('chosen', interval=240) is None
clock[0] += 1
assert p.claim_warm('chosen', interval=240) is not None
print('Cleanup policy: coalescing, cooldown, stale ownership, model scope and recording refresh passed')

p = CleanupPolicy()
token = p.claim_warm('chosen', interval=0)
assert p.finish_warm(token, success=True) is True
assert p.finish_warm(token, success=False) is False
token = p.claim_warm('chosen', interval=0)
p.succeeded('chosen')
assert p.finish_warm(token, success=False) is False
assert p.available('chosen'), 'An older warm failure beat a newer real success'
