"""Owner-shared phrase patterns; synthetic fixtures, no microphone/provider."""
from pathlib import Path
import re
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from spoken_format import formatting_is_safe, layout_explicit_lists
from spoken_corrections import correction_source


def render(raw):
    source = correction_source(raw)
    result = layout_explicit_lists(source)
    if result != source:
        assert formatting_is_safe(source, result), (source, result)
    assert layout_explicit_lists(result) == result
    return source, result


fixtures = [
    ('shopping_missing_header_stop', 'I need a shopping list with five items Apples, bananas, a bag of chips, milk, and eggs.', '-', 5),
    ('addition_without_stop', 'I need a shopping list with three items Bread, Milk, and Eggs Oh, and some ice cream', '-', 4),
    ('already_bulleted_compounds', 'Make a grocery list with three items.\n- Mac and cheese\n- peanut butter and jelly\n- and orange juice.', '-', 3),
    ('quantities_comma_header', 'I need a shopping list with 4 items, 2 apples, 3 bananas, 1 carton of milk, and 6 eggs', '-', 4),
    ('number_alias', 'Make a number list First, wash the vegetables Second, chop them Third, put them in the refrigerator', 'number', 3),
    ('letter_alias', 'Make a letter list of three options. Take the bus, ride a bicycle, or walk.', 'letter', 3),
    ('explicit_correction', 'I need a shopping list with three items, apples, bananas, scratch that oranges, and milk', '-', 3),
    ('already_bulleted_negation', 'Make a shopping list with three items.\n- Apples\n- lactose free milk\n- and bread without nuts.\n\n Do not add eggs.', '-', 3),
    ('ordinary_asr_words_preserved', "About Apple's bananas and milk yesterday, then walked home.", None, 0),
    ('counted_no_end_stop', 'I need a shopping list with 4 items Apples, Bananas, Milk, and Eggs', '-', 4),
]
tokens = lambda value: re.findall(r"\w+(?:['’]\w+)?", value.casefold())
for name, raw, kind, count in fixtures:
    source, result = render(raw)
    if kind == '-':
        assert sum(line.startswith('- ') for line in result.splitlines()) == count, (name, result)
    elif kind == 'number':
        assert all(re.search(r'^' + str(i) + r'\.\s+', result, re.M) for i in range(1, count + 1)), result
        assert all(word in result for word in ('First', 'Second', 'Third'))
    elif kind == 'letter':
        assert all(re.search(r'^' + chr(64 + i) + r'\.\s+', result, re.M) for i in range(1, count + 1)), result
    else:
        assert result == raw
    if name == 'explicit_correction':
        assert source != raw and 'bananas' not in result and 'scratch that' not in result
        assert 'oranges' in result and 'apples' in result and 'milk' in result
        assert not formatting_is_safe(raw, result), 'Base guard must not silently permit word loss'
    else:
        assert source == raw
    if name == 'quantities_comma_header':
        assert re.findall(r'\d+', raw) == re.findall(r'\d+', result)
    if name == 'addition_without_stop':
        assert 'three items' in result and 'Oh, and some ice cream' in result
    if name == 'already_bulleted_negation':
        assert result.endswith('Do not add eggs.') and 'without nuts' in result

unclear = [
    'I bought bananas, scratch that oranges, and milk.',
    'Do not make a shopping list with three items, apples, bananas, scratch that oranges, and milk.',
    'I need a shopping list with three items, apples, bananas scratch that oranges, and milk',
    'I need a shopping list with three items, apples, bananas, scratch that',
    'I need a shopping list with four items, apples, bananas, scratch that oranges, and milk',
    'I need a shopping list with three items, apples, a sign saying scratch that oranges, and milk',
    'I need a shopping list with three items, apples, `bananas`, scratch that oranges, and milk',
    'I need a shopping list with three items, apples, bananas, scratch that oranges, scratch that pears, and milk',
    'The words scratch that are part of this sentence.',
    'I saw apples, bananas, scratch that oranges. Make a shopping list with three items, milk, eggs, bread.',
    'Make a shopping list with three items, milk, eggs, bread. Later I saw apples, bananas, scratch that oranges.',
    'I need a shopping list with two items apples, milk Later I saw bananas, scratch that oranges',
    'I need a shopping list with three items, apples, bananas, scratch that is the phrase on the label, and milk.',
    'I need a shopping list with three items, apples, bananas, scratch that means cancel the last item, and milk.',
    'I need a shopping list with three items, apples, bananas, scratch that refers to a correction, and milk.',
]
for raw in unclear:
    assert correction_source(raw) == raw, raw
