"""Pure presentation safety checks; no model, device or network access."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from dictation_quality import cleanup_is_safe
from spoken_format import FORMAT_INSTRUCTIONS, formatting_is_safe, semantic_content


def run():
    positive = [
        ('Buy apples and pears', 'Buy apples and pears.'),
        ('apples pears bananas', '- apples\n* pears\n+ bananas'),
        ('First wash hands then dry hands', '1. First wash hands\n2. then dry hands'),
        ('wash hands dry hands', 'A. wash hands\nB. dry hands'),
        ('wash hands dry hands', 'a) wash hands\nb) dry hands'),
        ('wash hands dry hands', '1) wash hands\n\n2) dry hands'),
        ('apples 2 pears 3', '| Item | Value |\n| --- | ---: |\n| apples | 2 |\n| pears | 3 |'),
        ('Item Quantity apples 3 oranges 2 grapes 1',
         '| Item | Quantity |\n| --- | ---: |\n| apples | 3 |\n| oranges | 2 |\n| grapes | 1 |'),
        ('Item Value apples 2 pears 3', '| Item | Value |\n| :--- | ---: |\n| apples | 2 |\n| pears | 3 |'),
        ('Item Alice 20', '| Item | Value |\n| --- | --- |\n| Alice | 20 |'),
        ('Alice 20 Bob 30', 'Item | Measure\n--- | ---\nAlice | 20\nBob | 30'),
        ('Use `a|b` now', '| Item | Details |\n| --- | --- |\n| Use `a|b` | now |'),
        (r'Use a\|b now', '| Item | Details |\n| --- | --- |\n| Use a\\|b | now |'),
        ('Use `foo.py|bar.py` now', '| Item | Details |\n| --- | --- |\n| Use `foo.py|bar.py` | now |'),
        ('run --dry-run then stop', '- run --dry-run\n- then stop'),
        ('x + y then x - y', '- x + y\n- then x - y'),
        ('Use `x + y` then stop', '- Use `x + y`\n- then stop'),
        ('x = -5 y >= 3', '- x = -5\n- y >= 3'),
        ('x | y then x && y', '- x | y\n- then x && y'),
        ('x * (y + z)', '- x * (y + z)'),
        ('price $5', '- price $5'),
        (r'open C:\work\app.py then stop', '- open C:\\work\\app.py\n- then stop'),
        ('Start -> Stop', '+-------+\n| Start -> Stop |\n+-------+'),
    ]
    negative = [
        ('apples 2 pears 3', '| Item | Value |\n| --- | --- |\n| apples | 4 |\n| pears | 3 |'),
        ('do not delete files', '- do delete files'),
        ('apples pears bananas', '- apples\n- pears'),
        ('Alice Bob', '- Alice\n- Bill'),
        ('Alice Bob', '- Bob\n- Alice'),
        (r'open C:\work\app.py', '- open C:/work/app.py'),
        ('run --dry-run', '- run --dry_run'),
        ('run --dry-run', '- run dry-run'),
        ('1 apples 2 pears', '1. apples\n2. pears'),
        ('apples pears', '2. apples\n3. pears'),
        ('apples pears', '1. apples\n3. pears'),
        ('apples pears', 'A. apples\nC. pears'),
        ('apples', '1. apples'),
        ('Alice 20 Bob 30', '| Evil | Value |\n| --- | --- |\n| Alice | 20 |\n| Bob | 30 |'),
        ('Alice 20', '| Item hacked | Value |\n| --- | --- |\n| Alice | 20 |'),
        ('Alice 20', '| Item | Value |\n| --- | --- |\n| Alice | 20 | extra |'),
        ('Alice 20', '| Item | Value |\n| --- | --- |\n| Alice | |'),
        ('Alice 20', '| Item | Value |\n| --- | --- |'),
        ('Use `a|b` now', '| Item | Details |\n| --- | --- |\n| Use `a|c` | now |'),
        ('Use `a+b` now', '| Item | Details |\n| --- | --- |\n| Use `a-b` | now |'),
        ('Use `foo.py|bar.py` now', '| Item | Details |\n| --- | --- |\n| Use `foo.py|baz.py` | now |'),
        ('Alice Bob', 'Alice -> Bob'),
        ('Alice -> Bob Carol', 'Alice Bob -> Carol'),
        ('not Alice Bob', '| Item | Value |\n| --- | --- |\n| Alice | Bob |'),
        ('Alice Bob', '| Item | Value |\n| --- | --- |\n| Alice | Bob |\n| Category | Measure |'),
        ('x + y', '- x\n- y'),
        ('x - y', '- x\n- y'),
        ('x * y', '- x\n- y'),
        ('x = y', '- x\n- y'),
        ('x + y', '- x - y'),
        ('x = y', '- x == y'),
        ('x <= y', '- x >= y'),
        ('x | y', '- x & y'),
        ('x || y', '- x | y'),
        ('x && y', '- x & y'),
        ('x / y', '- x\n- y'),
        ('x ^ y', '- x\n- y'),
        ('x % y', '- x\n- y'),
        ('! x', '- x'),
        ('~ x', '- x'),
        ('quantity -5', '- quantity 5'),
        ('quantity - 5', '- quantity 5'),
        ('x + y z', '- x y + z'),
        ('x * (y + z)', '- x * y + z'),
        ('x := y', '- x = y'),
        ('price $5', '- price 5'),
        ('x ≤ y', '- x ≥ y'),
    ]
    count = 0
    for raw, formatted in positive:
        assert formatting_is_safe(raw, formatted), ('rejected', raw, formatted, semantic_content(raw, formatted))
        count += 1
    for raw, formatted in negative:
        assert not formatting_is_safe(raw, formatted), ('accepted', raw, formatted, semantic_content(raw, formatted))
        count += 1
    for raw, formatted in [(None, 'text'), ('text', None), ('', ''), ('text', '')]:
        assert not formatting_is_safe(raw, formatted)
        count += 1
    assert not cleanup_is_safe('wash hands dry hands', '1. wash hands\n2. dry hands')
    assert 'pasteable' in FORMAT_INSTRUCTIONS
    print(f'{count + 2} spoken formatting checks passed')


if __name__ == '__main__':
    run()
