"""Conservative explicit shopping-list layout; no models, devices or network."""
from pathlib import Path
import re
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from spoken_format import layout_explicit_lists, formatting_is_safe, semantic_content


def words(text):
    return re.findall(r"\w+(?:['\u2019]\w+)?", text.casefold())


def bullets(text):
    return [line[2:].strip().rstrip(',.') for line in text.splitlines() if line.startswith('- ')]


def check(raw, expected, *, intro=None, suffix=None, vocab=()):
    formatted = layout_explicit_lists(raw, vocab)
    assert formatted != raw, ('Expected explicit list layout', raw)
    assert bullets(formatted) == expected, (raw, formatted, expected)
    assert formatting_is_safe(raw, formatted, vocab), ('Full guard rejected layout', raw, formatted)
    assert words(raw) == words(semantic_content(raw, formatted)), ('Changed words/order', raw, formatted)
    if intro is not None:
        assert formatted.startswith(intro), ('Changed intro', intro, formatted)
    if suffix is not None:
        assert formatted.endswith(suffix), ('Changed trailing sentences', suffix, formatted)
    assert layout_explicit_lists(formatted, vocab) == formatted, 'Layout is not idempotent'
    return formatted


owner = 'I need to make a shopping list with five items. One is apple, I need a banana, bag of chips, milk and eggs.'
check(owner, ['One is apple', 'I need a banana', 'bag of chips', 'milk', 'and eggs'],
      intro='I need to make a shopping list with five items.')
# Known unsafe model output drops spoken framing words. The fallback must not.
unsafe_model = 'I need to make a shopping list with five items: apple, banana, bag of chips, milk, and eggs.'
assert not formatting_is_safe(owner, unsafe_model)
check('I need a shopping list with 3 items. Apples, bananas and pears.',
      ['Apples', 'bananas', 'and pears'], intro='I need a shopping list with 3 items.')
check('Write a grocery list with three items: 2 apples, 1 loaf of bread, and 3 bananas.',
      ['2 apples', '1 loaf of bread', 'and 3 bananas'],
      intro='Write a grocery list with three items:')
check('Create a shopping list with four items: apples, mac and cheese, pears and milk.',
      ['apples', 'mac and cheese', 'pears', 'and milk'])
# Without a declared count, final conjunctions may name a compound item.
check('Make a grocery list: apples, pears, mac and cheese.',
      ['apples', 'pears', 'mac and cheese'])
check('Make a shopping list: apples, pears and bananas.',
      ['apples', 'pears and bananas'])
check('I want a shopping list. Apples, bananas, milk. Keep the receipt. Go home afterward.',
      ['Apples', 'bananas', 'milk'], intro='I want a shopping list.',
      suffix='Keep the receipt. Go home afterward.')
check('Make a shopping list: Acme apples, pears, and milk.',
      ['Acme apples', 'pears', 'and milk'], vocab=('Acme',))

# Owner's real recordings: punctuation after the declared item count may be
# missing or a comma; record-end may complete a count that already matches.
check('I need a shopping list with five items Apples, bananas, a bag of chips, milk, and eggs.',
      ['Apples', 'bananas', 'a bag of chips', 'milk', 'and eggs'],
      intro='I need a shopping list with five items')
check('I need a shopping list with 4 items, 2 apples, 3 bananas, 1 carton of milk, and 6 eggs',
      ['2 apples', '3 bananas', '1 carton of milk', 'and 6 eggs'],
      intro='I need a shopping list with 4 items,')
check('I need a shopping list with 4 items Apples, Bananas, Milk, and Eggs',
      ['Apples', 'Bananas', 'Milk', 'and Eggs'])
check('I need a shopping list with three items Bread, Milk, and Eggs Oh, and some ice cream',
      ['Bread', 'Milk', 'and Eggs', 'Oh, and some ice cream'],
      intro='I need a shopping list with three items')
check('I need a shopping list with three items: apples, bananas, and milk. Oh, and some ice cream. Keep the receipt.',
      ['apples', 'bananas', 'and milk', 'Oh, and some ice cream'], suffix='Keep the receipt.')
check('I need a shopping list with three items, apples, oranges, and milk',
      ['apples', 'oranges', 'and milk'])
