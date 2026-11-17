"""Original-source partition, immutable provenance and bounded natural lists."""
from dataclasses import FrozenInstanceError
from pathlib import Path
import re
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import spoken_format
from spoken_format import (format_spoken_lists, formatting_is_safe,
                           layout_explicit_lists, prepare_spoken_list, list_layout_is_safe)
from spoken_corrections import correction_source
from spoken_lists import ListPlan, Span, parse_spoken_list


def words(text):
    return re.findall(r"\w+(?:['’]\w+)?", text.casefold())


def check(raw, *, corrected=False, count=3):
    prepared = prepare_spoken_list(raw)
    assert prepared is not None, raw
    plan = prepared.plan
    assert isinstance(plan, ListPlan)
    cursor = 0
    for span in plan.partition:
        assert isinstance(span, Span)
        assert span.start == cursor and span.start < span.end <= len(raw)
        cursor = span.end
        if not span.keep:
            assert corrected and span.role in ('cancelled', 'command', 'separator')
    assert cursor == len(raw)
    assert ''.join(raw[s.start:s.end] for s in plan.partition) == raw
    assert plan.normalized_source() == prepared.source
    assert plan.render() == prepared.formatted
    assert len(plan.items) == count
    retained = ''.join(raw[s.start:s.end] for s in plan.partition if s.keep)
    assert retained == prepared.source
    assert words(retained) == words(spoken_format.semantic_content(retained, prepared.formatted))
    assert formatting_is_safe(prepared.source, prepared.formatted)
    assert format_spoken_lists(prepared.formatted) == prepared.formatted
    assert layout_explicit_lists(prepared.formatted) == prepared.formatted
    assert correction_source(raw) == prepared.source
    for value, attribute in ((plan, 'original'), (plan.partition[0], 'start'),
                             (prepared, 'source')):
        try:
            setattr(value, attribute, None)
        except FrozenInstanceError:
            pass
        else:
            raise AssertionError('Mutable parse result')
    return prepared


latest = "Okay, I need a shopping list with three items or so. It's going to be apples, bananas, oranges, actually scratch that milk."
expected = "Okay, I need a shopping list with three items or so.\nIt's going to be\n- apples\n- bananas\n- milk."
prepared = check(latest, corrected=True)
assert prepared.formatted == expected
assert prepared.source == "Okay, I need a shopping list with three items or so. It's going to be apples, bananas, milk."
assert not formatting_is_safe(latest, prepared.formatted)
assert parse_spoken_list(latest) is None
assert layout_explicit_lists(latest) == latest
assert prepare_spoken_list(latest, allow_corrections=False) is None

# One public preparation invokes the original parser exactly once. Neither
# normalization nor rendering invokes recognition on the changed source.
with patch.object(spoken_format, 'parse_spoken_list', wraps=parse_spoken_list) as parser:
    result = prepare_spoken_list(latest)
    assert result.formatted == expected
    assert parser.call_count == 1 and parser.call_args.args == (latest,)
    result.plan.normalized_source()
    result.plan.render()
    assert parser.call_count == 1

cases = 0
for intro in ("It's going to be", 'It is going to be', 'The items are',
              'They are', 'Here are the items', 'It’s going to be'):
    for setup_separator in (' ', '. ', ': ', ', '):
        for intro_separator in (' ', ': ', ', '):
            for command in ('scratch that', 'Actually scratch that'):
                raw = ('Okay, I need a grocery list with three items or so' +
                       setup_separator + intro + intro_separator +
                       'café apples, bananas, ' + command + ', pears, and milk.')
                value = check(raw, corrected=True)
                assert value.formatted.splitlines()[0].startswith('Okay, I need')
                assert value.formatted.splitlines()[1].startswith(intro)
                assert [line for line in value.formatted.splitlines() if line.startswith('- ')] == [
                    '- café apples', '- pears', '- and milk.']
                assert 'bananas' not in value.source and 'scratch that' not in value.source.lower()
                cases += 1

for raw in (
    'Make a shopping list with three items apples, bananas, scratch that pears, and milk',
    'Make a shopping list with three items apples, bananas, milk, actually scratch that eggs',
    'Make a shopping list with three items apples, bananas, scratch that pears and milk',
    'Make a shopping list with three items apples, mac and cheese, and milk.',
    'Make a shopping list with three items One is apple, I need a banana, we will buy milk.',
    'Make a shopping list with three items. The items are 2 apples, 3 pears, and 6 eggs. Keep the receipt.',
):
    check(raw, corrected='scratch that' in raw)

base = ', '.join('item' + str(i) for i in range(1, 21))
check('Make a shopping list with twenty items ' + base + ' Oh, and ice cream.', count=21)
check('Make a numbered list First, wash. Second, dry. Third, put everything away.')
check('Make a letter list of three options. Bus, bicycle, or walk.')

