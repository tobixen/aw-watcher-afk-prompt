"""Tests for the widgets module."""

import tkinter as tk

import pytest


@pytest.fixture
def root():
    """Create a Tk root window for testing."""
    root = tk.Tk()
    root.withdraw()  # Hide the window
    yield root
    root.destroy()


class TestEnhancedEntry:
    """Tests for the EnhancedEntry widget."""

    def test_import(self):
        """Test that EnhancedEntry can be imported."""
        from aw_watcher_afk_prompt.widgets import EnhancedEntry
        assert EnhancedEntry is not None

    def test_creation(self, root):
        """Test that EnhancedEntry can be created."""
        from aw_watcher_afk_prompt.widgets import EnhancedEntry
        entry = EnhancedEntry(root)
        assert entry is not None

    def test_set_text(self, root):
        """Test set_text method."""
        from aw_watcher_afk_prompt.widgets import EnhancedEntry
        entry = EnhancedEntry(root)
        entry.set_text("hello world")
        assert entry.get() == "hello world"

    def test_set_text_replaces_existing(self, root):
        """Test that set_text replaces existing content."""
        from aw_watcher_afk_prompt.widgets import EnhancedEntry
        entry = EnhancedEntry(root)
        entry.insert(0, "original text")
        entry.set_text("new text")
        assert entry.get() == "new text"

    def test_remove_word_simple(self, root):
        """Test removing a single word."""
        from aw_watcher_afk_prompt.widgets import EnhancedEntry
        entry = EnhancedEntry(root)
        entry.insert(0, "hello world")
        entry.icursor(tk.END)  # Put cursor at end
        entry._remove_word()
        assert entry.get() == "hello "

    def test_remove_word_from_middle(self, root):
        """Test removing word from middle of text.

        Note: This leaves a double space because we remove the word and its
        trailing space, but the following word already has a leading space.
        This matches the original behavior.
        """
        from aw_watcher_afk_prompt.widgets import EnhancedEntry
        entry = EnhancedEntry(root)
        entry.insert(0, "one two three")
        entry.icursor(7)  # Put cursor after "one two"
        entry._remove_word()
        assert entry.get() == "one  three"

    def test_remove_word_with_trailing_space(self, root):
        """Test removing word with trailing whitespace."""
        from aw_watcher_afk_prompt.widgets import EnhancedEntry
        entry = EnhancedEntry(root)
        entry.insert(0, "hello world ")
        entry.icursor(tk.END)  # Put cursor at end
        entry._remove_word()
        assert entry.get() == "hello "

    def test_remove_word_single_word(self, root):
        """Test removing the only word."""
        from aw_watcher_afk_prompt.widgets import EnhancedEntry
        entry = EnhancedEntry(root)
        entry.insert(0, "hello")
        entry.icursor(tk.END)
        entry._remove_word()
        assert entry.get() == ""

    def test_remove_word_empty_entry(self, root):
        """Test removing word from empty entry."""
        from aw_watcher_afk_prompt.widgets import EnhancedEntry
        entry = EnhancedEntry(root)
        entry._remove_word()
        assert entry.get() == ""

    def test_keyboard_bindings_exist(self, root):
        """Test that keyboard bindings are set up."""
        from aw_watcher_afk_prompt.widgets import EnhancedEntry
        entry = EnhancedEntry(root)

        # Check that bindings exist (they return non-empty tuples)
        ctrl_backspace = entry.bind("<Control-BackSpace>")
        ctrl_w = entry.bind("<Control-w>")

        # These should return function references (non-empty strings)
        assert ctrl_backspace
        assert ctrl_w

    def test_inherits_from_ttk_entry(self, root):
        """Test that EnhancedEntry inherits from ttk.Entry."""
        from tkinter import ttk

        from aw_watcher_afk_prompt.widgets import EnhancedEntry
        entry = EnhancedEntry(root)
        assert isinstance(entry, ttk.Entry)

    def test_passes_kwargs_to_parent(self, root):
        """Test that kwargs are passed to parent Entry."""
        from aw_watcher_afk_prompt.widgets import EnhancedEntry
        entry = EnhancedEntry(root, width=50)
        # If no exception is raised, kwargs were passed correctly
        assert entry.cget("width") == 50

    def test_textvariable_support(self, root):
        """Test that textvariable works correctly."""
        from aw_watcher_afk_prompt.widgets import EnhancedEntry
        var = tk.StringVar(value="initial")
        entry = EnhancedEntry(root, textvariable=var)
        assert entry.get() == "initial"

        var.set("updated")
        assert entry.get() == "updated"