check('Make a grocery list with three items, apples, pears and bananas',
      ['apples', 'pears', 'and bananas'])
check('Make a shopping list with three items apples, bananas, Oh Henry bars',
      ['apples', 'bananas', 'Oh Henry bars'])


def ordered(raw, expected, markers, *, suffix=None):
    formatted = layout_explicit_lists(raw)
    assert formatted != raw, ('Expected explicit ordered layout', raw)
    lines = [match.groups() for line in formatted.splitlines()
             if (match := re.match(r'^(\d+|[A-Z])[.]\s+(.*)$', line))]
    assert [marker for marker, _ in lines] == markers, (raw, formatted)
    assert [content.rstrip(',.') for _, content in lines] == expected, (raw, formatted)
    assert formatting_is_safe(raw, formatted)
    assert words(raw) == words(semantic_content(raw, formatted)), ('Changed words/order', raw, formatted)
    assert layout_explicit_lists(formatted) == formatted
    if suffix is not None:
        assert formatted.endswith(suffix)


for kind in ('number', 'numbered'):
    ordered('Make a ' + kind + ' list First, wash the vegetables Second, chop them Third, put them in the refrigerator',
            ['First, wash the vegetables', 'Second, chop them', 'Third, put them in the refrigerator'], ['1', '2', '3'])
    ordered('Make a ' + kind + ' list with three items. First, wash vegetables. Second, chop them. Third, refrigerate them. Keep the receipt.',
            ['First, wash vegetables', 'Second, chop them', 'Third, refrigerate them'], ['1', '2', '3'],
            suffix='Keep the receipt.')
for kind in ('letter', 'lettered'):
    ordered('Make a ' + kind + ' list of three options. Take the bus, ride a bicycle, or walk.',
            ['Take the bus', 'ride a bicycle', 'or walk'], ['A', 'B', 'C'])
    ordered('Make a ' + kind + ' list of three options Take the bus, ride a bicycle or walk',
            ['Take the bus', 'ride a bicycle', 'or walk'], ['A', 'B', 'C'])

# Later prose mentioning an ordinal cannot extend a completed list. Continue
# across a sentence only when its next words are the immediately expected cue.
ordered('Make a number list First, wash Second, dry. The word third, is an ordinal.',
        ['First, wash', 'Second, dry'], ['1', '2'], suffix='The word third, is an ordinal.')
ordered('Make a number list First, wash. Second, dry. Third, put everything away.',
        ['First, wash', 'Second, dry', 'Third, put everything away'], ['1', '2', '3'])
ordered('Make a numbered list First, wash Second, dry. Keep this sentence. Third, is an ordinal.',
        ['First, wash', 'Second, dry'], ['1', '2'], suffix='Keep this sentence. Third, is an ordinal.')
ordered('Make a numbered list First, wash. Second, dry. Third, refrigerate. The phrase fourth, is unrelated.',
        ['First, wash', 'Second, dry', 'Third, refrigerate'], ['1', '2', '3'],
        suffix='The phrase fourth, is unrelated.')

