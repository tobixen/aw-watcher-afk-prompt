import json
import logging
import re
import time
import tkinter as tk
from collections import UserDict
from collections.abc import Callable
from itertools import chain
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

import appdirs

from aw_watcher_afk_prompt.widgets import EnhancedEntry

logger = logging.getLogger(__name__)

# How often the ongoing dialog polls the afk watcher to notice the user returned.
_AFK_POLL_INTERVAL_MS = 5_000

# How often an open dialog asks the caller for fresh facts (see the ``refresh``
# argument of AWAfkPromptDialog). Both things it displays go stale while it sits
# unanswered: the "ended N minutes ago" age, and the "(N of M)" queue count when
# further AFK periods pile up behind it.
_REFRESH_INTERVAL_MS = 60_000

# Trailing marker on the ongoing prompt, dropped once the user is back.
_STILL_AFK_RE = re.compile(r"\s*\(still AFK\)\s*$")

# How long to wait between attempts at reaching the display server, and for how
# long in total, before the process gives up and lets its supervisor restart it.
DISPLAY_RETRY_INTERVAL = 5.0
DISPLAY_WAIT_MINUTES = 15.0

# The hidden Tk root the dialogs in this module are parented to. Created on demand
# rather than at import time: this watcher is typically started by systemd
# alongside the graphical session, so the display server is regularly a few
# seconds — or a compositor crash — away, and dying inside `import` gives no chance
# to log why (see wait_for_display).
#
# Not the only root in the process, as much as it should be: ask_split_activities()
# is called without a parent (see ask_string) and makes one of its own.
_root: tk.Tk | None = None


def get_root() -> tk.Tk:
    """The hidden Tk root, created on first use.

    Raises tk.TclError when there is no display server to connect to; callers
    that can afford to wait for one should use wait_for_display first.
    """
    global _root
    if _root is None:
        _root = tk.Tk()
        _root.withdraw()
    return _root


def wait_for_display(
    timeout_minutes: float = DISPLAY_WAIT_MINUTES,
    interval: float = DISPLAY_RETRY_INTERVAL,
    sleep: Callable[[float], None] = time.sleep,
) -> tk.Tk:
    """Create the hidden Tk root, waiting for the display server to turn up.

    Retrying in-process rather than exiting matters for a --user service started
    with the session: a watcher that dies on a missing display restarts every
    RestartSec, and systemd's start-limit eventually stops restarting it at all —
    leaving no watcher and no prompts (which is precisely how the Wayland window
    watcher, and with it the whole AFK feed, ended up dead after a reboot).

    Gives up after timeout_minutes rather than waiting forever, so a genuinely
    display-less machine leaves a reason in the log and an exit code behind
    instead of holding a service "active" that can never prompt. The supervisor
    is still free to start it again — the bundled unit deliberately lets it.
    """
    attempts = max(1, int(timeout_minutes * 60 / interval))
    for attempt in range(1, attempts + 1):
        try:
            return get_root()
        except tk.TclError as e:
            if attempt == attempts:
                logger.error(f"No display server after {timeout_minutes:.0f} minutes, giving up: {e}")
                raise
            # Once at WARNING so the reason is on the record, then quietly: this
            # can be hundreds of attempts while a compositor starts up.
            log = logger.warning if attempt == 1 else logger.debug
            log(f"No display server yet ({e}), retrying every {interval:.0f}s for {timeout_minutes:.0f} minutes")
            sleep(interval)
    raise AssertionError("unreachable")  # pragma: no cover


def __getattr__(name: str):
    """Keep the historical module-level ``dialog.root`` working, lazily."""
    if name == "root":
        return get_root()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _queue_text(queue_info: dict | None) -> str:
    """Render the "(2 of 5) — next: …" line; empty string when there is no queue."""
    if not queue_info:
        return ""
    pos = queue_info["position"]
    total = queue_info["total"]
    next_str = queue_info.get("next_str")
    if next_str:
        return f"({pos} of {total}) — next: {next_str}"
    return f"({pos} of {total}) — last interval"


def open_link(link: str) -> None:
    import webbrowser

    webbrowser.open(link)


