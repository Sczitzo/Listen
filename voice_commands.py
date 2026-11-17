"""Explicit in-recording commands and conservative preservation checks, no model calls."""
import collections
import re
from dictation_quality import technical_literals

_PREFIX = re.compile(r'\A\s*hey[\s,]+prompt[\s,]+engineer[\s,]+this(?=$|[\s,:.!?])', re.I)
_OPENERS = frozenset('Please Make Write Create Build Help I We The A An Can Could Would Do Review Fix Update Use Plan Summarize Explain Design Draft Generate Keep Ensure Limit Include Avoid Return Output Add Remove Preserve Retain'.split())

def split_voice_command(text):
    match = _PREFIX.match(text)
    if not match:
        return 'dictate', text
    payload = text[match.end():].lstrip()
    if payload[:1] in (',', ':', '.', '!', '?'):
        payload = payload[1:].lstrip()
    return 'engineer', payload

def engineering_preserves_literals(raw, result, vocab=()):
    """Heuristic only: rejects missing literal constraints, not a semantic proof."""
    return not engineering_preservation_issue(raw, result, vocab)


def engineering_preservation_issue(raw, result, vocab=()):
    """Content-free reason for conservative review; never include user text."""
    if not isinstance(result, str) or not result.strip():
        return 'empty_output'
    def ordered_subset(before, after):
        remaining = iter(after)
        return all(any(item == candidate for candidate in remaining) for item in before)
    if not ordered_subset(technical_literals(raw), technical_literals(result)):
        return 'technical_literals'
    options=lambda text:re.findall(r'(?<![\w-])-[A-Za-z](?:[\w-]*)(?:=[^\s,;]+)?',text)
    if not ordered_subset(options(raw),options(result)):
        return 'command_options'
    numbers=lambda text:re.findall(r'\d+(?:[.,:]\d+)*',text)
    if not ordered_subset(numbers(raw),numbers(result)):
        return 'numbers'
    protected=[w for w in re.findall(r'\b[A-Z][\w]*\b',raw) if w not in _OPENERS]
    protected.extend(v for v in vocab if isinstance(v,str) and v and v in raw)
    if any(not re.search(r'(?<!\w)'+re.escape(term)+r'(?!\w)',result) for term in protected):
        return 'names_or_vocabulary'
    negatives=lambda text:collections.Counter(w.lower() for w in re.findall(r"[\w]+(?:['’][\w]+)?",text) if w.lower() in ('no','not','never','without','cannot') or w.lower().endswith(("n't","n’t")))
    return 'negations' if negatives(raw)-negatives(result) else ''
