"""Presentation-only cleanup allowances, using the existing content guard.

This deliberately rejects unsupported structures instead of guessing what a
speaker meant. It has no model, device, or persistence dependencies.
"""
import re

from dictation_quality import cleanup_is_safe, strip_spoken_fillers, technical_literals


FORMAT_INSTRUCTIONS = (
    " Use natural, pasteable text structure only when the speech warrants it: "
    "plain paragraphs, bullets, consecutive numbered or lettered lists, or a "
    "simple Markdown table. Preserve all content in its original order, including "
    "names, quantities, negations, commands, paths and technical literals. List "
    "markers are presentation, never a replacement for a spoken quantity. Use "
    "table headers already spoken, or only neutral Item, Detail, Details, Value, "
    "Category or Measure headers. Do not invent facts, labels or relationships, "
    "summarize away words, execute code, or create images. Prefer ordinary text "
    "when a structure would require guessing. For reliable preservation, keep "
    "the original words in their original order; only remove um, uh, erm or hmm "
    "and immediately repeated words. Do not paraphrase, combine repeated "
    "sentences, expand contractions, or substitute synonyms. Improve readability "
    "through punctuation, capitalization and layout. When the speaker explicitly "
    "requests a shopping list or another list and names its items, output one "
    "item per line with list markers, not a comma-separated paragraph. Keep "
    "framing words and conjunctions rather than deleting them to shorten items."
)

_NEUTRAL_HEADERS = frozenset(('item', 'detail', 'details', 'value', 'category', 'measure'))
_ORDERED = re.compile(r'^(\s*)(\d+|[A-Za-z])([.)])\s+(\S.*)$')
_BULLET = re.compile(r'^\s*[-*+]\s+(\S.*)$')
_ARROWS = re.compile(r'<[-=]+>|[-=]+>|<[-=]+|[→←↔↑↓]')
_OPERATORS = re.compile(r':=|[+\-*/=^%<>|&!~\\()\[\]{}$£€¥−×÷≤≥≠∧∨¬]+')


def _operator_context(text):
    """Retain operators and their word boundaries, including unary signs.

    Word-only comparison cannot distinguish x+y from x-y, or x+y z from
    x y+z. Ignore only the same fillers/consecutive repeats as the base guard.
    """
    def words_in(part):
        words = re.findall(r"\w+(?:['’]\w+)?", part.casefold().replace('’', "'"))
        kept = []
        for word in words:
            if word not in {'um', 'uh', 'erm', 'hmm'} and (not kept or kept[-1] != word):
                kept.append(word)
        return tuple(kept)

    pieces = []
    position = 0
    for operator in _OPERATORS.finditer(text):
        pieces.append((words_in(text[position:operator.start()]), operator.group()))
        position = operator.end()
    pieces.append((words_in(text[position:]), ''))
    return pieces


def _cells(line):
    """Split only unescaped pipes outside inline code; retain cell text exactly."""
    cells, current, ticks = [], [], 0
    index = 0
    while index < len(line):
        char = line[index]
        if char == '\\' and index + 1 < len(line):
            current.extend(line[index:index + 2])
            index += 2
            continue
        if char == '`':
            end = index + 1
            while end < len(line) and line[end] == '`':
                end += 1
            run = end - index
            if not ticks:
                ticks = run
            elif ticks == run:
                ticks = 0
            current.append(line[index:end])
            index = end
            continue
        if char == '|' and not ticks:
            cells.append(''.join(current).strip())
            current = []
        else:
            current.append(char)
        index += 1
    if ticks:
        return None
    cells.append(''.join(current).strip())
    if line.lstrip().startswith('|'):
        cells = cells[1:]
    if line.rstrip().endswith('|') and cells and not cells[-1]:
        cells = cells[:-1]
    return cells if len(cells) >= 2 else None


def _flatten_tables(raw, lines):
    raw_words = set(re.findall(r'\w+', raw.casefold()))
    result = []
    index = 0
    while index < len(lines):
        header = _cells(lines[index])
        divider = _cells(lines[index + 1]) if index + 1 < len(lines) else None
        if not (header and divider and len(header) == len(divider)
                and all(re.fullmatch(r':?-{3,}:?', cell) for cell in divider)):
            result.append(lines[index])
            index += 1
            continue
        # Remove only entire neutral header cells absent from the source. Spoken
        # header words remain content and must pass the ordered semantic guard.
        kept = [cell for cell in header
                if cell.casefold() not in _NEUTRAL_HEADERS or cell.casefold() in raw_words]
        result.append(' '.join(kept))
        index += 2
        row_count = 0
        while index < len(lines):
            row = _cells(lines[index])
            if row is None:
                break
            if len(row) != len(header) or any(not cell for cell in row):
                return None
            result.append(' '.join(row))
            row_count += 1
            index += 1
        if not row_count:
            return None
    return result


