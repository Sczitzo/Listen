"""One focus-preserving, session-only dictation overlay."""
import ctypes
from ctypes import wintypes
import os
import math
import time
import tkinter as tk

from PIL import ImageTk
from target_icon import window_icon
from preview_shadow import CardShadow

C = dict(bg='#2d2925', border='#a49380', fg='#f1e9dd', status='#c8bdaf',
         thumb_hover='#dcc09a', thumb='#a49380', accent='#dcc09a', on_accent='#282117')
OVERLAY_WIDTH = 380
HEADER_HEIGHT = 110
FOOTER_ROW_HEIGHT = 32
FOOTER_PADDING = 12
FOOTER_HEIGHT = FOOTER_ROW_HEIGHT + 2 * FOOTER_PADDING


def voice_level(levels):
    """Use the main meter's gain, with a perceptual response for quiet speech."""
    recent = [value for value in levels[-5:]
              if isinstance(value, (int, float)) and math.isfinite(value) and value > 0]
    return math.sqrt(min(1., max(recent, default=0.) * 6))


def client_animation_enabled():
    try:
        query = ctypes.WinDLL('user32', use_last_error=True).SystemParametersInfoW
        query.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
        query.restype = wintypes.BOOL
        enabled = wintypes.BOOL()
        return bool(query(0x1042, 0, ctypes.byref(enabled), 0) and enabled.value)
    except (AttributeError, OSError):
        return False


def card_bounds(work_area, width=720, height=310, margin=16, *, centered=False):
    left, top, right, bottom = work_area
    width = min(width, max(1, right - left - 2 * margin))
    height = min(height, max(1, bottom - top - 2 * margin))
    x = left + (right - left - width) // 2 if centered else left + margin
    return width, height, x, bottom - margin - height


def work_area(anchor=0):
    class MonitorInfo(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('monitor', wintypes.RECT),
                    ('work', wintypes.RECT), ('flags', wintypes.DWORD)]
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user.MonitorFromWindow.restype = wintypes.HANDLE
    user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
    user.GetMonitorInfoW.restype = wintypes.BOOL
    monitor = user.MonitorFromWindow(anchor or 0, 2 if anchor else 1)
    info = MonitorInfo()
    info.size = ctypes.sizeof(info)
    if not monitor or not user.GetMonitorInfoW(monitor, ctypes.byref(info)):
        raise OSError('Monitor work area unavailable')
    return info.work.left, info.work.top, info.work.right, info.work.bottom


def fitted_height(content_pixels, area, previous=0, chrome=56):
    """Grow within a recording, leaving nearly half the work area uncovered."""
    available = max(1, area[3] - area[1] - 32)
    maximum = min(520, available, max(1, int(available * .55)))
    return min(maximum, max(90, previous, int(content_pixels) + chrome))


def text_pixels(widget):
    result = widget.count('1.0', 'end', 'update', 'ypixels')
    return int(result[0] if isinstance(result, tuple) else result or 0)


def draw_card_border(canvas, width, height, top=1):
    """Keep the complete rounded stroke inside the native window's clip."""
    canvas.delete('card-border')
    if width < 4 or height < 4:
        return
    left, right, bottom = 1, width - 2, height - 2
    top = min(max(1, top), bottom - 1)
    radius = min(24, (right - left) / 2, (bottom - top) / 2)
    options = dict(fill=C['border'], width=1, tags='card-border')
    for points in ((left + radius, top, right - radius, top),
                   (right, top + radius, right, bottom - radius),
                   (left + radius, bottom, right - radius, bottom),
                   (left, top + radius, left, bottom - radius)):
        canvas.create_line(*points, **options)
    for box, start in (((left, top, left + 2 * radius, top + 2 * radius), 90),
                       ((right - 2 * radius, top, right, top + 2 * radius), 0),
                       ((right - 2 * radius, bottom - 2 * radius, right, bottom), 270),
                       ((left, bottom - 2 * radius, left + 2 * radius, bottom), 180)):
        canvas.create_arc(*box, start=start, extent=90, style='arc',
                          outline=C['border'], width=1, tags='card-border')
    canvas.tag_lower('card-border')


