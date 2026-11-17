"""Bounded spoken-list recognition over immutable original-source ranges."""
from dataclasses import dataclass
import re

from dictation_quality import technical_literals


@dataclass(frozen=True)
class Span:
    role: str
    start: int
    end: int
    keep: bool = True


@dataclass(frozen=True)
class ListPlan:
    original: str
    partition: tuple
    items: tuple
    markers: tuple
    headings: tuple
    suffix: tuple

    def normalized_source(self):
        return ''.join(self.original[s.start:s.end] for s in self.partition if s.keep)

    def render(self):
        take = lambda group: ''.join(self.original[s.start:s.end] for s in group)
        result = '\n'.join(take(group).rstrip() for group in self.headings)
        result += '\n' + '\n'.join(marker + ' ' + take(group)
                                   for marker, group in zip(self.markers, self.items))
        if self.suffix:
            result += '\n\n' + take(self.suffix)
        return result


_COUNT_WORDS = dict(zip(('one', 'two', 'three', 'four', 'five', 'six', 'seven',
    'eight', 'nine', 'ten', 'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen',
    'sixteen', 'seventeen', 'eighteen', 'nineteen', 'twenty'), range(1, 21)))
_ORDINAL_WORDS = ('first', 'second', 'third', 'fourth', 'fifth', 'sixth', 'seventh',
    'eighth', 'ninth', 'tenth', 'eleventh', 'twelfth', 'thirteenth', 'fourteenth',
    'fifteenth', 'sixteenth', 'seventeenth', 'eighteenth', 'nineteenth', 'twentieth')
_OPERATORS = re.compile(r':=|[+\-*/=^%<>|&!~\\()\[\]{}$£€¥−×÷≤≥≠∧∨¬]+')
_REQUEST = re.compile(
    r'\b(?:I\s+(?:need|want|would\s+like)\s+(?:to\s+)?'
    r'(?:(?:make|create|write)\s+)?(?:a\s+|my\s+|the\s+)?'
    r'|(?:please\s+)?(?:make|create|write)\s+(?:me\s+)?(?:a\s+|my\s+|the\s+)?)'
    r'(?P<kind>(?:shopping|grocery|number(?:ed)?|letter(?:ed)?|bullet(?:ed)?)\s+list\b'
    r'|list\s+for\s+(?:the\s+)?groceries\b)', re.I)
_SERIES = re.compile(r'^\s*(?:I|we)\s+need\s+to\s+(?:buy|pick\s+up)\s+', re.I)
_ITEM_LABEL = re.compile(
    r'(?<!\w)(?:(?P<ordinal>' + '|'.join(_ORDINAL_WORDS) + r')\s+item'
    r'|(?:item\s+)?number\s+(?P<number>[0-9]{1,2}|' + '|'.join(_COUNT_WORDS) + r'))'
    r'(?:\s+is\b\s*[:,]?|\s*[:,])\s*', re.I)
_REFERENCED_LABEL = re.compile(
    r'\b(?:word|term|phrase|label|called|named|says?|reads?|marked|printed|means?|mentions?|mentioned)\s*[:,]?\s*$', re.I)
_INTRO = re.compile(r"(?:it['’]s\s+going\s+to\s+be|it\s+is\s+going\s+to\s+be|"
                    r'the\s+items\s+are|they\s+are|here\s+are\s+the\s+items)\b', re.I)
_COUNTED_INTRO = re.compile(
    r'\b(?:(?:I|we)\s+have|(?:there|here)\s+are|(?:my|our|the)\s+(?:plan|list)\s+(?:has|includes)'
    r'|(?:make|create|write)\s+(?:me\s+)?a\s+list\s+(?:with|of))\s+'
    r'(?P<count>[0-9]{1,2}|' + '|'.join(_COUNT_WORDS) + r')\s+'
    r'(?P<unit>tasks|steps|items|points|options)\s*[.:]\s*', re.I)