# A completed item can appear in the live overlay before later items arrive.
grocery_heading = 'Create a list for groceries.'
grocery_sentences = ('First item is apples.', 'Number two is oranges.', 'Number three is grapes.')
grocery_lines = ('1. apples', '2. oranges', '3. grapes')
for completed in range(1, len(grocery_sentences) + 1):
    raw = ' '.join((grocery_heading, *grocery_sentences[:completed]))
    expected = '\n'.join((grocery_heading, *grocery_lines[:completed]))
    grocery = prepare_spoken_list(raw)
    assert grocery is not None and grocery.formatted == expected, (raw, grocery)
    assert [span.role for span in grocery.plan.partition if not span.keep] == ['spoken marker'] * completed
    assert layout_explicit_lists(raw) == raw  # Model punctuation cannot authorize marker removal.
    assert format_spoken_lists(expected) == expected

needs = check('We need to buy apples, oranges, grapes, and pears.', count=4)
assert needs.formatted == 'We need to buy\n- apples\n- oranges\n- grapes\n- and pears.'
assert check('Make a bulleted list: apples, oranges, and grapes.', count=3).formatted == (
    'Make a bulleted list:\n- apples\n- oranges\n- and grapes.')
assert prepare_spoken_list('Create a list for groceries. First item is apples? Number two is oranges.').formatted == (
    'Create a list for groceries.\n1. apples?\n2. oranges')

# ASR may leave whitespace before a sentence stop. Content and stop remain
# separate original ranges; recognition must not require adjacent characters.
for raw, count in (
    ('Make a shopping list with three items. apples, bananas, milk .', 3),
    ('Make a numbered list First, wash . Second, dry . Third, refrigerate .', 3),
    ('Make a shopping list with three items apples, bananas, milk . Oh, and ice cream .', 4),
):
    value = check(raw, count=count)
    assert value.source == raw
    assert value.formatted != raw
    assert value.formatted.endswith(' .'), value.formatted

refusals = [
    "I need a shopping list with three items. It's supposed to be apples, bananas, milk.",
    'I need a shopping list with three items. It’s supposed to be apples, bananas, milk.',
    'I need a shopping list with three items. It is supposed to be apples, bananas, milk.',
    'I need a shopping list with three items. The items include apples, bananas, milk.',
    'I need a shopping list with three items. They include apples, bananas, milk.',
    'I need a shopping list with three items. Here we have apples, bananas, milk.',
    'Make a shopping list with three items. These are apples, bananas, scratch that milk.',
    'Make a shopping list with three items. Some setup. They are apples, bananas, milk.',
    'Make a shopping list with three items. They are the items are apples, bananas, milk.',
    'Make a shopping list with three items. They are apples, bananas, milk. Here are the items pears.',
    'Make a shopping list with three items. They are apples, scratch that pears, milk.',
    'Make a shopping list with three items apples, bananas. Actually scratch that milk, pears.',
    'Make a shopping list with three items apples, bananas, scratch that. Milk, pears.',
    'Make a shopping list with three items apples, bananas, scratch that means the label, milk.',
    'Make a shopping list with three items apples, bananas, scratch that pears, scratch that eggs, milk.',
    'Make a shopping list with four items apples, bananas, scratch that pears, milk.',
    'Make a shopping list with three items apples, a sign saying bananas, scratch that pears, milk.',
    'Do not format a list. Make a shopping list with three items apples, bananas, milk.',
    'Make a shopping list with three items apples, run --dry-run, milk.',
    'Make a shopping list with three items apples, x + y, milk.',
    '"Make a shopping list with three items apples, bananas, milk."',
    'Make a shopping list with three items apples, `bananas`, milk.',
    'Make a shopping list with three items. They are apples, bananas,',
    'Make a shopping list with three items or so. They are apples, milk.',
    'Make a shopping list with three items apples, bananas, milk. Make a grocery list pears, eggs, butter.',
    'Make a shopping list with ' + '9' * 5000 + ' items apples, pears, milk.',
    'Make a shopping list with three items apples, ' + 'x' * 201 + ', scratch that pears, milk.',
    'Create a list for groceries.',
    'Create a list for groceries. First item is apples. Number three is grapes.',
    'Create a list for groceries. First item is apples. Number two is oranges. Number two is pears.',
    'Create a list for groceries. Number two is oranges. First item is apples.',
    'Create a list for groceries. First item is apples with a label that says Number two is oranges.',
    'Create a list for groceries. First item is apples with the label Number two is oranges.',
    'Create a list for groceries. First item is apples with the label, Number two is oranges.',
    'Create a list for groceries. First item is apples and the sign reads, Number two is oranges.',
    'I had a meeting with Alice, Bob, and Carol.',
    'We need to wash, dry, and fold the towels.',
    'We need apples, oranges, or grapes.',
    'We need to buy apples, oranges, grapes, and Sam will bring pears.',
    'We need to buy apples, oranges, grapes, and then call Sam.',
    'We need to buy apples, Dad is cooking, the store closes early, and Mom has the cash.',
    "I need your help, I feel overwhelmed, I missed work, and I can't sleep.",
    'I need a minute, seriously, please, and thanks.',
    'I need to get some sleep, I worked late, the alarm is set, and I feel awful.',
]
for raw in refusals:
    assert parse_spoken_list(raw, allow_corrections=True) is None, raw
    assert format_spoken_lists(raw) == raw, raw
    assert correction_source(raw) == raw, raw