def place_card(window, anchor, area=None, height=310, *, width=720, centered=False):
    width, height, x, y = card_bounds(work_area(anchor) if area is None else area,
                                      width=width, height=height, centered=centered)
    window.geometry(f'{width}x{height}')
    window.update_idletasks()
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.GetParent.argtypes = [wintypes.HWND]
    user.GetParent.restype = wintypes.HWND
    user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user.SetWindowPos.restype = wintypes.BOOL
    user.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
    user.SetWindowRgn.restype = ctypes.c_int
    # Tk interprets negative geometry offsets from the screen's right/bottom edge.
    # Native placement uses actual work-area coordinates, including negative monitors.
    hwnd = user.GetParent(window.winfo_id())
    if not user.SetWindowPos(hwnd, -1, x, y, width, height, 0x10):  # HWND_TOPMOST | SWP_NOACTIVATE
        raise OSError('Preview placement unavailable')
    gdi = ctypes.WinDLL('gdi32', use_last_error=True)
    gdi.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
    gdi.CreateRoundRectRgn.restype = wintypes.HANDLE
    gdi.DeleteObject.argtypes = [wintypes.HANDLE]
    radius = min(50, width, height)
    region = gdi.CreateRoundRectRgn(0, 0, width + 1, height + 1, radius, radius)
    # On success the window owns the region.
    if region and not user.SetWindowRgn(hwnd, region, True):
        gdi.DeleteObject(region)


def card_content(snapshot, now=None):
    now = time.perf_counter() if now is None else now
    phase = snapshot.get('phase', 'recording' if snapshot.get('active') else 'idle')
    if phase == 'final':
        outcome = snapshot.get('final_outcome', '')
        if outcome in ('sent', 'pasted') or (snapshot.get('dismiss_at') or 0) <= now:
            return False, '', ''
        return True, '', {
            'no_speech': 'No speech heard',
            'copied': 'Text copied · Ctrl+V to paste', 'uncertain': 'Check the original app for your text',
            'failed': 'Processing failed', 'review': 'Review in Listen', 'retained': 'Open Listen to recover text',
        }.get(outcome, 'Check delivery in Listen')
    if phase != 'recording':
        return False, '', ''
    raw = snapshot.get('text') or ''
    draft = snapshot.get('draft_text') or ''
    covered = snapshot.get('draft_covered_chars', 0)
    text = raw
    if draft and 0 < covered <= len(raw):
        tail = raw[covered:]
        text = draft + ('\n\n' + tail.lstrip() if tail and '\n' in draft else tail)
    if snapshot.get('state') == 'paused':
        status = 'Listening · Preview paused'
    elif snapshot.get('draft_state') == 'updating':
        status = 'Listening · Cleaning preview…'
    elif snapshot.get('draft_reason') in ('length', 'long transcript'):
        status = 'Listening · Cleanup on Finish'
    elif text != raw:
        status = 'Listening · Preview cleaned'
    else:
        status = 'Listening…'
    return True, text, status


class ThinScrollbar(tk.Canvas):
    """A quiet four-pixel indicator with a larger, focus-free mouse target."""
    def __init__(self, parent, command):
        super().__init__(parent, width=10, highlightthickness=0, borderwidth=0,
                         bg=C['bg'], takefocus=False, cursor='arrow')
        self._command = command
        self._first, self._last, self._hover = 0., 1., False
        self._grab = None
        self.bind('<Configure>', lambda event: self._draw())
        self.bind('<Enter>', lambda event: self._highlight(True))
        self.bind('<Leave>', lambda event: self._highlight(False))
        self.bind('<Button-1>', self._press)
        self.bind('<B1-Motion>', self._drag)
        self.bind('<ButtonRelease-1>', self._release)

    def set(self, first, last):
        self._first, self._last = float(first), float(last)
        self._draw()

    def _thumb(self):
        height = max(1, self.winfo_height())
        size = min(height, max(24., (self._last - self._first) * height))
        travel = height - size
        extent = 1. - (self._last - self._first)
        top = travel * self._first / extent if extent > 0 else 0.
        return top, size, travel, extent

    def _draw(self):
        self.delete('all')
        if self._last - self._first >= .999:
            return
        top, size, _, _ = self._thumb()
        width = 6 if self._hover else 4
        x = self.winfo_width() / 2
        self.create_line(x, top + width / 2, x, top + size - width / 2,
                         width=width, capstyle='round', fill=C['thumb_hover'] if self._hover else C['thumb'])

    def _highlight(self, hover):
        self._hover = hover
        self._draw()

    def _press(self, event):
        top, size, _, _ = self._thumb()
        self._grab = event.y - top if top <= event.y <= top + size else size / 2
        return self._drag(event)

    def _drag(self, event):
        if self._grab is not None:
            _, _, travel, extent = self._thumb()
            fraction = min(1., max(0., (event.y - self._grab) / max(1, travel))) * extent
            self._command('moveto', fraction)
        return 'break'

    def _release(self, event):
        self._grab = None
        return 'break'


