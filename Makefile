.PHONY: help install install-dev install-all test lint format clean uninstall install-service uninstall-service enable-service disable-service setup-wayland

help:
	@echo "Available targets:"
	@echo "  install-all       - Complete setup (install + enable service)"
	@echo "  install           - Install the package using pipx"
	@echo "  install-dev       - Install with development dependencies"
	@echo "  test              - Run tests"
	@echo "  lint              - Run linting (ruff check)"
	@echo "  format            - Format code (ruff format)"
	@echo "  clean             - Remove build artifacts and cache"
	@echo "  install-service   - Install systemd user service"
	@echo "  uninstall-service - Uninstall systemd user service"
	@echo "  enable-service    - Install and enable the service"
	@echo "  disable-service   - Disable and stop the service"
	@echo "  setup-wayland     - Configure Wayland environment import"
	@echo "  uninstall         - Uninstall the package"
	@echo "WARNING: This large amount of targets was made by an eager AI-bot"
	@echo "Only enable-service and setup-wayland has been tested, the latter with sway"

install:  ## Install the package (auto-detects root, uv, pipx, or pip)
	@if [ "$$(id -u)" = "0" ]; then \
		echo "Running as root, installing system-wide..."; \
		pip install .; \
	elif command -v uv >/dev/null 2>&1; then \
		echo "Installing with uv..."; \
		uv tool install .; \
	elif command -v pipx >/dev/null 2>&1; then \
		echo "Installing with pipx..."; \
		pipx install .; \
	else \
		echo "Tip: Install uv or pipx for isolated installs (pacman -S uv, apt install pipx, brew install uv)"; \
		echo "Falling back to pip install --user ..."; \
		PIP_BREAK_SYSTEM_PACKAGES=1 pip install --user .; \
	fi
	@echo ""
	@echo "Next steps:"
	@echo "  1. Recommended: Use aw-qt (ActivityWatch GUI)"
	@echo "     aw-qt will detect and start this watcher automatically"
	@echo ""
	@echo "  2. Alternative: Run as systemd service"
	@echo "     make enable-service"
	@echo ""
	@echo "  3. For Wayland users running systemd:"
	@echo "     make setup-wayland"
	@echo ""

install-dev:  ## Install with development dependencies (editable)
	PIP_BREAK_SYSTEM_PACKAGES=1 pip install -e ".[dev]"

install-all: install enable-service
	@echo ""
	@echo "✓ Installation complete!"
	@echo "  The watcher is now installed and running as a systemd service."
	@echo ""
	@echo "Check status with: systemctl --user status aw-watcher-afk-prompt"
	@echo "View logs with:    journalctl --user -u aw-watcher-afk-prompt -f"
	@echo ""
	@echo "Note: Wayland users should also run: make setup-wayland"

test:  ## Run tests
	python -m pytest tests/ -v

lint:  ## Run ruff linter and formatter check
	python -m ruff check .
	python -m ruff format --check .

format:  ## Auto-format code
	python -m ruff check --fix .
	python -m ruff format .

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	rm -rf dist/ build/

install-service:
	@echo "Installing systemd user service..."
	mkdir -p ~/.config/systemd/user
	cp misc/aw-watcher-afk-prompt.service ~/.config/systemd/user/
	systemctl --user daemon-reload
	@echo "Service installed. Use 'make enable-service' to enable and start it."

uninstall-service:
	@echo "Uninstalling systemd user service..."
	systemctl --user stop aw-watcher-afk-prompt 2>/dev/null || true
	systemctl --user disable aw-watcher-afk-prompt 2>/dev/null || true
	rm -f ~/.config/systemd/user/aw-watcher-afk-prompt.service
	systemctl --user daemon-reload
	@echo "Service uninstalled."

enable-service: install-service
	@echo "Enabling and starting service..."
	systemctl --user enable aw-watcher-afk-prompt
	systemctl --user start aw-watcher-afk-prompt
	@echo "Service status:"
	@systemctl --user status aw-watcher-afk-prompt --no-pager

disable-service:
	@echo "Disabling and stopping service..."
	systemctl --user stop aw-watcher-afk-prompt
	systemctl --user disable aw-watcher-afk-prompt
	@echo "Service disabled."

setup-wayland: enable-service
	@echo "Configuring Wayland environment import..."
	@echo ""
	@echo "Detecting compositor configuration files..."
	@if [ -f ~/.config/sway/config ]; then \
		echo "Found Sway config at ~/.config/sway/config"; \
		if grep -q "systemctl --user import-environment WAYLAND_DISPLAY" ~/.config/sway/config; then \
			echo "✓ Environment import already configured"; \
		else \
			echo "" >> ~/.config/sway/config; \
			echo "# Import WAYLAND_DISPLAY for systemd services" >> ~/.config/sway/config; \
			echo "exec systemctl --user import-environment WAYLAND_DISPLAY" >> ~/.config/sway/config; \
			echo "✓ Added environment import to Sway config"; \
			echo "  Please reload Sway config or log out and back in"; \
		fi; \
	elif [ -f ~/.config/hypr/hyprland.conf ]; then \
		echo "Found Hyprland config at ~/.config/hypr/hyprland.conf"; \
		if grep -q "systemctl --user import-environment WAYLAND_DISPLAY" ~/.config/hypr/hyprland.conf; then \
			echo "✓ Environment import already configured"; \
		else \
			echo "" >> ~/.config/hypr/hyprland.conf; \
			echo "# Import WAYLAND_DISPLAY for systemd services" >> ~/.config/hypr/hyprland.conf; \
			echo "exec-once = systemctl --user import-environment WAYLAND_DISPLAY" >> ~/.config/hypr/hyprland.conf; \
			echo "✓ Added environment import to Hyprland config"; \
			echo "  Please reload Hyprland config or log out and back in"; \
		fi; \
	else \
		echo "Could not detect compositor config file."; \
		echo ""; \
		echo "Please manually add this line to your compositor startup:"; \
		echo "  exec systemctl --user import-environment WAYLAND_DISPLAY"; \
		echo ""; \
		echo "Common locations:"; \
		echo "  - Sway: ~/.config/sway/config"; \
		echo "  - Hyprland: ~/.config/hypr/hyprland.conf"; \
		echo "  - Others: check your compositor documentation"; \
	fi
	@echo ""
	@echo "Restarting service to pick up environment changes..."
	@systemctl --user restart aw-watcher-afk-prompt 2>/dev/null || echo "Note: Service restart will happen after compositor reload"
	@echo ""
	@echo "⚠ IMPORTANT: The environment variable will only be available after:"
	@echo "  1. Reloading your compositor config, OR"
	@echo "  2. Logging out and back in"
	@echo ""
	@echo "After that, verify the service is working:"
	@echo "  systemctl --user status aw-watcher-afk-prompt"

uninstall:
	pip uninstall -y aw-watcher-afk-prompt 2>/dev/null || true
	@echo "Package uninstalled."