_COUNTED_ORDINAL = re.compile(r'\b(?P<ordinal>' + '|'.join(_ORDINAL_WORDS) + r')(?P<comma>,?)\s+', re.I)
_TASK_VERBS = frozenset(('add ask attach back bring buy call check choose clean click close compare '
    'complete cook copy create delete do don\'t download email ensure finish fix get install keep make '
    'move open pay prepare put read remove rename reply review run save send set share sign start '
    'stop submit take test turn type update upload use verify wait wash write').split())
_UNSAFE_PREFIX = re.compile(
    r"\b(?:not|never|no|without|don't|doesn't|cannot|can't|explain|example|quoted|quote|"
    r"said|says?|reads?|mentioned|mentions?|wrote|written|whether|how|if|why|word|phrase|label|called|named)\b", re.I)


def _unsafe_list_prefix(text):
    # Normalize only for refusal matching; every plan range uses untouched speech.
    return bool(_UNSAFE_PREFIX.search(text.replace('’', "'").replace('‘', "'")))


def _counted_ordinal_list(text):
    introductions = list(_COUNTED_INTRO.finditer(text))
    if len(introductions) != 1 or technical_literals(text) or _OPERATORS.search(text):
        return None
    introduction = introductions[0]
    start = introduction.end()
    if _unsafe_list_prefix(text[:start]):
        return None
    if re.search(r'\bscratch\s+that\b', text, re.I):
        return None
    token = introduction.group('count').lower()
    count = int(token) if token.isdigit() else _COUNT_WORDS[token]
    if not 2 <= count <= 20:
        return None
    labels = list(_COUNTED_ORDINAL.finditer(text, start))
    if len(labels) != count or labels[0].start() != start:
        return None
    for label in labels:
        if label.start() != start and not re.search(r'(?:[.!?]\s*|\n[ \t]*)$', text[start:label.start()]):
            return None
        if not label.group('comma'):
            first = re.match(r"[A-Za-z]+(?:['’][A-Za-z]+)?", text[label.end():])
            # Without a pause/comma, require task wording rather than First Aid,
            # Second Street or another literal ordinal compound.
            if (introduction.group('unit').lower() not in ('tasks', 'steps') or first is None
                    or not first.group()[0].islower() or first.group().lower().replace('’', "'") not in _TASK_VERBS):
                return None
    return _labeled_list(text, start, count, bulleted=False, labels=labels)


def _list_sentence(text):
    end = re.search(r'[.!?](?=\s|$)', text)
    if end is None:
        return text.strip(), '', ''
    return text[:end.start()].strip(), text[end.start():end.end()], text[end.end():]


def _comma_items(body, count):
    items = [part.strip() for part in body.split(',')]
    if len(items) < 2 or len(items) > 20 or any(not re.search(r'\w', item) for item in items):
        return None
    if any(re.fullmatch(r'(?:and|or)\W*', item, re.I) for item in items):
        return None
    if count is not None:
        if count == len(items) + 1:
            conjunctions = list(re.finditer(r'\s+(?:and|or)\s+', items[-1], re.I))
            if len(conjunctions) != 1:
                return None
            split = conjunctions[0]
            items[-1:] = [items[-1][:split.start()].strip(), items[-1][split.start():].strip()]
            if any(not re.search(r'\w', item) or re.fullmatch(r'(?:and|or)\W*', item, re.I)
                   for item in items[-2:]):
                return None
        if count != len(items) or not 2 <= count <= 20:
            return None
    return items


def _ordinal_body(text):
    segment_start = ordinal_count = 0
    for end in re.finditer(r'[.!?](?=\s|$)', text):
        ordinal_count += len(re.findall(r'(?<!\w)(?:' + '|'.join(_ORDINAL_WORDS) +
                                        r')\s*,\s*', text[segment_start:end.start()], re.I))
        suffix = text[end.end():]
        if (ordinal_count < len(_ORDINAL_WORDS) and re.match(
                r'\s*' + _ORDINAL_WORDS[ordinal_count] + r'\s*,\s*', suffix, re.I)):
            segment_start = end.end()
            continue
        return text[:end.start()].strip(), text[end.start():end.end()], suffix
    return text.strip(), '', ''