class PreviewCard:
    def __init__(self, root, configure_native, *, window=None, on_finish=None, on_cancel=None):
        self.window = window if window is not None else tk.Toplevel(root)
        self.window.withdraw()
        self.window.attributes('-disabled', True)  # Tk can activate its first native wrapper before it shows.
        self.window.title('Listen recording preview')
        self.window.overrideredirect(True)
        self.window.configure(bg=C['bg'])
        self._configure_native = configure_native
        self._native_hook = self._native_previous = self._native_hwnd = None
        self._shown = self.verified = self._destroyed = False
        self._area = self._layout = self._generation = None
        self._text = ''
        self._follow = True
        self._pending_fit = self._fit_id = None
        self._height = 0
        self._footer_height = FOOTER_ROW_HEIGHT
        self._status_layout = None
        self._icon = None
        self._on_cancel = on_cancel
        self._animation_enabled = client_animation_enabled()
        self._animation_id = None
        self._voice_target = self._voice_energy = 0.
        self._voice_time = None
        self._voice_updated = 0.
        self._shadow = CardShadow()
        self._recording_controls = False
        self._header_height = 0
        self._legend_height = 0
        self._legend_width = 0
        self._pressed_action = None

        self.background = tk.Canvas(self.window, bg=C['bg'], highlightthickness=0, borderwidth=0, takefocus=False)
        self.background.pack(fill='both', expand=True)
        self.background.bind('<Configure>', self._draw_border)
        self.legend = tk.Frame(self.background, bg=C['bg'])
        self.meter = tk.Canvas(self.legend, width=84, height=84, bg=C['bg'], highlightthickness=0, takefocus=False)
        self.meter.pack(side='left', padx=(0, 13))
        self.ring = self.meter.create_line(0, 0, 1, 1, fill=C['accent'], width=1.8, smooth=True, splinesteps=12)
        self.bars = [self.meter.create_line(28 + i * 7, 40, 28 + i * 7, 44, width=4,
                                          capstyle='round', fill=C['accent']) for i in range(5)]
        self.titles = tk.Frame(self.legend, bg=C['bg'])
        self.titles.pack(side='left', fill='x', expand=True)
        self.heading = tk.Label(self.titles, text='Listen', bg=C['bg'], fg=C['fg'],
                                font=('Candara', 17, 'bold'), anchor='w')
        self.heading.pack(fill='x')
        self.caption = tk.Label(self.titles, text='Live preview', bg=C['bg'], fg=C['status'],
                                font=('Segoe UI', 10), anchor='w')
        self.caption.pack(fill='x', pady=(4, 0))
        self.body = tk.Frame(self.background, bg=C['bg'])
        self.body.grid_rowconfigure(0, weight=1)
        self.body.grid_columnconfigure(0, weight=1)
        self.text = tk.Text(self.body, wrap='word', font=('Candara', 16), bg=C['bg'], fg=C['fg'],
                            relief='flat', borderwidth=0, highlightthickness=0, padx=1, pady=0, takefocus=False,
                            spacing1=1, spacing3=2, state='disabled', exportselection=False,
                            cursor='arrow', width=1, height=1)
        self.scrollbar = ThinScrollbar(self.body, command=self._scroll)
        self.text.configure(yscrollcommand=self.scrollbar.set)
        self.text.grid(row=0, column=0, sticky='nsew')
        self.scrollbar.grid(row=0, column=1, sticky='ns')
        for widget in (self.text, self.scrollbar):
            widget.bind('<MouseWheel>', self._wheel)
        # Tk's normal Text click explicitly takes focus.
        self.text.bind('<Button-1>', lambda event: 'break')
        self.text.bind('<Button-2>', lambda event: 'break')
        self.text.bind('<Map>', self._schedule_fit)
        self.text.bind('<Configure>', self._schedule_fit)

        self.footer = tk.Frame(self.background, bg=C['bg'])
        self.badge = tk.Label(self.footer, text='Listen', bg=C['bg'], fg=C['status'], font=('Candara', 10, 'bold'))
        self.badge.pack(side='left', padx=(0, 9))
        self.cancel_button = tk.Button(self.footer, text='Cancel', command=lambda: self._act('cancel'),
                                       font=('Segoe UI', 10), bg=C['bg'], fg=C['fg'],
                                       activebackground=C['bg'], activeforeground=C['fg'],
                                       relief='flat', borderwidth=0, highlightthickness=1, highlightbackground=C['border'],
                                       padx=12, pady=6, takefocus=False)
        self.cancel_button.bind('<ButtonPress-1>', lambda event: self._arm_action('cancel'))
        self.status = tk.Label(self.footer, bg=C['bg'], fg=C['status'], font=('Candara', 10),
                               anchor='e', justify='right', padx=2, pady=1, borderwidth=0)
        self.status.pack(side='right', fill='both', expand=True)
        self.window.update_idletasks()

    def _arm_action(self, action):
        self._pressed_action = (action, self._generation) if self._shown and self._recording_controls else None

    def _draw_border(self, _event=None):
        width, height = self.background.winfo_width(), self.background.winfo_height()
        draw_card_border(self.background, width, height)
        self.background.delete('footer-divider')
        if self._text:
            y = height - self._footer_height - 2 * FOOTER_PADDING
            self.background.create_line(1, y, width - 2, y, fill=C['border'], tags='footer-divider')

    def _act(self, action):
        pressed, self._pressed_action = self._pressed_action, None
        if (action == 'cancel' and pressed == (action, self._generation) and self._shown and self._recording_controls
                and self._on_cancel and str(self.cancel_button['state']) != 'disabled'):
            if self._on_cancel(self._generation):
                self.hide()

    def _draw_voice(self, energy, now):
        points = []
        for step in range(49):
            angle = step / 48 * math.tau
            wobble = energy * .75 * (2.3 * math.sin(3 * angle + now * 4.4)
                                     + 1.1 * math.sin(5 * angle - now * 3.2))
            radius = 31 + energy * 1.3 + wobble
            points.extend((42 + math.cos(angle) * radius, 42 + math.sin(angle) * radius))
        self.meter.coords(self.ring, *points)
        for index, bar in enumerate(self.bars):
            half = 2 + energy * (6, 11, 15, 11, 6)[index] * (.85 + .15 * math.sin(now * 12 - index * .8))
            x = 28 + index * 7
            self.meter.coords(bar, x, 42 - half, x, 42 + half)

    def _animate(self):
        self._animation_id = None
        if not (self._animation_enabled and self._shown and self._recording_controls and self.window.winfo_viewable()):
            return
        now = time.monotonic()
        elapsed = min(.1, max(0., now - self._voice_time)) if self._voice_time is not None else .04
        self._voice_time = now
        target = self._voice_target if now - self._voice_updated < .35 else 0.
        response = .055 if target > self._voice_energy else .14
        self._voice_energy += (target - self._voice_energy) * (1 - math.exp(-elapsed / response))
        if self._voice_energy < .001:
            self._voice_energy = 0.
        self._draw_voice(self._voice_energy, now)
        self._animation_id = self.window.after(40, self._animate)

    def _stop_animation(self):
        if self._animation_id is not None:
            try:
                self.window.after_cancel(self._animation_id)
            except tk.TclError:
                pass
            self._animation_id = None
        self._voice_energy = self._voice_target = 0.
        self._voice_time = None
        try:
            self._draw_voice(0., 0.)
        except tk.TclError:
            pass

    def _protect_activation(self):
        if self._native_hook is not None:
            return True
        user = ctypes.WinDLL('user32', use_last_error=True)
        user.GetParent.argtypes = [wintypes.HWND]
        user.GetParent.restype = wintypes.HWND
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user.GetWindowThreadProcessId.restype = wintypes.DWORD
        user.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user.GetWindowLongPtrW.restype = ctypes.c_void_p
        user.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        user.SetWindowLongPtrW.restype = ctypes.c_void_p
        user.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                        wintypes.WPARAM, wintypes.LPARAM]
        user.CallWindowProcW.restype = ctypes.c_ssize_t
        hwnd = user.GetParent(self.window.winfo_id())
        owner = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != os.getpid():
            return False
        previous = user.GetWindowLongPtrW(hwnd, -4)
        if not previous:
            return False
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                           wintypes.WPARAM, wintypes.LPARAM)

        def procedure(handle, message, wparam, lparam):
            if message == 0x21:  # WM_MOUSEACTIVATE: Tk can override the extended style.
                return 3  # MA_NOACTIVATE; still deliver the actual mouse action.
            return user.CallWindowProcW(previous, handle, message, wparam, lparam)

        callback = callback_type(procedure)
        if not user.SetWindowLongPtrW(hwnd, -4, ctypes.cast(callback, ctypes.c_void_p)):
            return False
        self._native_hook, self._native_previous, self._native_hwnd = callback, previous, hwnd
        self._native_user = user
        return True

    def _scroll(self, *args):
        self.text.yview(*args)
        self._follow = self.text.yview()[1] >= .999

    def _wheel(self, event):
        delta = int(-event.delta / 120) if abs(event.delta) >= 120 else (-1 if event.delta > 0 else 1)
        self._scroll('scroll', delta * 3, 'units')
        return 'break'

    def _replace(self, value):
        self.text.configure(state='normal')
        self.text.delete('1.0', 'end')
        self.text.insert('1.0', value)
        self.text.configure(state='disabled')
        self._text = value

    def _schedule_fit(self, _event=None):
        if self._pending_fit is not None and self._fit_id is None:
            self._fit_id = self.window.after_idle(self._fit_text)

    def _fit_text(self):
        self._fit_id = None
        if self._pending_fit is None or not self.text.winfo_viewable() or self.text.winfo_width() <= 1:
            return
        previous, first = self._pending_fit
        self._pending_fit = None
        chrome = 86 + self._footer_height - FOOTER_ROW_HEIGHT + self._header_height
        height = fitted_height(text_pixels(self.text), self._area, previous, chrome=chrome)
        try:
            self._place(self._area, height, True)
        except (OSError, tk.TclError):
            self.hide()
            return
        if self._follow:
            self.text.yview_moveto(1.0)
        elif first is not None:
            self.text.yview(first)

    def _place(self, area, height, has_text):
        width, height, _, _ = card_bounds(area, width=OVERLAY_WIDTH, height=height, centered=True)
        layout = area, width, height, has_text, self._footer_height, self._header_height, self._legend_height, self._legend_width
        if layout == self._layout:
            return
        self.footer.place(x=22, rely=1, y=-(self._footer_height + FOOTER_PADDING),
                          relwidth=1, width=-44, height=self._footer_height)
        if self._header_height:
            self.legend.place(x=22, y=14, width=max(1, width - 44), height=self._legend_height)
        else:
            self.legend.place_forget()
        if has_text:
            self.body.place(x=26, y=12 + self._header_height, relwidth=1, width=-48, relheight=1,
                            height=-(54 + self._footer_height + self._header_height))
        else:
            self.body.place_forget()
        place_card(self.window, 0, area, height=height, width=width, centered=True)
        self._draw_border()
        self._layout, self._height = layout, height
        if self._shown:
            self._shadow.show(self._native_hwnd)

    def _target_icon(self, target):
        image = None
        try:
            if target is not None:
                icon = window_icon(target.hwnd, target.pid, 32, C['bg'])
                if icon is not None:
                    image = ImageTk.PhotoImage(icon, master=self.window)
        except (OSError, AttributeError, tk.TclError):
            pass
        self.badge.configure(image=image or '', text='' if image else 'Listen')
        self._icon = image

    def update(self, snapshot, *, enabled=True, anchor=0, area=None, now=None,
               activity='', generation=None, target=None, levels=(), hands_free_key=''):
        show, text, status = card_content(snapshot, now)
        phase = snapshot.get('phase')
        if phase != 'finishing' and not (phase == 'final' and show) and (not enabled or not show):
            show, text, status = bool(activity), '', activity
        if generation is None:
            generation = snapshot.get('generation')
        try:
            self.verified = self._configure_native(self.window) and self._protect_activation()
            if not self.verified or not show or area is False:
                self.hide()
                return
            area = work_area(anchor) if area is None else area
            new_session = generation != self._generation or area != self._area or not self._shown
            text_changed = text != self._text
            if new_session:
                self._pressed_action = None
                self._generation, self._area, self._follow = generation, area, True
                self._target_icon(target)
                self._voice_energy = 0.
                self._voice_time = None
            recording = phase == 'recording' or activity.startswith(('Starting', 'Listening'))
            listening = recording and (activity.startswith('Listening') or (enabled and not activity.startswith('Starting')))
            self._recording_controls = recording
            self.heading.configure(text='Listening' if listening else 'Starting')
            self.caption.configure(text='Getting ready' if not listening else 'Preview paused' if enabled and snapshot.get('state') == 'paused'
                                   else 'Live preview' if enabled else 'Recording')
            self.cancel_button.configure(state='normal' if recording and self._on_cancel else 'disabled')
            if recording and self._on_cancel:
                self.cancel_button.pack(side='right', padx=(8, 0))
            else:
                self.cancel_button.pack_forget()
            if recording:
                # A newly packed container still reports its previous requested size.
                legend_height = max(self.meter.winfo_reqheight(), self.heading.winfo_reqheight() + self.caption.winfo_reqheight() + 4)
                legend_width = self.meter.winfo_reqwidth() + 13 + max(self.heading.winfo_reqwidth(), self.caption.winfo_reqwidth())
                header_height = max(HEADER_HEIGHT, legend_height + 28)
                status = ''
                self.status.pack_forget()
            else:
                legend_height = legend_width = header_height = 0
                self.status.pack(side='right', fill='both', expand=True)
            header_changed = (header_height != self._header_height or legend_height != self._legend_height
                              or legend_width != self._legend_width)
            self._legend_height, self._legend_width = legend_height, legend_width
            self._header_height = header_height
            width = card_bounds(area, width=OVERLAY_WIDTH)[0]
            # Reserve the outer padding, badge gap, and label padding before wrapping.
            action_width = self.cancel_button.winfo_reqwidth() + 8 if recording and self._on_cancel else 0
            wrap = max(1, width - 59 - self.badge.winfo_reqwidth() - action_width)
            if (status, wrap) != self._status_layout:
                self.status.configure(text=status, wraplength=wrap)
                self._status_layout = status, wrap
            footer_height = max(FOOTER_ROW_HEIGHT, self.status.winfo_reqheight() if not recording else 0, self.badge.winfo_reqheight(),
                                self.cancel_button.winfo_reqheight() if recording and self._on_cancel else 0)
            footer_delta = footer_height - self._footer_height
            self._footer_height = footer_height
            first = self.text.index('@0,0') if (text_changed or footer_delta) and not self._follow else None
            if new_session or footer_delta or header_changed or bool(self._text) != bool(text):
                extra = footer_height - FOOTER_ROW_HEIGHT
                previous = self._height + footer_delta if self._text and not new_session else 0
                height = fitted_height(0, area, previous, chrome=86 + extra + header_height) if text else FOOTER_HEIGHT + extra + header_height
                self._place(area, height, bool(text))
            if new_session or text_changed:
                self._replace(text)
                if not self._shown:
                    # On Windows, deiconify raises the window and can take focus.
                    self.window.state('normal')
                    self.window.update_idletasks()
                    self.window.attributes('-disabled', False)
                    self._shown = True
            if text and (new_session or text_changed or footer_delta):
                previous = 0 if snapshot.get('phase') == 'final' else self._height
                self._pending_fit = previous, first
                self._schedule_fit()
            elif not text:
                self._pending_fit = None
            self._voice_target = voice_level(levels) if listening else 0.
            self._voice_updated = time.monotonic()
            if recording and self._animation_enabled:
                if self._animation_id is None:
                    self._animate()
            else:
                self._stop_animation()
            if self._shown:
                self._shadow.show(self._native_hwnd)
        except (OSError, tk.TclError):
            self.hide()

    def hide(self):
        self._shadow.hide()
        self._stop_animation()
        self._pressed_action = None
        self._pending_fit = None
        if self._shown:
            self.window.attributes('-disabled', True)
            self.window.withdraw()
            self._shown = False
        if self._text:
            self._replace('')
        if self._icon is not None:
            self._target_icon(None)

    def destroy(self):
        self._shadow.destroy()
        if self._destroyed:
            return
        self._destroyed = True
        self._stop_animation()
        if self._fit_id is not None:
            self.window.after_cancel(self._fit_id)
            self._fit_id = None
        if self._native_hook is not None:
            current = self._native_user.GetWindowLongPtrW(self._native_hwnd, -4)
            if current == ctypes.cast(self._native_hook, ctypes.c_void_p).value:
                self._native_user.SetWindowLongPtrW(self._native_hwnd, -4, self._native_previous)
        try:
            self.window.destroy()
        except tk.TclError:
            pass
        self._native_hook = self._native_previous = self._native_hwnd = None
        self._icon = None
        self._configure_native = None