def tk_font_system() -> str | None:
    """Which font backend this Tk build uses: "xft", "x11", or None if unknown.

    Only X11 builds answer this at all. "x11" means the build was compiled
    *without* Xft: it falls back to core X bitmap fonts and to the legacy
    keyboard-input path, so dialogs render in "fixed" and non-ASCII characters
    (æøå) cannot be typed into them. The interpreters uv downloads
    (python-build-standalone) ship such a Tk; distro Pythons do not.
    """
    try:
        return get_root().tk.call("::tk::pkgconfig", "get", "fontsystem")
    except tk.TclError:
        return None


def warn_on_degraded_tk() -> str | None:
    """Log a warning when the Tk build cannot render/accept non-ASCII text.

    Returns the font system so callers (and tests) can see what was found.
    """
    font_system = tk_font_system()
    if font_system == "x11":
        import sys

        logger.warning(
            "This Tk build has no Xft support (fontsystem=x11): dialogs use the bitmap "
            "'fixed' font and non-ASCII characters cannot be typed into them. The "
            f"interpreter in use is {sys.executable} — reinstall with 'make install', "
            "which builds against a system Python (see README: Installation)."
        )
    return font_system


class _AbbreviationStore(UserDict[str, str]):
    """A class to store abbreviations and their expansions.

    And to manage saving this information to the config directory.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(self, *args, **kwargs)
        config_dir = Path(appdirs.user_config_dir("aw-watcher-afk-prompt"))
        config_dir.mkdir(parents=True, exist_ok=True)
        self._config_file = config_dir / "abbreviations.json"
        self._load_from_config()

    def _load_from_config(self) -> None:
        if self._config_file.exists():
            with self._config_file.open() as f:
                try:
                    self.update(json.load(f))
                except json.JSONDecodeError:
                    logger.exception("Failed to load abbreviations from config file.")

    def _save_to_config(self) -> None:
        with self._config_file.open("w") as f:
            json.dump(self.data, f, indent=4)

    def __setitem__(self, key: str, value: str) -> None:
        self.data[key] = value
        self._save_to_config()

    def __delitem__(self, key: str) -> None:
        super().__delitem__(key)
        self._save_to_config()


class ConfigDialog(simpledialog.Dialog):
    def __init__(self, master):
        super().__init__(master, "Configuration")

    def body(self, master):
        master = ttk.Frame(master)
        master.grid()
        notebook = ttk.Notebook(master)
        notebook.grid(row=1, column=0)

        # Setup abbreviations as a tab
        abbr_tab = ttk.Frame(notebook)
        notebook.add(abbr_tab, text="Abbreviations")
        self.abbr_pane = AbbreviationPane(abbr_tab)
        self.abbr_pane.grid()


class AddAbbreviationDialog(simpledialog.Dialog):
    def __init__(self, master, expansion: str | None = None):
        self.expansion_value = expansion
        super().__init__(master, "Add Abbreviation")

    def body(self, master):
        master = ttk.Frame(master)
        master.grid()

        ttk.Label(master, text="Abbreviation").grid(row=0, column=0)
        ttk.Label(master, text="Expansion").grid(row=1, column=0)

        self.abbr = ttk.Entry(master)
        self.abbr.grid(row=0, column=1)
        self.expansion = ttk.Entry(master)
        if self.expansion_value:
            self.expansion.insert(0, self.expansion_value)
        self.expansion.grid(row=1, column=1)
        return self.abbr

    def apply(self):
        self.result = (self.abbr.get(), self.expansion.get())


# TODO: Link the abbreviations json file for editing directly.
class AbbreviationPane(ttk.Frame):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Set up a canvas so we can get a scroll bar.
        # TODO: Think of a better way to display these abbreviations?
        self.canvas = tk.Canvas(self, borderwidth=0, background="#ffffff")
        self.canvas.grid(row=0, column=0, sticky=tk.N + tk.S + tk.E + tk.W)
        self.scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)
        self.scrollbar.grid(row=0, column=1, sticky=tk.N + tk.S)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))
        # enable scroll on a track pad
        self.canvas.bind_all("<Button-4>", lambda _: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind_all("<Button-5>", lambda _: self.canvas.yview_scroll(1, "units"))

        self.frame = ttk.Frame(self.canvas)
        self.frame.grid(row=0, column=0, sticky=tk.N + tk.S + tk.E + tk.W)

        self.canvas.create_window((0, 0), window=self.frame, anchor="nw")

        ttk.Label(self.frame, text="Abbr", justify=tk.LEFT).grid(row=0, column=0, sticky=tk.W)
        ttk.Label(self.frame, text="Expansion", justify=tk.LEFT).grid(row=0, column=1, sticky=tk.W)

        self.new_abbr = ttk.Entry(self.frame)
        self.new_abbr.grid(row=1, column=0)
        self.new_expansion = ttk.Entry(self.frame)
        self.new_expansion.grid(row=1, column=1)
        ttk.Button(self.frame, text="+", command=self.add_abbreviation).grid(row=1, column=2)

        self.other_rows = []

        self.draw_abbreviations()

    def _make_del_function(self, key):
        def del_function():
            abbreviations.pop(key)
            self.draw_abbreviations()

        return del_function

    def draw_abbreviations(self):
        for child in chain(*self.other_rows):
            child.destroy()
        self.other_rows = []

        for i, (abbr_key, abbr_value) in enumerate(sorted(abbreviations.items())):
            row_index = i + 2
            # TODO: Allow _editing_ abbreviations in place instead of remove and re-add.
            # Maybe by using a readonly entry and double clicking to activate it?
            abbr = ttk.Label(self.frame, text=abbr_key, justify=tk.LEFT)
            abbr.grid(row=row_index, column=0, sticky=tk.W)
            expansion = ttk.Label(self.frame, text=abbr_value, justify=tk.LEFT)
            expansion.grid(row=row_index, column=1, sticky=tk.W)
            button = ttk.Button(self.frame, text="-", command=self._make_del_function(abbr_key))
            button.grid(row=row_index, column=2)
            self.other_rows.append((abbr, expansion, button))

    def add_abbreviation(self):
        abbr = self.new_abbr.get()
        expansion = self.new_expansion.get()
        if not abbr or not expansion:
            return
        abbreviations[abbr] = expansion
        self.new_abbr.delete(0, tk.END)
        self.new_expansion.delete(0, tk.END)
        self.draw_abbreviations()


# Singleton
abbreviations = _AbbreviationStore()


# TODO: This widget pops up off-center when using multiple screes on Linux, possibly other platforms.
# See https://stackoverflow.com/questions/30312875/tkinter-winfo-screenwidth-when-used-with-dual-monitors/57866046#57866046
class AWAfkPromptDialog(simpledialog.Dialog):
    def __init__(
        self,
        title: str,
        prompt: str,
        history: list[str],
        afk_start=None,
        afk_duration_seconds=None,
        queue_info: dict | None = None,
        is_ongoing: bool = False,
        still_afk_check=None,
        refresh=None,
        timeout_ms: int | None = None,
    ) -> None:
        self.prompt = prompt
        self.history = history
        self.history_index = len(history)
        self.afk_start = afk_start
        self.afk_duration_seconds = afk_duration_seconds
        self.split_mode = False  # Track if user wants split mode
        self.queue_info = queue_info  # dict with 'position', 'total', 'next_str' keys
        self.is_ongoing = is_ongoing
        # Callable returning True while the user is still AFK. Polled so the dialog
        # can notice the user returned (via the OS afk watcher) without them typing.
        self.still_afk_check = still_afk_check
        # Callable returning a dict of facts that may have changed while the dialog
        # waited: {"prompt": str, "queue_info": dict | None}. A missing key means
        # "unchanged" (e.g. a failed recount), so it can report partial updates.
        self.refresh = refresh
        # Auto-snooze an unanswered dialog after this many ms (None/0 = never).
        self.timeout_ms = timeout_ms
        self._returned = False
        self._live_timer = None
        self._afk_poll_timer = None
        self._afk_poll_interval_ms = _AFK_POLL_INTERVAL_MS
        self._refresh_timer = None
        self._timeout_timer = None
        super().__init__(get_root(), title)

    # @override (when we get to 3.12)
    def body(self, master):
        # Make the whole body a ttk fram as recommended by the tkdocs.com guy.
        # It should help the formatting be more consistent with the ttk children widgets.
        master = ttk.Frame(master)
        master.grid()

        # Prompt
        # Copied from the simpledialog source code.
        w = ttk.Label(master, text=self.prompt, justify=tk.LEFT)
        w.grid(row=0, padx=5, sticky=tk.W)
        self._prompt_label = w

        # Input field (EnhancedEntry provides Ctrl+Backspace and Ctrl+w shortcuts)
        self.entry = EnhancedEntry(master, name="entry", width=40)
        self.entry.grid(row=1, padx=5, sticky=tk.W + tk.E)

        # README link
        doc_label = ttk.Label(master, text="Documentation", foreground="blue", cursor="hand2", justify=tk.RIGHT)
        doc_label.grid(row=0, padx=5, sticky=tk.W, column=1)
        doc_label.bind("<Button-1>", self.open_readme)

        # Issue link
        issue_label = ttk.Label(master, text="Report an issue", foreground="blue", cursor="hand2", justify=tk.RIGHT)
        issue_label.grid(row=1, padx=5, sticky=tk.W, column=1)
        issue_label.bind("<Button-1>", self.open_an_issue)

        # Quick dismiss as UNKNOWN (Ctrl-U)
        self.bind("<Control-u>", self.submit_unknown)

        # Open web interface shortcut
        self.bind("<Control-o>", self.open_web_interface)

        # History navigation shotcuts
        self.bind("<Up>", self.previous_entry)
        self.bind("<Down>", self.next_entry)
        self.bind("<Control-j>", self.next_entry)
        self.bind("<Control-k>", self.previous_entry)

        # Expand abbreviations the user types
        self.entry.bind("<KeyRelease>", self.expand_abbreviations)

        # Add a new abbreviation from a highlighted section of text.
        self.entry.bind("<Control-n>", self.save_new_abbreviation)
        self.entry.bind("<Control-N>", lambda e: self.save_new_abbreviation(e, long=True))

        self.bind("<Control-comma>", self.open_config)

        # Live duration label for ongoing AFK periods (updates every 10s)
        if self.is_ongoing and self.afk_start is not None:
            self._duration_var = tk.StringVar(value=self._make_duration_text())
            duration_label = ttk.Label(master, textvariable=self._duration_var, justify=tk.LEFT)
            duration_label.grid(row=2, padx=5, sticky=tk.W, columnspan=2)
            self._live_timer = self.after(10_000, self._tick_duration)
            # Once the user types, they're back: the period is assumed over, so
            # stop the live counter and drop the "still AFK" wording.
            self.entry.bind("<KeyPress>", self._mark_returned, add="+")

        # Notice when the OS afk watcher reports the user is active again, even if
        # they haven't touched the dialog yet. An ongoing dialog needs this promptly
        # (it freezes its counter); any other dialog only needs it to know there is
        # finally someone there to see it, so it polls at the slower cadence.
        if self.still_afk_check is not None:
            self._afk_poll_interval_ms = _AFK_POLL_INTERVAL_MS if self.is_ongoing else _REFRESH_INTERVAL_MS
            self._afk_poll_timer = self.after(self._afk_poll_interval_ms, self._poll_afk)

        # Queue info label: shown when multiple AFK intervals are pending. Always
        # built (but only gridded while non-empty) so a queue that appears or grows
        # while the dialog waits can be shown without rebuilding the body.
        self._queue_var = tk.StringVar(value=_queue_text(self.queue_info))
        self._queue_label = ttk.Label(master, textvariable=self._queue_var, foreground="gray", justify=tk.LEFT)
        self._grid_queue_label()

        # Keep an unanswered dialog honest: re-read the age and the queue count from
        # the caller once a minute, so it can't keep claiming the period ended "2
        # minutes ago" an hour later, or that it is the only interval waiting.
        if self.refresh is not None:
            self._refresh_timer = self.after(_REFRESH_INTERVAL_MS, self._refresh_tick)

        # Auto-snooze if ignored: a dialog buried under other windows would
        # otherwise be lost and forgotten. Snoozing hides it for a few minutes and
        # the main loop re-raises it, re-scanned and up to date. Typing restarts the
        # countdown so it can't self-destruct mid-sentence.
        if self.timeout_ms:
            # Don't burn the fuse while nobody is at the keyboard: the user hasn't had
            # a chance to see the dialog, and it would spend half its life hidden —
            # quite possibly at the moment they sit down. This covers the ongoing
            # dialog and the completed periods prompted while the user is still away.
            # _mark_returned arms the countdown once they are back.
            if not self.is_ongoing and not self._user_is_away():
                self._arm_timeout()
            self.bind("<KeyPress>", self._arm_timeout, add="+")

        return self.entry

    def _grid_queue_label(self) -> None:
        """Show the queue line only when there is something to say."""
        if self._queue_var.get():
            self._queue_label.grid(row=3, padx=5, sticky=tk.W, columnspan=2)
        else:
            self._queue_label.grid_remove()

    def _cancel_timers(self, *names: str) -> None:
        """Cancel the named ``after`` callbacks if armed. Idempotent."""
        for name in names:
            timer = getattr(self, name, None)
            if timer is not None:
                self.after_cancel(timer)
                setattr(self, name, None)

    def _cancel_all_timers(self) -> None:
        self._cancel_timers("_live_timer", "_afk_poll_timer", "_refresh_timer", "_timeout_timer")

    def _set_prompt_text(self, text: str) -> None:
        """Update the prompt label, keeping the "user is back" wording consistent."""
        if self._returned:
            text = _STILL_AFK_RE.sub("", text)
        self._prompt_label.configure(text=text)

    def _refresh_tick(self) -> None:
        self._refresh_now()
        self._refresh_timer = self.after(_REFRESH_INTERVAL_MS, self._refresh_tick)

    def _refresh_now(self) -> None:
        """Pull the current prompt text / queue count from the ``refresh`` callback.

        Keys the callback leaves out are left as they are — a recount that failed
        against the server must not blank the queue line or invent a number.
        """
        try:
            update = self.refresh() or {}
        except Exception:
            logger.exception("Dialog refresh failed; keeping the text as it is")
            return
        if update.get("prompt"):
            self._set_prompt_text(update["prompt"])
        if "queue_info" in update:
            self.queue_info = update["queue_info"]
            self._queue_var.set(_queue_text(self.queue_info))
            self._grid_queue_label()

    def _arm_timeout(self, event=None) -> None:  # noqa: ARG002
        """(Re)start the ignored-for-too-long countdown."""
        if not self.timeout_ms:
            return
        self._cancel_timers("_timeout_timer")
        self._timeout_timer = self.after(self.timeout_ms, self._auto_snooze)

    def _auto_snooze(self) -> None:
        """Nobody answered in time: close as a snooze so the main loop re-asks."""
        self._timeout_timer = None
        logger.info(f"Dialog unanswered for {self.timeout_ms // 1000}s — auto-snoozing; it will be re-raised later")
        self.cancel_with_snooze()

    def _make_duration_text(self) -> str:
        from datetime import UTC, datetime

        from aw_watcher_afk_prompt.utils import format_duration

        elapsed = datetime.now(UTC) - self.afk_start
        return f"Time away so far: {format_duration(elapsed)} (updating...)"

    def _user_is_away(self) -> bool:
        """Does the OS afk watcher still report the user as AFK?

        False when there is nothing to ask. An error counts as "away": we would
        rather keep the dialog on screen than hide one nobody has seen.
        """
        if self.still_afk_check is None:
            return False
        try:
            return bool(self.still_afk_check())
        except Exception:
            logger.exception("still_afk_check failed; assuming the user is still away")
            return True

    def _poll_afk(self) -> None:
        """Watch for the user coming back.

        When they do, freeze the dialog (and start the unanswered countdown) even
        if they haven't typed into it yet.
        """
        if not self._user_is_away():
            self._mark_returned()
            return
        self._afk_poll_timer = self.after(self._afk_poll_interval_ms, self._poll_afk)

    def _mark_returned(self, event=None) -> None:  # noqa: ARG002
        """The user is back (they typed, or the afk watcher reports activity):
        freeze the duration and drop the 'still AFK' wording so the dialog no
        longer claims the period is ongoing."""
        if self._returned:
            return
        self._returned = True
        self._cancel_timers("_live_timer", "_afk_poll_timer")
        # Freeze the live duration label (no more "updating...").
        if hasattr(self, "_duration_var") and self.afk_start is not None:
            from datetime import UTC, datetime

            from aw_watcher_afk_prompt.utils import format_duration

            elapsed = datetime.now(UTC) - self.afk_start
            self._duration_var.set(f"Time away: {format_duration(elapsed)}")
        # Drop the trailing "(still AFK)" marker from the prompt label.
        if hasattr(self, "_prompt_label"):
            self._set_prompt_text(self._prompt_label.cget("text"))
        # The countdown was held back while the user was away; now that they are
        # back, start it so the dialog doesn't sit forever if they wander off.
        self._arm_timeout()

    def _tick_duration(self) -> None:
        if hasattr(self, "_duration_var"):
            self._duration_var.set(self._make_duration_text())
            from datetime import UTC, datetime

            elapsed_s = (datetime.now(UTC) - self.afk_start).total_seconds()
            # Wake up just after the display value changes: every minute below 24 h,
            # every hour above (format_duration switches to "X days Y hours" there).
            if elapsed_s < 24 * 3600:
                interval_s = 60 - (elapsed_s % 60)
            else:
                interval_s = 3600 - (elapsed_s % 3600)
            ms = int(interval_s * 1000) + 200  # 200 ms buffer so we don't fire slightly early
            self._live_timer = self.after(ms, self._tick_duration)

    def save_new_abbreviation(self, event=None, *, long: bool = False):  # noqa: ARG002
        if self.entry.selection_present():
            # Get the highlighted Text
            initial_expansion = self.entry.selection_get().strip()
        elif long:
            # Get all the text before the cursor
            cursor_index = self.entry.index(tk.INSERT)
            initial_expansion = self.entry.get()[:cursor_index].strip()
        else:
            # Get the word under or before the cursor
            cursor_index = self.entry.index(tk.INSERT)
            words = re.split(r"(\W+)", self.entry.get())
            char_count = 0
            initial_expansion = ""
            for word in words:
                char_count += len(word)
                if re.fullmatch(r"\w+", word):
                    initial_expansion = word
                if char_count >= cursor_index:
                    break

        # Prompt for the abbreviation
        result = AddAbbreviationDialog(self, initial_expansion).result

        if result:
            abbr, expansion = result
            abbr = abbr.strip()
            expansion = expansion.strip()
            if not re.fullmatch(r"\w+", abbr):
                messagebox.showerror("Invalid abbreviation", "Abbreviations must be alphanumeric and without spaces.")
                return

            if existing := abbreviations.get(abbr):
                if not messagebox.askyesno(
                    "Overwrite confirmation",
                    f"That abbreviation ({abbr}) already exists as '{existing}', would you like to over write?",
                ):
                    return
            abbreviations[abbr] = expansion

        # Refocus on the main text entry
        self.entry.focus_set()

    def expand_abbreviations(self, event=None):  # noqa: ARG002
        text = self.entry.get()
        cursor_index = self.entry.index(tk.INSERT)

        # Get the potential appreviation
        abbr_regex = r"(['\w]+)\s$"  # Include ' so if you has s as an abbreviation "what's" doesn't expand to what is.
        abbr = re.search(abbr_regex, text[:cursor_index])
        if abbr and abbr.group(1) in abbreviations:
            before_index = len(re.sub(abbr_regex, "", text[:cursor_index]))
            self.entry.delete(before_index, cursor_index - 1)
            self.entry.insert(before_index, abbreviations[abbr.group(1)])

    def set_text(self, text: str):
        self.entry.set_text(text)

    def previous_entry(self, event=None):  # noqa: ARG002
        if not self.history:
            return
        self.history_index = max(0, self.history_index - 1)
        self.set_text(self.history[self.history_index])

    def next_entry(self, event=None):  # noqa: ARG002
        if not self.history:
            return
        self.history_index = min(len(self.history) - 1, self.history_index + 1)
        self.set_text(self.history[self.history_index])

    def open_an_issue(self, event=None):  # noqa: ARG002
        open_link("https://github.com/tobixen/aw-watcher-afk-prompt/issues/new")

    def open_readme(self, event=None):  # noqa: ARG002
        open_link("https://github.com/tobixen/aw-watcher-afk-prompt#aw-watcher-afk-prompt")

    def open_web_interface(self, event=None):  # noqa: ARG002
        open_link("http://localhost:5600/#/timeline")

    def remove_to_start(self, event=None):  # noqa: ARG002
        """Remove text from cursor to start of line (Ctrl-Shift-U can be used instead)."""
        cursor = self.entry.index(tk.INSERT)
        self.entry.delete(0, cursor)
        self.entry.insert(0, "")

    def validate(self):
        if not self.entry.get().strip():
            # Empty Enter = snooze (same as the Snooze button)
            self.after(0, self.cancel_with_snooze)
            return False
        return True

    def apply(self):
        self.result = self.entry.get().strip()

    def submit_unknown(self, event=None):  # noqa: ARG002
        """Quick dismiss as UNKNOWN."""
        self.result = "UNKNOWN"
        self.cancel()

    def open_config(self, event=None):  # noqa: ARG002
        ConfigDialog(self)

    def cancel(self, event=None):  # noqa: ARG002
        # Call withdraw first because it is faster.
        # The process should wait on the destroy instead of the human.
        self._cancel_all_timers()
        self.withdraw()
        self.destroy()

    def cancel_with_snooze(self, event=None):  # noqa: ARG002
        """Snooze: close the dialog without an answer.

        The result stays None, which the main loop interprets as "leave me
        alone for a while" — it suppresses further prompts for a few minutes
        (without blocking the watcher) and re-asks later.
        """
        if getattr(self, "_snoozing", False):
            return
        self._snoozing = True
        self.cancel()

    def switch_to_split_mode(self):
        """Switch to split mode (close this dialog and open split dialog)."""
        self.split_mode = True
        # For an ongoing period the end time is unknown; assume it ends now so
        # the split dialog has a concrete total duration to distribute.
        if self.afk_duration_seconds is None and self.afk_start is not None:
            from datetime import UTC, datetime

            self.afk_duration_seconds = (datetime.now(UTC) - self.afk_start).total_seconds()
        self._cancel_all_timers()
        self.destroy()

    # @override (when we get to 3.12)
    def buttonbox(self):
        """The buttons at the bottom of the dialog.

        This is overridden to add Split, Unknown, and Settings buttons.
        """
        box = ttk.Frame(self)

        w = ttk.Button(box, text="OK", width=10, command=self.ok, default=tk.ACTIVE)
        w.pack(side=tk.LEFT, padx=5, pady=5)
        # "Snooze" (formerly "Cancel"): close without answering, re-ask later.
        # Escape and an empty Enter do the same.
        w = ttk.Button(box, text="Snooze", width=10, command=self.cancel_with_snooze)
        w.pack(side=tk.LEFT, padx=5, pady=5)

        # Unknown button - quick dismiss for forgotten activities (Ctrl-U)
        w = ttk.Button(box, text="Unknown", width=10, command=self.submit_unknown)
        w.pack(side=tk.LEFT, padx=5, pady=5)

        # Split button: shown when we know the start. For a still-ongoing period
        # the end is unknown, but clicking Split assumes the period ends now, so
        # switch_to_split_mode snapshots the duration as start..now.
        if self.afk_start is not None and (self.afk_duration_seconds is not None or self.is_ongoing):
            w = ttk.Button(box, text="Split", width=10, command=self.switch_to_split_mode)
            w.pack(side=tk.LEFT, padx=5, pady=5)

        # TODO: Figure out a quick easy way to pick how long to snooze for.
        w = ttk.Button(box, text="Settings", command=self.open_config)
        w.pack(side=tk.LEFT, padx=5, pady=5)

        self.bind("<Return>", self.ok)
        self.bind("<Escape>", self.cancel_with_snooze)

        box.pack()


class BatchEditDialog(simpledialog.Dialog):
    """Dialog for editing multiple entries at once."""

    def __init__(self, title: str, events: list, format_time_func) -> None:
        """Initialize batch edit dialog.

        Args:
            title: Dialog window title
            events: List of aw_core.Event objects to edit
            format_time_func: Function to format timestamps for display
        """
        self.events = events
        self.format_time = format_time_func
        self.entries: list[ttk.Entry] = []
        self.result: list[tuple] | None = None  # List of (event, new_value) tuples
        super().__init__(get_root(), title)

    def body(self, master):
        master = ttk.Frame(master)
        master.grid()

        # Create scrollable frame
        canvas = tk.Canvas(master, width=600, height=400)
        scrollbar = ttk.Scrollbar(master, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)

        scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        # Mouse wheel scrolling
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        canvas.bind_all("<Button-4>", lambda e: canvas.yview_scroll(-1, "units"))
        canvas.bind_all("<Button-5>", lambda e: canvas.yview_scroll(1, "units"))

        # Header
        ttk.Label(scrollable_frame, text="Time", font=("", 9, "bold")).grid(row=0, column=0, padx=5, pady=2, sticky="w")
        ttk.Label(scrollable_frame, text="Duration", font=("", 9, "bold")).grid(
            row=0, column=1, padx=5, pady=2, sticky="w"
        )
        ttk.Label(scrollable_frame, text="Description", font=("", 9, "bold")).grid(
            row=0, column=2, padx=5, pady=2, sticky="w"
        )

        # Create entry for each event
        for i, event in enumerate(self.events):
            row = i + 1
            start_str = self.format_time(event.timestamp)
            duration_min = event.duration.total_seconds() / 60
            current_msg = event.data.get("message", "")

            ttk.Label(scrollable_frame, text=start_str).grid(row=row, column=0, padx=5, pady=2, sticky="w")
            ttk.Label(scrollable_frame, text=f"{duration_min:.0f}m").grid(row=row, column=1, padx=5, pady=2, sticky="w")

            entry = EnhancedEntry(scrollable_frame, width=50)
            entry.insert(0, current_msg)
            entry.grid(row=row, column=2, padx=5, pady=2, sticky="ew")
            self.entries.append(entry)

        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        # Focus first entry
        if self.entries:
            return self.entries[0]

    def buttonbox(self):
        box = ttk.Frame(self)

        w = ttk.Button(box, text="Save All", width=12, command=self.ok, default=tk.ACTIVE)
        w.pack(side=tk.LEFT, padx=5, pady=5)
        w = ttk.Button(box, text="Cancel", width=12, command=self.cancel)
        w.pack(side=tk.LEFT, padx=5, pady=5)

        self.bind("<Return>", self.ok)
        self.bind("<Escape>", self.cancel)

        box.pack()

    def apply(self):
        """Collect all edited values."""
        self.result = []
        for event, entry in zip(self.events, self.entries):
            new_value = entry.get().strip()
            self.result.append((event, new_value))


def ask_batch_edit(title: str, events: list, format_time_func) -> list[tuple] | None:
    """Show batch edit dialog for multiple events.

    Args:
        title: Dialog title
        events: List of events to edit
        format_time_func: Function to format timestamps

    Returns:
        List of (event, new_value) tuples, or None if cancelled
    """
    d = BatchEditDialog(title, events, format_time_func)
    return d.result


def ask_string(
    title: str,
    prompt: str,
    history: list[str],
    afk_start=None,
    afk_duration_seconds=None,
    initial_value: str | None = None,
    queue_info: dict | None = None,
    is_ongoing: bool = False,
    still_afk_check=None,
    refresh=None,
    timeout_ms: int | None = None,
) -> str | None | tuple:
    """Ask for a string input, with optional split mode support.

    Args:
        title: Dialog window title
        prompt: Prompt text to display
        history: List of previous entries for history navigation
        afk_start: Start time of AFK period (optional, enables split mode)
        afk_duration_seconds: Duration of AFK period in seconds (optional)
        initial_value: Pre-fill the entry with this value (for editing)
        refresh: Callable re-read once a minute for facts that go stale while the
            dialog waits: {"prompt": str, "queue_info": dict | None}
        timeout_ms: Auto-snooze an unanswered dialog after this long (None = never)

    Returns:
        String input from user, or None if cancelled
        If split mode is activated, returns a special marker to indicate
        the calling code should use ask_split_activities instead.
    """
    # Loop to handle switching between single and split modes
    initial_text = initial_value
    while True:
        d = AWAfkPromptDialog(
            title,
            prompt,
            history,
            afk_start,
            afk_duration_seconds,
            queue_info=queue_info,
            is_ongoing=is_ongoing,
            still_afk_check=still_afk_check,
            refresh=refresh,
            timeout_ms=timeout_ms,
        )

        # Pre-fill with initial value or text from split mode
        if initial_text:
            d.entry.delete(0, tk.END)
            d.entry.insert(0, initial_text)
            initial_text = None

        # Wait for dialog to close
        # (AWAfkPromptDialog.__init__ calls wait_window internally via Dialog.__init__)

        # Check if user clicked Split button
        if d.split_mode:
            # Import here to avoid circular dependency
            from aw_watcher_afk_prompt.split_dialog import ask_split_activities

            # Show split dialog. Use the dialog's duration, which switch_to_split_mode
            # snapshots to start..now for ongoing periods (passed-in value is None there).
            result = ask_split_activities(title, prompt, afk_start, d.afk_duration_seconds, history)

            # Check what the split dialog returned
            if result is None:
                return None  # Cancelled in split mode
            elif isinstance(result, str):
                # User removed activities down to 1 - return to single mode
                logger.info(f"Returning to single mode with description: '{result}'")
                initial_text = result
                continue  # Loop back to show main dialog again
            else:
                # List of activities - return as split mode
                return ("SPLIT_MODE", result)

        # Normal mode - return the result
        return d.result


if __name__ == "__main__":
    print(ask_string("Testing testing", "123", ["1", "2", "3", "4"]))  # noqa: T201