def _ordinal_items(body, count):
    markers = list(re.finditer(r'(?<!\w)(' + '|'.join(_ORDINAL_WORDS) + r')\s*,\s*', body, re.I))
    if not 2 <= len(markers) <= 20 or markers[0].start() != 0:
        return None
    items = []
    for index, marker in enumerate(markers):
        if marker.group(1).lower() != _ORDINAL_WORDS[index]:
            return None
        if index and _REFERENCED_LABEL.search(body[:marker.start()]):
            return None
        end = markers[index + 1].start() if index + 1 < len(markers) else len(body)
        item = body[marker.start():end].strip().rstrip(',').rstrip()
        if not re.search(r'\w', body[marker.end():end]):
            return None
        items.append(item)
    if count is not None and count != len(items):
        return None
    return items


def _labeled_list(text, start, count, *, bulleted, labels=None):
    labels = list(_ITEM_LABEL.finditer(text, start)) if labels is None else labels
    if not labels or labels[0].start() != start or len(labels) > 20:
        return None
    if count is not None and len(labels) != count:
        return None
    groups, removed, suffix = [], [], ()
    for index, label in enumerate(labels):
        ordinal, number = label.group('ordinal'), label.groupdict().get('number')
        position = (_ORDINAL_WORDS.index(ordinal.lower()) + 1 if ordinal else
                    int(number) if number.isdigit() else _COUNT_WORDS[number.lower()])
        if position != index + 1:
            return None
        if index:
            before = text[labels[index - 1].end():label.start()]
            if not re.search(r'(?:[.!?,;]\s*|\n[ \t]*)$', before) or _REFERENCED_LABEL.search(before):
                return None
        end = labels[index + 1].start() if index + 1 < len(labels) else len(text)
        piece = text[label.end():end]
        item, stop, remaining = _list_sentence(piece)
        item = item.rstrip(',;').rstrip()
        if not re.search(r'\w', item) or technical_literals(item) or _OPERATORS.search(item):
            return None
        if remaining.strip():
            if index + 1 < len(labels):
                return None
            suffix = (Span('suffix', end - len(remaining), end),)
        offset = label.end() + piece.index(item)
        item_end = end - len(remaining) if stop in ('?', '!') else offset + len(item)
        groups.append((Span('item', offset, item_end),))
        removed.append((label.start(), label.end(), 'spoken marker'))
    markers = tuple('-' if bulleted else f'{i + 1}.' for i in range(len(groups)))
    return _make_plan(text, tuple(groups), markers, ((Span('framing', 0, start),),), suffix, removed)


def _correction(text, body_start, body_end, command):
    """Return explicit original ranges to remove, without reparsing a candidate."""
    if not body_start < command.start() < command.end() < body_end:
        return None
    before = text[:command.start()].rstrip()
    if before.endswith(','):
        boundary = len(before) - 1
    elif re.match(r'actually\b', command.group(), re.I):
        boundary = len(before)
    else:
        return None
    previous = text.rfind(',', body_start, boundary)
    if previous < body_start:
        return None
    cancelled_start = previous + 1
    while cancelled_start < boundary and text[cancelled_start].isspace():
        cancelled_start += 1
    cancelled = text[cancelled_start:boundary].strip()
    if (not cancelled or len(cancelled) > 200 or any(c in cancelled for c in '.!?;\n') or
            re.search(r'\b(?:I|we|you|he|she|they|later|then|saw|say|says|saying|called|named|phrase|words|quote|scratch|delete)\b', cancelled, re.I)):
        return None
    replacement_start = command.end()
    separator = re.match(r'\s*,?\s*', text[replacement_start:body_end])
    replacement_start += separator.end()
    replacement = text[replacement_start:body_end]
    if not re.match(r'\w', replacement) or re.match(
            r'(?:is|are|was|were|means?|meant|refers?|says?|said|saying|reads?|'
            r'indicates?|describes?|the\s+(?:phrase|words?|command))\b', replacement, re.I):
        return None
    return ((cancelled_start, boundary, 'cancelled'),
            (boundary, command.start(), 'separator'),
            (command.start(), command.end(), 'command'),
            (command.end(), replacement_start, 'separator'))