unchanged = [
    # Declared counts are admission constraints, not permission to guess.
    'I need a shopping list with five items. Apples, bananas, milk.',
    'I need a shopping list with two items. Apples, bananas, milk.',
    'I want a shopping list with four items. Apples, mac and cheese, milk.',
    'Make a shopping list with six items: apples, pears and milk.',
    # Count phrases must match completely; a numeric suffix is not a count.
    'Make a shopping list with twenty five items. Apples, bananas, pears, milk, eggs.',
    'Make a shopping list with 2.5 items. Apples, bananas, milk, pears, eggs.',
    'Make a shopping list with 2 to 3 items. Apples, bananas, milk.',
    'Make a shopping list with fewer than three items. Apples, bananas, milk.',
    'Do not turn this into a list. I need a shopping list with three items. apple, banana, eggs.',
    # Ordinary, negated, quoted and metadescriptive prose stays prose.
    'I bought apples, bananas, milk.',
    'A shopping list can contain apples, bananas, milk.',
    'I do not want a shopping list. Apples, bananas, milk.',
    'Do not make a shopping list: apples, bananas, milk.',
    'I never need a shopping list. Apples, bananas, milk.',
    'I want no shopping list. Apples, bananas, milk.',
    '\"Make a shopping list: apples, bananas, milk.\"',
    'She said make a shopping list. Apples, bananas, milk.',
    'Explain how to make a shopping list. Apples, bananas, milk.',
    'The phrase make a shopping list means apples, bananas, milk.',
    # Broader kinds of lists and unframed requests are outside this heuristic.
    'Make a todo list: wash clothes, buy soap, cook dinner.',
    'I need to make a shopping list apples, bananas, milk.',
    # Technical/code/operator and quoted-item content stays untouched.
    r'Make a shopping list: apples, C:\work\cart.txt, milk.',
    'Make a shopping list: apples, run --dry-run, milk.',
    'Make a shopping list: apples, x + y, milk.',
    'Make a shopping list: apples, `milk`, pears.',
    'Make a shopping list: apples, "milk", pears.',
    'Make a shopping list: apples, x -> y, milk.',
    # Existing structure and incomplete fragment cues must not be rearranged.
    'Make a shopping list:\n- apples\n- bananas\n- milk',
    'Make a shopping list:\n1. apples\n2. bananas\n3. milk',
    'Make a shopping list: apples, bananas,',
    'Make a shopping list: apples, bananas',
    'I need to make a shopping list with five items.',
    'One is apple, I need a banana, bag of chips, milk and eggs.',
    'Make a shopping list: apples,, milk.',
    # Missing punctuation is admitted only after exact, clear count framing.
    'Make a shopping list Apples, bananas, milk.',
    'Make a shopping list, apples, bananas, milk.',
    'Make a shopping list with three items apples, bananas',
    'Make a shopping list with three items apples, bananas,',
    'Make a shopping list with three items apples, bananas, and',
    'Make a shopping list with three items apples, bananas, or',
    'Make a shopping list with three items apples, bananas, AND.',
    'Make a shopping list with three items apples, bananas, and:',
    'Make a letter list of three options apples, bananas, or',
    'Make a grocery list with three items apples, and, bananas.',
    'Make a shopping list with four items apples, bananas, pears and',
    'Make a shopping list with four items apples, bananas, pears or',
    'Make a shopping list with twenty five items apples, bananas, milk, pears, eggs',
    'Make a shopping list with 2.5 items apples, bananas, milk, pears, eggs',
    'Make a shopping list with 2 to 3 items apples, bananas, milk',
    'Make a shopping list with fewer than three items apples, bananas, milk',
    'Make a shopping list with three items or four apples, bananas, milk',
    # Number words are content; skipped/repeated/unframed ordinals are ambiguous.
    'Make a number list First, wash Third, dry',
    'Make a numbered list First, wash Second, dry Second, stop',
    'Make a number list First, wash Second,',
    'Make a number list with three items First, wash Second, dry',
    'Make a number list wash, dry, stop.',
    'Make a number list First wash Second dry Third stop',
    'Make a number list First, wash Second, dry the word third, is an ordinal.',
    'Make a number list First, wash Second, discuss the term third, before leaving.',
    'Make a letter list of three options take the bus, ride a bike',
    'Explain how to make a number list First, wash Second, dry',
    'Do not make a letter list of three options. Bus, bike, walk.',
    # A followup is additional only after the initial spoken count is complete.
    'I need a shopping list with three items apples, bananas Oh, and milk',
    'I need a shopping list with three items apples, bananas, Oh, and milk',
    'I need a shopping list with three items apples, bananas, milk Oh, and',
    'I need a shopping list with three items apples, bananas, milk Oh, and ice cream, cake',
    'I need a shopping list with three items apples, bananas, milk. Keep the receipt. Oh, and ice cream.',
    'I need a shopping list with three items apples, bananas, milk Oh, and run --dry-run',
    'I need a shopping list with three items, apples, bananas, scratch that oranges, and milk',
    'I need a shopping list with four items, apples, bananas, scratch that oranges, and milk',
    'Make a number list First, run --dry-run Second, stop',
]
for raw in unchanged:
    formatted = layout_explicit_lists(raw)
    assert formatted == raw, ('Ambiguous or unsupported source was changed', raw, formatted)
print(f'Explicit lists: shopping/ordinal/letter layouts, exact owner recordings, full preservation/idempotence, and {len(unchanged)} conservative refusals passed')