for prefix in ("Alright, I'm gonna go through the list Number one, basic list. ", 'Number two '):
    raw = prefix + fixtures[0][1]
    source, result = render(raw)
    assert result.startswith(prefix) and sum(line.startswith('- ') for line in result.splitlines()) == 5
variation = 'I need a shopping list with three items or so apples, bananas, actually scratch that oranges and milk'
source, result = render(variation)
assert source == 'I need a shopping list with three items or so apples, oranges and milk'
assert [line for line in result.splitlines() if line.startswith('- ')] == ['- apples', '- oranges', '- and milk']
assert 'three items or so' in result and 'actually' not in result and 'bananas' not in result
for raw in (
    'Make a shopping list with three items or so apples, milk',
    'Make a shopping list with three items or so apples, bananas, milk, eggs',
    'Make a shopping list with three items or four apples, bananas, milk',
    'Make a shopping list with three items or so or so apples, bananas, milk',
    'Make a shopping list with four items or so apples, bananas, actually scratch that oranges and milk',
    'I saw apples, bananas, actually scratch that oranges and milk. Make a shopping list with three items apples, milk, eggs.',
    'Make a shopping list with three items or so apples, bananas, actually scratch that means cancel the last item, milk',
):
    assert correction_source(raw) == raw, raw
    if raw.startswith('Make a shopping list'):
        assert layout_explicit_lists(raw) == raw, raw
# Dictation punctuation is provisional: the explicit Actually cue can mark
# the command boundary, while a prior comma still bounds the cancelled item.
for qualifier in ('', ' or so'):
    for command in ('actually scratch that', 'Actually scratch that', 'ACTUALLY SCRATCH THAT'):
        for before_command in (' ', ', '):
            for after_command in (' ', ', '):
                raw = ('I need a shopping list with three items' + qualifier +
                       ' Apples, Bananas' + before_command + command + after_command + 'oranges and milk')
                source, result = render(raw)
                assert source == ('I need a shopping list with three items' + qualifier + ' Apples, oranges and milk'), (raw, source)
                assert [line for line in result.splitlines() if line.startswith('- ')] == ['- Apples', '- oranges', '- and milk'], result
for raw in (
    'I need a shopping list with three items or so Apples, Bananas scratch that, oranges and milk',
    'I need a shopping list with three items or so Apples Bananas Actually scratch that, oranges and milk',
    'I need a shopping list with four items or so Apples, Bananas Actually scratch that, oranges and milk',
    'I need a shopping list with three items or so Apples, a sign saying Bananas Actually scratch that, oranges and milk',
    'I need a shopping list with three items or so Apples, Bananas Actually scratch that, means the label, and milk',
    'I need a shopping list with three items or so Apples, Bananas. Actually scratch that, oranges and milk',
    'I saw Apples, Bananas Actually scratch that, oranges and milk. Make a shopping list with three items Apples, pears, milk',
):
    assert correction_source(raw) == raw, raw
print('Owner field trials: phrase patterns, exact-word structure, 24 correction punctuation/case combinations and ambiguous refusals passed')

natural = "Okay, I need a shopping list with three items or so. It's going to be apples, bananas, oranges, actually scratch that milk."
source, result = render(natural)
assert source == "Okay, I need a shopping list with three items or so. It's going to be apples, bananas, milk.", source
assert result == "Okay, I need a shopping list with three items or so.\nIt's going to be\n- apples\n- bananas\n- milk.", result
print('Natural setup sentence and correction preserve framing and replace only oranges with milk')
