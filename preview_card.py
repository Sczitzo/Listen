"""One focus-preserving, session-only dictation overlay."""
import ctypes
from ctypes import wintypes
import os
import time
import tkinter as tk

from PIL import ImageTk
from target_icon import window_icon

C = dict(bg='#2d2925', border='#a49380', fg='#f1e9dd', status='#c8bdaf',
         thumb_hover='#dcc09a', thumb='#a49380', accent='#dcc09a', on_accent='#282117')
OVERLAY_WIDTH = 500
HEADER_HEIGHT = 44
FOOTER_ROW_HEIGHT = 28
FOOTER_PADDING = 12
FOOTER_HEIGHT = FOOTER_ROW_HEIGHT + 2 * FOOTER_PADDING


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
    radius = min(26, width, height)
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
        self._on_finish, self._on_cancel = on_finish, on_cancel
        self._recording_controls = False
        self._header_height = 0
        self._pressed_action = None

        self.background = tk.Frame(self.window, bg=C['bg'], highlightthickness=1, highlightbackground=C['border'])
        self.background.pack(fill='both', expand=True)
        self.header = tk.Frame(self.background, bg=C['bg'])
        self.meter = tk.Canvas(self.header, width=38, height=28, bg=C['bg'], highlightthickness=0, takefocus=False)
        self.meter.pack(side='left', padx=(0, 10))
        self.bars = [self.meter.create_line(5 + i * 7, 14, 5 + i * 7, 14, width=4,
                                          capstyle='round', fill=C['accent']) for i in range(5)]
        self.heading = tk.Label(self.header, text='Listen', bg=C['bg'], fg=C['fg'],
                                font=('Candara', 12, 'bold'), anchor='w')
        self.heading.pack(side='left', fill='x', expand=True)
        self.finish_button = tk.Button(self.header, text='Finish', command=lambda: self._act('finish'),
                                       font=('Segoe UI', 10, 'bold'), bg=C['accent'], fg=C['on_accent'],
                                       activebackground=C['accent'], activeforeground=C['on_accent'],
                                       relief='flat', borderwidth=0, padx=16, pady=4, takefocus=False)
        self.finish_button.bind('<ButtonPress-1>', lambda event: self._arm_action('finish'))
        self.body = tk.Frame(self.background, bg=C['bg'])
        self.body.grid_rowconfigure(0, weight=1)
        self.body.grid_columnconfigure(0, weight=1)
        self.text = tk.Text(self.body, wrap='word', font=('Candara', 12), bg=C['bg'], fg=C['fg'],
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
                                       font=('Segoe UI', 10), bg=C['bg'], fg=C['status'],
                                       activebackground=C['bg'], activeforeground=C['fg'],
                                       relief='flat', borderwidth=0, padx=10, pady=4, takefocus=False)
        self.cancel_button.bind('<ButtonPress-1>', lambda event: self._arm_action('cancel'))
        self.status = tk.Label(self.footer, bg=C['bg'], fg=C['status'], font=('Candara', 10),
                               anchor='e', justify='right', padx=2, pady=1, borderwidth=0)
        self.status.pack(side='right', fill='both', expand=True)
        self.window.update_idletasks()

    def _arm_action(self, action):
        self._pressed_action = (action, self._generation) if self._shown and self._recording_controls else None

    def _act(self, action):
        pressed, self._pressed_action = self._pressed_action, None
        callback = self._on_finish if action == 'finish' else self._on_cancel
        button = self.finish_button if action == 'finish' else self.cancel_button
        if (pressed == (action, self._generation) and self._shown and self._recording_controls
                and callback and str(button['state']) != 'disabled'):
            if callback(self._generation):
                self.hide()

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
        chrome = 62 + self._footer_height - FOOTER_ROW_HEIGHT + self._header_height
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
        layout = area, width, height, has_text, self._footer_height, self._header_height
        if layout == self._layout:
            return
        self.footer.place(x=14, rely=1, y=-(self._footer_height + FOOTER_PADDING),
                          relwidth=1, width=-28, height=self._footer_height)
        if self._header_height:
            self.header.place(x=14, y=8, relwidth=1, width=-28, height=self._header_height - 8)
        else:
            self.header.place_forget()
        if has_text:
            self.body.place(x=15, y=12 + self._header_height, relwidth=1, width=-30, relheight=1,
                            height=-(30 + self._footer_height + self._header_height))
        else:
            self.body.place_forget()
        place_card(self.window, 0, area, height=height, width=width, centered=True)
        self._layout, self._height = layout, height

    def _target_icon(self, target):
        image = None
        try:
            if target is not None:
                icon = window_icon(target.hwnd, target.pid, 23, C['bg'])
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
        if hands_free_key:
            status = f'Listening · Tap {hands_free_key} to finish · Esc to cancel'
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
            recording = phase == 'recording' or activity.startswith(('Starting', 'Listening'))
            listening = recording and (activity.startswith('Listening') or (enabled and not activity.startswith('Starting')))
            self._recording_controls = recording
            self.heading.configure(text='Listening' if listening else 'Starting')
            self.finish_button.configure(state='normal' if listening and self._on_finish else 'disabled')
            self.cancel_button.configure(state='normal' if recording and self._on_cancel else 'disabled')
            if recording and self._on_finish:
                self.finish_button.pack(side='right', padx=(10, 0))
            else:
                self.finish_button.pack_forget()
            if recording and self._on_cancel:
                self.cancel_button.pack(side='right', before=self.status, padx=(8, 0))
            else:
                self.cancel_button.pack_forget()
            header_height = max(HEADER_HEIGHT, self.header.winfo_reqheight() + 8) if recording else 0
            header_changed = header_height != self._header_height
            self._header_height = header_height
            width = card_bounds(area, width=OVERLAY_WIDTH)[0]
            # Reserve the outer padding, badge gap, and label padding before wrapping.
            action_width = self.cancel_button.winfo_reqwidth() + 8 if recording and self._on_cancel else 0
            wrap = max(1, width - 43 - self.badge.winfo_reqwidth() - action_width)
            if (status, wrap) != self._status_layout:
                self.status.configure(text=status, wraplength=wrap)
                self._status_layout = status, wrap
            footer_height = max(FOOTER_ROW_HEIGHT, self.status.winfo_reqheight(), self.badge.winfo_reqheight(),
                                self.cancel_button.winfo_reqheight() if recording and self._on_cancel else 0)
            footer_delta = footer_height - self._footer_height
            self._footer_height = footer_height
            first = self.text.index('@0,0') if (text_changed or footer_delta) and not self._follow else None
            if new_session or footer_delta or header_changed or bool(self._text) != bool(text):
                extra = footer_height - FOOTER_ROW_HEIGHT
                previous = self._height + footer_delta if self._text and not new_session else 0
                height = fitted_height(0, area, previous, chrome=62 + extra + header_height) if text else FOOTER_HEIGHT + extra + header_height
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
            for index, bar in enumerate(self.bars):
                half = (4, 8, 11, 8, 4)[index] if listening else (2, 2, 4, 2, 2)[index]
                x = 5 + index * 7
                self.meter.coords(bar, x, 14 - half, x, 14 + half)
        except (OSError, tk.TclError):
            self.hide()

    def hide(self):
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
        if self._destroyed:
            return
        self._destroyed = True
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