def _strip_lists(lines):
    result = list(lines)
    index = 0
    while index < len(lines):
        bullet = _BULLET.match(lines[index])
        if bullet:
            result[index] = bullet.group(1)
            index += 1
            continue
        first = _ORDERED.match(lines[index])
        if not first:
            index += 1
            continue
        marker = first.group(2)
        if marker not in ('1', 'A', 'a'):
            index += 1
            continue
        end = index
        expected = 1 if marker == '1' else ord(marker)
        matches = []
        while end < len(lines):
            if not lines[end].strip():
                end += 1
                continue
            match = _ORDERED.match(lines[end])
            if not match or match.group(1) != first.group(1) or match.group(3) != first.group(3):
                break
            value = match.group(2)
            actual = int(value) if value.isdigit() and marker == '1' else ord(value) if len(value) == 1 and not value.isdigit() and marker != '1' else None
            if actual != expected:
                break
            matches.append((end, match.group(4)))
            expected += 1
            end += 1
        if len(matches) >= 2:
            for position, content in matches:
                result[position] = content
            index = end
        else:
            index += 1
    return result


def semantic_content(raw, formatted):
    """Return a presentation-stripped candidate, or None for invalid structure."""
    if not isinstance(raw, str) or not isinstance(formatted, str):
        return None
    # New arrows can assert an unspoken relationship; the word guard cannot see
    # that. Diagrams that need inferred arrows are intentionally unsupported.
    if _ARROWS.findall(raw) != _ARROWS.findall(formatted):
        return None
    lines = _flatten_tables(raw, formatted.splitlines())
    if lines is None:
        return None
    lines = _strip_lists(lines)
    # Border-only rows and paired vertical borders contain no semantic text.
    lines = [line for line in lines
             if not re.fullmatch(r'\s*\+(?:[-=]+\+)+\s*', line)]
    lines = [line.strip()[1:-1].strip() if re.fullmatch(r'\s*\|[^|]+\|\s*', line) else line
             for line in lines]
    return '\n'.join(lines)


def formatting_is_safe(raw, formatted, vocab=()):
    """Allow presentation markers only when the existing guard still accepts."""
    content = semantic_content(raw, formatted)
    if content is None:
        return False
    # Check the original output too: removing structure must not conceal a
    # changed command flag, path, filename or other protected technical token.
    if technical_literals(raw) != technical_literals(formatted):
        return False
    context = _operator_context(content)
    if _operator_context(raw) != context and _operator_context(strip_spoken_fillers(raw)) != context:
        return False
    # Arrows must retain their original endpoints, not just their count.
    if _ARROWS.search(raw):
        words = lambda part: re.findall(r"\w+(?:['’]\w+)?", part.casefold())
        if [words(part) for part in _ARROWS.split(raw)] != [words(part) for part in _ARROWS.split(content)]:
            return False
    # Inline code can carry meaning in punctuation (a+b versus a-b). Preserve
    # source spans verbatim; newly wrapped spans must already occur verbatim.
    spans = lambda text: re.findall(r'(?<!`)(`+)(.+?)\1(?!`)', text)
    before_code, after_code = spans(raw), spans(formatted)
    if any(span not in after_code for span in before_code):
        return False
    if any(value not in raw for _, value in after_code):
        return False
    return cleanup_is_safe(raw, content, vocab)


from dataclasses import dataclass, replace
from spoken_lists import ListPlan, parse_spoken_list


@dataclass(frozen=True)
class PreparedList:
    source: str
    formatted: str
    plan: ListPlan


def prepare_spoken_list(text, vocab=(), *, allow_corrections=True):
    """Validate one interpretation of original speech for source and layout."""
    plan = parse_spoken_list(text, allow_corrections=allow_corrections)
    if plan is None:
        return None
    source, formatted = plan.normalized_source(), plan.render()
    # The original marker proves the singleton number; the generic guard only
    # treats consecutive numbers as formatting. Check the same content as a bullet.
    checked = replace(plan, markers=('-',)).render() if plan.markers == ('1.',) else formatted
    if not formatting_is_safe(source, checked, vocab):
        return None
    return PreparedList(source, formatted, plan)


def format_spoken_lists(text, vocab=()):
    prepared = prepare_spoken_list(text, vocab)
    return prepared.formatted if prepared is not None else text


def layout_explicit_lists(text, vocab=()):
    prepared = prepare_spoken_list(text, vocab, allow_corrections=False)
    return prepared.formatted if prepared is not None else text
