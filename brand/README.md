# Listen: brand sheet

**Name:** Listen (capital L). Used in the window title, tray tooltip, tray menu, messages and documents. Program files and the project folder keep their old names on purpose.

**Idea:** the app listens the way a good listener does: quietly, accurately, without a fuss. The mark is your own voice as the five bars you already see in the app while you speak.

## The mark: Level

Five rounded vertical bars on a 64-unit grid. The loudest, centre bar is **Signal Blue** (`#5B8CFF`); the others are warm off-white (`#F0EDE8`) at 50% and 80% opacity. On the app tile it sits on warm graphite (`#1B1915`) with a hairline edge.

The shape lives in one place, `LEVEL_BARS` in `dictate.py`. Everything else is made from it:

| File | What it is |
|---|---|
| `brand/listen-mark.svg` | Mark on the dark tile. The main logo. |
| `brand/listen-mark-light.svg` | Mark on a light tile (documents, installers). Blue becomes `#3F6FE6`. |
| `brand/listen-bars.svg` | The bars alone, for dark backgrounds. |
| `ui/app.png`, `ui/app.ico` | App icon for the window and taskbar (16 to 256 px). |
| tray icons | Drawn by `level_icon()` in `dictate.py`, on the same tile. |

To regenerate every file after changing the table: `py brand/make_assets.py`. `tests/brand_test.py` fails if any of them drift apart.

## Tray icon states

The loud bar takes the status colour, everything else stays put: blue at rest, rose while recording or on error, amber while working, grey while loading. The colours follow the Status Triplet Rule in `DESIGN.md`.

## Use

- Clear space: at least one bar width (7 units, about 11% of the tile) on every side.
- Smallest sizes: tile 16 px (tray). Below that, use the tray version, never a shrunk logo.
- On light backgrounds use the light tile version, never the bare bars.
- Colours and type come from `DESIGN.md`; the wordmark is "Listen" in Segoe UI Variable, semibold.

## Don't

- Change the number, order or heights of the bars.
- Recolour the loud bar to anything but Signal Blue (logo) or a status colour (tray).
- Add shadows, gradients, outlines or a glow to the mark.
- Stretch it, rotate it, or place it on a busy picture.
- Use lowercase "listen" for the product name.
