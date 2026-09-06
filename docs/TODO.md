# TODO List for aw-watcher-afk-prompt

This document tracks planned improvements, known issues, and future work for the aw-watcher-afk-prompt project.

## High Priority

### User Experience Improvements
- [ ] Handle video calls better
  - Stop asking every few minutes during active calls
  - Detect call state from window titles or system events
  - Option to automatically track calls as single activity
- [ ] Widget positioning improvements
  - Dialog pops up off-center when using multiple screens on Linux
  - Implement proper multi-monitor support (dialog.py:175)

## Medium Priority

### Manual operations

* The logic in aw-watcher-afk-prompt should possibly also be applicable when not-afk.
* aw-export-timewarrior: Should consider to ask for activity when the hints in the acticitywatcher data is weak
* Should be easy to specify that "activity with tags X today was Y".  Like, feh was used for sorting inventory, etc.

### Dialog & UI Enhancements
- [ ] Allow customizing the prompt from the prompt interface (__main__.py:25)
- [ ] Make configurable whether to show abbreviations panel by default (dialog.py:346)
- [ ] Implement easy snooze duration picker (dialog.py:372)
  - Quick buttons for common durations (5min, 15min, 30min)
  - Custom duration input

### Abbreviations System
- [ ] Link abbreviations JSON file for direct editing (dialog.py:100)
- [ ] Improve abbreviations display in settings (dialog.py:106)
  - Consider table view or searchable list
  - Show usage statistics
- [ ] Allow editing abbreviations in place (dialog.py:150)
  - Currently requires remove and re-add
  - Implement inline editing

### Code Quality
- [ ] An unanswered split dialog blocks the watcher indefinitely
      (split_dialog.py, no `after(` anywhere in it)
  - The ordinary prompt auto-snoozes after `timeout_ms` and comes back later
    (dialog.py:481-494), but pressing Split cancels those timers
    (dialog.py:698) and the split dialog arms none of its own. A split left
    open therefore holds the poll loop for as long as it sits there — observed
    2026-09-05: 15:56 to 17:29, no prompts, no snooze.
  - Fix: thread `timeout_ms` through `ask_string` → `ask_split_activities` →
    `SplitActivityDialog` and arm the same countdown. Needs a decision first on
    what an auto-snooze does with half-typed activity lines: dropping the
    user's typing silently is worse than the block it prevents.
- [ ] Start time is validated and committed on every keystroke
      (split_dialog.py:743, :910 → `_on_start_change`)
  - The `trace_add("write", …)` callback runs per keystroke, so each partially
    typed value is parsed. The ones that fail log at WARNING (:1122, :1129,
    :1161, :1164) — typing `22:00` into an empty field gives three, and a
    backspaced-then-retyped `01:03` gives eight, which is where the observed
    `Invalid start time format: 0103`, `003`, `03` came from.
  - Worse than the log noise: a partial value that *parses* is committed.
    Typing `22:0` on the way to `22:05` reaches `adjust_start_time` (:1144),
    redistributes every duration, and rewrites the entry text through
    `update_from_activity` (:1158, :883) while the user is still typing. So the
    fix is to validate on commit (Return/focus-out), not to lower the log level.
  - The duration field has the same shape: `_on_duration_change` per keystroke,
    warning at :809 whenever the `IntVar` holds a partial or empty value.
- [x] Wrap Entry widget for reuse across dialogs (dialog.py:215)
  - Created EnhancedEntry widget in widgets.py with keyboard shortcuts
  - Used in AWAskAwayDialog, BatchEditDialog, and split_dialog.py
- [x] Investigate why aw-watcher-afk uses queued=True (core.py:186)
  - queued=True enables persistent request queue for reliability
  - Bucket creation is queued and retried if server is temporarily down
  - Added queued=True to match pattern used by other watchers

## Low Priority / Future Considerations

### Data Management
- [ ] Option to add data to AFK events instead of separate bucket
  - Some users may prefer consolidated data
  - Make it configurable
  - Consider migration path

