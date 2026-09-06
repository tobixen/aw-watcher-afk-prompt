"""Reusable UI widgets for aw-watcher-afk-prompt.

This module contains shared widget classes that provide consistent
behavior across all dialogs in the application.
"""

import contextlib
import re
import tkinter as tk
from tkinter import ttk


def flush_closed_windows(widget: tk.Misc) -> None:
    """Send a just-closed dialog's window changes to the display server.

    ``destroy()`` only queues the removal; Tk sends it on the next pass through
    the event loop, and a watcher that closes a dialog and goes back to polling
    never makes one. The window then stays on the screen, mapped and
    unresponsive, with nobody left to read its events. Any live widget will do
    as the argument — the flush is per interpreter, not per window.

    Swallows the TclError from a display server that has gone away meanwhile,
    since this runs where an exception would replace a more interesting one.
    """
    with contextlib.suppress(tk.TclError):
        widget.update_idletasks()


class EnhancedEntry(ttk.Entry):
    """Enhanced Entry widget with keyboard shortcuts for text editing.

    Provides common keyboard shortcuts that work consistently across all
    dialogs in the application:
    - Ctrl+Backspace / Ctrl+w: Remove word before cursor
    - Ctrl+u: Remove text from cursor to start of line (clear line)

    Usage:
        entry = EnhancedEntry(parent, width=40)
        entry.grid(row=0, column=0)
    """

    def __init__(self, master=None, **kwargs):
        super().__init__(master, **kwargs)
        self._bind_keyboard_shortcuts()

    def _bind_keyboard_shortcuts(self) -> None:
        """Bind keyboard shortcuts for text editing."""
        # Remove word before cursor
        self.bind("<Control-BackSpace>", self._remove_word)
        self.bind("<Control-w>", self._remove_word)

    def _remove_word(self, event=None):  # noqa: ARG002
        """Remove the word before the cursor.

        This mimics the behavior of Ctrl+Backspace in most text editors,
        removing the word before the cursor position.
        """
        text = self.get()
        cursor_index = self.index(tk.INSERT)

        # Use regex to remove trailing word and whitespace
        new_before = re.sub(r"\w+\W*$", "", text[:cursor_index])

        # Replace text up to cursor
        self.delete(0, cursor_index)
        self.insert(0, new_before)

        # Prevent the event from propagating (avoid dialog-level handlers)
        return "break"

    def set_text(self, text: str) -> None:
        """Set the entry text, replacing any existing content.

        Args:
            text: The text to set
        """
        self.delete(0, tk.END)
        self.insert(0, text)
