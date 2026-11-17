"""Regenerate every Listen logo file from the one bar table in dictate.py.

Run from the project root:  py brand/make_assets.py

Writes brand/listen-mark.svg, brand/listen-mark-light.svg, brand/listen-bars.svg and the app icon files
ui/app.png and ui/app.ico. Reads LEVEL_BARS, LEVEL_BAR_WIDTH and level_icon() straight from dictate.py (no
import, so nothing starts), so the app, the tray and the logo files can never disagree about the shape.
"""
import ast
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
BLUE, BLUE_ON_LIGHT = '#5B8CFF', '#3F6FE6'
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def load_mark():
    source = ast.parse((ROOT / 'dictate.py').read_text(encoding='utf-8'))
    wanted = [n for n in source.body
              if (isinstance(n, ast.FunctionDef) and n.name == 'level_icon')
              or (isinstance(n, ast.Assign) and any(getattr(t, 'id', '') in ('LEVEL_BARS', 'LEVEL_BAR_WIDTH') for t in n.targets))]
    namespace = {'Image': Image, 'ImageDraw': ImageDraw}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), '<level_icon>', 'exec'), namespace)
    return namespace


def svg(bars, width, tile, ink, blue, bare=False):
    """The mark as SVG on the same 64-unit grid the app draws on."""
    parts = []
    if not bare:
        parts.append('<rect x="1.5" y="1.5" width="61" height="61" rx="14" fill="%s" stroke="%s"/>' % (
            tile, 'rgba(255,244,232,.18)' if tile == '#1B1915' else 'rgba(27,25,21,.2)'))
    for x, y, height, opacity in bars:
        fill = blue if opacity is None else ink
        extra = '' if opacity is None else ' fill-opacity="%s"' % opacity
        x_text = ('%g' % x)
        parts.append('<rect x="%s" y="%g" width="%g" height="%g" rx="%g" fill="%s"%s/>' % (x_text, y, width, height, width / 2, fill, extra))
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="256" height="256" role="img" aria-label="Listen">\n  '
            + '\n  '.join(parts) + '\n</svg>\n')


def main():
    mark = load_mark()
    bars, width, level_icon = mark['LEVEL_BARS'], mark['LEVEL_BAR_WIDTH'], mark['level_icon']
    brand = ROOT / 'brand'
    brand.mkdir(exist_ok=True)
    (brand / 'listen-mark.svg').write_text(svg(bars, width, '#1B1915', '#F0EDE8', BLUE), encoding='utf-8', newline='\n')
    (brand / 'listen-mark-light.svg').write_text(svg(bars, width, '#F2EEE8', '#1B1915', BLUE_ON_LIGHT), encoding='utf-8', newline='\n')
    (brand / 'listen-bars.svg').write_text(svg(bars, width, None, '#F0EDE8', BLUE, bare=True), encoding='utf-8', newline='\n')
    level_icon(BLUE, tile=True, size=256).save(ROOT / 'ui' / 'app.png')
    frames = [level_icon(BLUE, tile=True, size=n) for n in ICON_SIZES]
    frames[-1].save(ROOT / 'ui' / 'app.ico', format='ICO', sizes=[(n, n) for n in ICON_SIZES], append_images=frames[:-1])
    print('wrote brand/listen-mark.svg, brand/listen-mark-light.svg, brand/listen-bars.svg, ui/app.png, ui/app.ico')


if __name__ == '__main__':
    main()