### Split Feature Enhancements
- [ ] Add preset split templates
  - Common patterns like "lunch + walk" or "meeting + email"
  - User-defined templates
- [ ] Improve split validation feedback
  - More detailed error messages
  - Suggestions for fixing validation errors
- [ ] Add keyboard shortcuts in split dialog
  - Tab navigation between fields
  - Enter to add new activity
  - Delete key to remove activity

### Testing & Quality
- [ ] `make test` draws real Tk windows on the developer's display
  - The `nonmodal` fixture in test_dialog_ui.py:35 stubs `deiconify` to prevent
    exactly this, and the stub does nothing: `tkinter.Wm.deiconify` *is*
    `tkinter.Wm.wm_deiconify` (an in-class alias), and `simpledialog` calls
    `w.wm_deiconify()` from `_place_window`, which no attribute set on the
    subclass shadows. Patch `wm_deiconify` instead.
  - So it is `AWAfkPromptDialog`, i.e. test_dialog_ui.py itself, that flashes —
    every dialog it constructs. test_split_dialog_ui.py and test_widgets.py do
    create a root per test but withdraw it, and bypass `Dialog.__init__`
    entirely, so they map nothing.
  - CI already avoids the whole question with `xvfb-run`
    (.github/workflows/ci.yml:34) while `make test` (Makefile:68) does not.
    `make test` should use `xvfb-run` when available and say so when it is not.
- [ ] Add integration tests with real ActivityWatch server
  - Currently only unit tests and mocked integration tests
- [ ] Add UI automation tests
  - Test dialog interactions
  - Verify split feature workflows
- [ ] Performance testing with many events
  - Test with hundreds of recent events
  - Optimize event fetching and processing

### Documentation
- [ ] Create video tutorials
  - Basic usage walkthrough
  - Split feature demonstration
  - Configuration guide
- [ ] Add troubleshooting guide
  - Common issues and solutions
  - Debug logging instructions
- [ ] Document API for extensions
  - How to add custom dialog behaviors
  - Integration with other tools

## Completed ✓

- [x] Basic AFK period logging
- [x] Dialog with history/abbreviations
- [x] Configuration file support
- [x] Lid watcher integration (optional)
- [x] Split AFK period feature
  - [x] Split dialog UI
  - [x] Time validation and automatic calculation
  - [x] Split event metadata for export tools
  - [x] Unit and integration tests
- [x] --test-dialog flag for UI testing
- [x] Systemd service configuration
- [x] Wrap Entry widget for reuse across dialogs
  - [x] Created EnhancedEntry in widgets.py
  - [x] Added keyboard shortcuts (Ctrl+Backspace, Ctrl+w)
  - [x] Used in all dialogs for consistent behavior
- [x] Investigated and implemented queued=True for reliability

### Distribution & Installation
- [ ] Set up a website and documentation
  - Consider GitHub organization for better visibility
  - Provide installation guides and tutorials


## Ideas / Discussion Needed

### Call Detection
- How to reliably detect video calls?
  - Window title patterns (Zoom, Teams, Meet, etc.)
  - System audio/video device usage
  - Manual "in call" toggle button?

### Bucket Strategy
- Should we continue with separate bucket or merge into AFK events?
  - Pros of separate: Clean separation, doesn't pollute AFK data
  - Cons of separate: More complex queries for consumers
  - Could we support both modes?

### UI Framework
- Should we consider moving away from tkinter?
  - tkinter pros: stdlib, cross-platform
  - tkinter cons: limited styling, positioning issues
  - Alternatives: Qt (via PySide6), web-based (Electron-style)
  - Migration effort vs. benefit?

## Contributing

To work on any of these items:
1. Comment on the relevant GitHub issue or create one
2. Update this TODO with your progress
3. Submit a PR when ready

For more information, see [CONTRIBUTING.md](../CONTRIBUTING.md) (if it exists).