def parse_spoken_list(text, *, allow_corrections=False):
    """Recognize one list; all returned ranges refer to this exact input."""
    if not isinstance(text, str) or not text or len(text) > 2_000_000:
        return None
    if any(c in text for c in ('"', '“', '”', '`')) or re.search(
            r"(?<!\w)['‘’]|['‘’](?!\w)", text):
        return None
    if re.search(r'(?m)^\s*(?:[-*+]\s+|(?:\d+|[A-Za-z])[.)]\s+)', text):
        return None
    if allow_corrections:
        counted = _counted_ordinal_list(text)
        if counted is not None:
            return counted
    requests = list(_REQUEST.finditer(text))
    implicit = not requests and _SERIES.match(text)
    if len(requests) != 1 and not implicit:
        return None
    request = implicit or requests[0]
    kind = '' if implicit else request.group('kind').lower()
    numbered = kind.startswith('number')
    lettered = kind.startswith('letter')
    bulleted = kind.startswith('bullet')
    counts = r'(\d+|' + '|'.join(_COUNT_WORDS) + r')'
    unit = r'(?:items|options)' if numbered or lettered else 'items'
    tail = text[request.end():]
    declared = re.match(r'\s+(?:with|of)\s+' + counts + r'\s+' + unit + r'\b', tail, re.I)
    count = None
    if declared:
        token = declared.group(1).lower()
        if token.isdigit() and len(token) > 2:
            return None
        count = int(token) if token.isdigit() else _COUNT_WORDS[token]
        if not 2 <= count <= 20:
            return None
        header_end = request.end() + declared.end()
        if not numbered and not lettered:
            qualifier = re.match(r'\s+or\s+so\b', text[header_end:], re.I)
            if qualifier:
                header_end += qualifier.end()
        separator = re.match(r'\s*[,.:!?]?\s*', text[header_end:])
    elif numbered or bulleted or implicit:
        header_end = request.end()
        separator = re.match(r'\s*[.:!?]?\s*', tail)
    else:
        if lettered:
            return None
        header_end = request.end()
        separator = re.match(r'\s*[.:!?]\s*', tail)
        if separator is None:
            separator = re.match(r'\s+', tail) if not implicit else None
            if separator is None or not _ITEM_LABEL.match(text, header_end + separator.end()):
                return None
    body_start = header_end + separator.end()
    headings = [(0, body_start)]
    intro = _INTRO.match(text, body_start) if count is not None and not numbered and not lettered else None
    if (count is not None and not numbered and not lettered and intro is None and
            re.match(r"(?:it['’]s|it\s+is|the\s+items|they|here)\b", text[body_start:], re.I)):
        return None
    if intro:
        headings = [(0, body_start), (body_start, intro.end())]
        body_start = intro.end() + re.match(r'\s*[:,.]?\s*', text[intro.end():]).end()
        headings[-1] = (headings[-1][0], body_start)
        if _INTRO.search(text, body_start):
            return None
    if re.match(r'(?:or|and|between|about|approximately|roughly|at\s+least|at\s+most|maybe)\b', text[body_start:], re.I):
        return None
    if _unsafe_list_prefix(text[:body_start]):
        return None
    commands = list(re.finditer(r'\b(?:actually\s+)?scratch\s+that\b', text, re.I))
    if not implicit and not lettered and _ITEM_LABEL.match(text, body_start):
        return _labeled_list(text, body_start, count, bulleted=bulleted) if allow_corrections and not commands else None
    removed = ()
    if commands:
        if not allow_corrections or len(commands) != 1 or count is None or numbered or lettered or technical_literals(text):
            return None
        end = re.search(r'[.!?](?=\s|$)', text[body_start:])
        body_end = body_start + end.start() if end else len(text)
        removed = _correction(text, body_start, body_end, commands[0])
        if removed is None:
            return None
    # One retained view, with explicit mapping back to the original source.
    # It is scanned once for item boundaries; normalized_source is never parsed.
    excluded = [(a, b) for a, b, _ in removed if a < b]
    mapping = []
    cursor = body_start
    for a, b in excluded:
        mapping.extend(range(cursor, a))
        cursor = b
    mapping.extend(range(cursor, len(text)))
    remainder = ''.join(text[i] for i in mapping)
    body, terminator, suffix = _ordinal_body(remainder) if numbered else _list_sentence(remainder)
    followup = None
    if count is not None and not numbered and not lettered:
        cues = list(re.finditer(r'\bOh\s*,\s*and\s+', remainder, re.I))
        if len(cues) > 1:
            return None
        if cues:
            cue = cues[0]
            initial, initial_end, between = _list_sentence(remainder[:cue.start()])
            if between.strip():
                return None
            followup, followup_end, suffix = _list_sentence(remainder[cue.start():])
            content, _, _ = _list_sentence(remainder[cue.end():])
            if not content or ',' in content:
                return None
            body, terminator = initial, initial_end
    if not body or technical_literals(body) or _OPERATORS.search(body) or (followup and (technical_literals(followup) or _OPERATORS.search(followup))):
        return None
    if count is None and not (numbered or bulleted or implicit or terminator):
        return None
    items = _ordinal_items(body, count) if numbered else _comma_items(body, count)
    if items is None:
        return None
    if implicit and (len(items) < 4 or suffix.strip() or not re.match(r'and\s+\w', items[-1], re.I)
                     or any(len(item.split()) > 7 or re.search(
                         r'\b(?:but|because|if|when|whether|or|to|then|will|shall|should|could|would|must'
                         r'|is|are|am|was|were|has|have|had)\b'
                         r'|^(?:and\s+)?(?:I|we|you|he|she|they|it)\s+', item, re.I)
                            for item in items)):
        return None
    endings = [''] * len(items)
    endings[-1] = terminator
    if followup:
        items.append(followup)
        endings.append(followup_end)
    ranges = []
    search_from = 0
    for item, ending in zip(items, endings):
        at = remainder.find(item, search_from)
        if at < 0:
            return None
        end = at + len(item)
        if ending:
            # Whitespace before the original stop is retained too. Never
            # search past intervening content to manufacture an adjacency.
            stop = re.match(r'\s*' + re.escape(ending), remainder[end:])
            if stop is None:
                return None
            end += stop.end()
        ranges.append((at, end))
        search_from = end
    def original_spans(a, b, role):
        indices = mapping[a:b]
        if not indices:
            return ()
        result = []
        begin = previous = indices[0]
        for index in indices[1:]:
            if index != previous + 1:
                result.append(Span(role, begin, previous + 1))
                begin = index
            previous = index
        result.append(Span(role, begin, previous + 1))
        return tuple(result)
    groups = tuple(original_spans(a, b, 'item') for a, b in ranges)
    heading_groups = tuple((Span('framing', a, b),) for a, b in headings if a < b)
    suffix_group = original_spans(len(remainder) - len(suffix), len(remainder), 'suffix') if suffix else ()
    markers = tuple(str(i + 1) + '.' if numbered else chr(65 + i) + '.' if lettered else '-' for i in range(len(groups)))
    return _make_plan(text, groups, markers, heading_groups, suffix_group, removed)


def _make_plan(text, groups, markers, heading_groups, suffix_group, removed):
    labeled = [s for group in heading_groups + groups + (suffix_group,) for s in group]
    labeled += [Span(role, a, b, False) for a, b, role in removed if a < b]
    labeled.sort(key=lambda s: s.start)
    partition = []
    cursor = 0
    for span in labeled:
        if not 0 <= cursor <= span.start < span.end <= len(text):
            return None
        if cursor < span.start:
            partition.append(Span('separator', cursor, span.start))
        partition.append(span)
        cursor = span.end
    if cursor < len(text):
        partition.append(Span('separator', cursor, len(text)))
    plan = ListPlan(text, tuple(partition), groups, markers, heading_groups, suffix_group)
    lexical = lambda value: re.findall(r"\w+(?:['’]\w+)?", value.casefold())
    # Marker words are excluded by rendering each retained group directly.
    retained = ''.join(text[s.start:s.end] for s in plan.partition if s.keep)
    rendered_content = '\n'.join(''.join(text[s.start:s.end] for s in group)
                                 for group in heading_groups + groups + (suffix_group,))
    if lexical(retained) != lexical(rendered_content):
        return None
    return plan
