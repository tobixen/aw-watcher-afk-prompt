# TODO List for aw-watcher-ask-away

This document tracks planned improvements, known issues, and future work for the aw-watcher-ask-away project.

## High Priority

### Distribution & Installation
- [ ] Use `pyinstaller` or similar for binary distribution
  - Target non-developer users who don't know PyPI
  - Consider creating installers for Windows, macOS, Linux
- [ ] Set up a website and documentation
  - Consider GitHub organization for better visibility
  - Provide installation guides and tutorials

### User Experience Improvements
- [ ] Handle video calls better
  - Stop asking every few minutes during active calls
  - Detect call state from window titles or system events
  - Option to automatically track calls as single activity
- [ ] Widget positioning improvements
  - Dialog pops up off-center when using multiple screens on Linux
  - Implement proper multi-monitor support (dialog.py:175)

## Medium Priority

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
- [ ] Wrap Entry widget for reuse across dialogs (dialog.py:215)
  - Create reusable component with keyboard shortcuts
  - Standardize behavior across all dialogs
- [ ] Investigate why aw-watcher-afk uses queued=True (core.py:92)
  - Determine if we should also use it
  - Document the reasoning

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