for raw in (None, 12, [], '', 'a' * 2_000_001):
    assert parse_spoken_list(raw) is None

counted = 'Today I have three tasks. First email Alex the 17 files. Second do not delete the backup. Third review the report.'
counted_source = 'Today I have three tasks. email Alex the 17 files. do not delete the backup. review the report.'
numbered = 'Today I have three tasks:\n\n1. Email Alex the 17 files.\n2. Do not delete the backup.\n3. Review the report.'
prepared = prepare_spoken_list(counted)
assert prepared is not None and prepared.source == counted_source
assert len(prepared.plan.items) == 3 and prepared.plan.markers == ('1.', '2.', '3.')
assert [span.role for span in prepared.plan.partition if not span.keep] == ['spoken marker'] * 3
assert ''.join(counted[s.start:s.end] for s in prepared.plan.partition) == counted
assert formatting_is_safe(prepared.source, numbered)
assert list_layout_is_safe(prepared, numbered)
assert list_layout_is_safe(prepared, prepared.formatted)
assert list_layout_is_safe(prepared, numbered.replace('1. Email Alex the 17 files.',
                                                      '1. Email Alex\n   the 17 files.'))
for candidate in (
    'Today I have three tasks:\n- Email Alex the 17 files. Do not delete the backup.\n- Review the report.',
    'Today I have three tasks:\n1. Email Alex\n2. the 17 files. Do not delete the backup.\n3. Review the report.',
    'Today I have three tasks. Email Alex the 17 files. Do not delete the backup. Review the report.',
):
    assert formatting_is_safe(prepared.source, candidate), 'Boundary test must pass the existing word guard'
    assert not list_layout_is_safe(prepared, candidate), ('Changed original item boundaries', candidate)
assert not formatting_is_safe(counted, numbered), 'The generic guard must not consume ordinal content'
assert layout_explicit_lists(counted) == counted, 'Model punctuation cannot create original-speech authority'
for raw in (
    'There are two steps: First, open the report. Second, review it.',
    'Create a list with three items: First, apples. Second, oranges. Third, milk.',
    'Our plan has two steps. First wash the vegetables. Second cook them.',
):
    value = prepare_spoken_list(raw)
    assert value is not None and len(value.plan.items) in (2, 3), raw
    assert formatting_is_safe(value.source, value.formatted)
for raw in (
    '"' + counted + '"',
    counted.replace('First email', 'First `email`'),
    counted.replace('I have three', 'I do not have three'),
    'Do not format a list. ' + counted,
    'The phrase is ' + counted,
    'If ' + counted,
    counted.replace('email Alex the 17 files', r'open C:\work\app.py'),
    'I have three items. First aid kits. Second hand chairs. Third party software.',
    'I have three tasks. First National Bank approval. Second Street inspection. Third Avenue plans.',
    counted.replace('three tasks', 'four tasks'),
    counted.replace('three tasks', 'about three tasks'),
    counted.replace('three tasks', 'twenty three tasks'),
    counted.replace('Second do', 'Third do'),
    counted.replace('Third review', 'Second review'),
    counted.replace('. First email', ' First email'),
):
    assert prepare_spoken_list(raw) is None, ('Unclear/literal counted ordinal list', raw)

for prefix in ("Don't format this. ", 'Don’t format this. ', 'Don‘t format this. ',
               "This doesn't need formatting. ", 'This doesn’t need formatting. ', 'This doesn‘t need formatting. ',
               "I can't have a list. ", 'I can’t have a list. ', 'I can‘t have a list. ',
               'The text reads: ', 'The text says: ', 'He mentioned: ', 'She wrote: '):
    for body in (counted,
                 'I need a shopping list with three items. First item is apples. Number two is oranges. Number three is milk.',
                 'I need a shopping list with three items. Apples, oranges, milk.'):
        raw = prefix + body
        assert prepare_spoken_list(raw) is None, ('Reported text or formatting refusal granted list authority', raw)
        assert format_spoken_lists(raw) == raw and correction_source(raw) == raw, raw
        assert layout_explicit_lists(raw) == raw, raw
for apostrophe in ("'", '’', '‘'):
    raw = 'Don' + apostrophe + 't format this. Make a shopping list with three items. Apples, milk, eggs.'
    assert prepare_spoken_list(raw) is None, raw

print(f'Natural lists: {cases} introductory/correction variants, partition, retained lexical proof, immutable results, single parse, 21-item compatibility and {len(refusals)} refusals passed')
