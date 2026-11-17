"""Conservative local checks, not a semantic correctness guarantee."""
import collections
import math
import re

CRITERIA = frozenset(('clarity', 'context', 'task', 'constraints', 'success_criteria', 'format'))
_TECHNICAL = r"(?<!\w)(?:[A-Za-z]:)?[\\/]*(?:[\w.-]+[\\/]+)+[\w.-]+|(?<![\w-])-{2,}[A-Za-z0-9][\w-]*|\b[A-Za-z_]\w*_\w+\b|\b[A-Za-z_][\w-]*\.[A-Za-z0-9][\w.-]*"

def technical_literals(text):
    return [match.group().rstrip('.,') for match in re.finditer(_TECHNICAL, text)]


def _finite(value, minimum, maximum=float('inf')):
    return type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum


def filter_segments(segments, initial_prompt=None):
    """Drop only strongly unsupported audio or exact low-confidence prompt echoes."""
    out = []
    normalize = lambda text: ' '.join(re.findall(r'\w+', text.lower()))
    prompt = normalize(initial_prompt or '')
    for segment in segments:
        text = str(segment.text).strip()
        logprob = getattr(segment, 'avg_logprob', 0)
        silence = getattr(segment, 'no_speech_prob', 0)
        low = _finite(logprob, -float('inf')) and logprob < -1.0
        if low and _finite(silence, 0, 1) and silence > 0.6:
            continue
        if prompt and normalize(text) == prompt and low:
            continue
        if text:
            out.append(text)
    return ' '.join(out)


def strip_spoken_fillers(raw):
    """Drop only comma-delimited spoken fillers ("it was, like, huge"); the same word without commas is content."""
    return re.sub(r",\s*(?:you know|i mean|like|basically|actually|literally|sort of|kind of)\s*(?=,)",
                  "", raw, flags=re.I)


def cleanup_is_safe(raw, cleaned, vocab=()):
    """Prefer false rejection over losing content. Meaning can still change undetected."""
    if not isinstance(raw, str) or not isinstance(cleaned, str) or not cleaned.strip():
        return False
    tokens = lambda text: re.findall(r"[\w]+(?:['’][\w]+)?", text.lower().replace('’', "'"))
    before, after = tokens(raw), tokens(cleaned)
    if not before or len(after) > len(before) * 1.25 + 2:
        return False
    if re.findall(r'\d+(?:[.,:]\d+)*', raw) != re.findall(r'\d+(?:[.,:]\d+)*', cleaned):
        return False
    # Word tokenization cannot detect a changed path separator or command flag.
    # Preserve technical literals exactly while permitting surrounding punctuation.
    if technical_literals(raw) != technical_literals(cleaned):
        return False
    negations = {'no', 'not', 'never', 'neither', 'nor', 'without', 'cannot'}
    negative = lambda words: collections.Counter(w for w in words if w in negations or w.endswith("n't"))
    if negative(before) != negative(after):
        return False
    # A sentence-opening "Um," is capitalized by speech recognition but is a filler, not a name.
    protected = re.findall(r'\b[A-Z][\w]*\b', re.sub(r'\b(?:um+|uh+|erm|hmm)\b', ' ', raw, flags=re.I))
    if isinstance(vocab, str):
        vocab = re.split(r'[,\n]', vocab)
    protected.extend(v for v in vocab if isinstance(v, str) and v.strip() and tokens(v))
    after_counts = collections.Counter(after)
    for phrase in protected:
        required = collections.Counter(tokens(phrase))
        if all(before.count(w) >= count for w, count in required.items()) and any(after_counts[w] < count for w, count in required.items()):
            return False
    preamble = r"^(?:sure\b|certainly\b|here(?:'s| is| are)\b|as an ai\b|i (?:can|will|cannot|can't) help\b)"
    if re.search(preamble, cleaned.strip(), re.I) and not re.search(preamble, raw.strip(), re.I):
        return False
    fillers = {'um', 'uh', 'erm', 'hmm'}
    # Spoken fillers set off by commas ("it was, like, huge") may be dropped; the same
    # word without commas ("I like it") is content and must survive.
    spoken = strip_spoken_fillers(raw)

    def words_ok(source):
        content = collections.Counter(w for w in source if w not in fillers)
        # Repetition removal is allowed, but every distinct content word must survive.
        if any(w not in after_counts for w in content):
            return False
        # New content is especially unsafe for short commands/questions.
        if any(w not in source for w in after):
            return False
        # Punctuation/case changes may not reorder a short command's actors/actions.
        def ordered(words):
            out = []
            for word in words:
                if word not in fillers and (not out or out[-1] != word):
                    out.append(word)
            return out
        return ordered(source) == ordered(after)
    return words_ok(before) or (spoken != raw and words_ok(tokens(spoken)))


def validate_score(data):
    if not isinstance(data, dict) or not _finite(data.get('score'), 1, 10):
        raise ValueError('Invalid score')
    criteria = data.get('criteria')
    if not isinstance(criteria, dict) or set(criteria) != CRITERIA or any(not _finite(v, 1, 5) for v in criteria.values()):
        raise ValueError('Invalid score criteria')
    if not isinstance(data.get('gaps'), list) or any(not isinstance(v, str) for v in data['gaps']):
        raise ValueError('Invalid score gaps')
    if not isinstance(data.get('improved'), str) or not isinstance(data.get('by'), str) or not _finite(data.get('cost'), 0):
        raise ValueError('Invalid score metadata')
    return dict(data, criteria=dict(criteria), gaps=list(data['gaps']))
