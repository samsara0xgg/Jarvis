#!/usr/bin/env python3
"""Claude Code API Key Switcher"""

import json
import sys
import os
from pathlib import Path

SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
KEYS_PATH = Path.home() / ".claude" / "api_keys.json"
OFFICIAL_BASE_URL = "https://api.anthropic.com"

# Default env settings to preserve
DEFAULT_ENV_SETTINGS = {
    "ANTHROPIC_SMALL_FAST_MODEL": "claude-haiku-4-5-20251001",
    "ANTHROPIC_MODEL": "claude-sonnet-4-6",
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "80",
    "CLAUDE_CODE_MAX_TOOL_USE_TOKENS": "30000"
}

# Additional settings for specific APIs
API_SPECIFIC_SETTINGS = {
    "month": {
        "DISABLE_NON_ESSENTIAL_MODEL_CALLS": "1"
    }
}

# Settings to remove for specific APIs
API_REMOVE_SETTINGS = {
    "month": ["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "CLAUDE_CODE_MAX_TOOL_USE_TOKENS"]
}

try:
    import questionary
    from questionary import Style
    INTERACTIVE = True

    # Claude Code style
    custom_style = Style([
        ('qmark', 'fg:#673ab7 bold'),
        ('question', 'bold'),
        ('answer', 'fg:#673ab7 bold'),
        ('pointer', 'fg:#673ab7 bold'),
        ('highlighted', 'fg:#673ab7 bold'),
        ('selected', 'fg:#cc5454'),
        ('separator', 'fg:#cc5454'),
        ('instruction', ''),
        ('text', ''),
    ])
except ImportError:
    INTERACTIVE = False
    custom_style = None


def load_settings():
    """Load Claude Code settings"""
    if not SETTINGS_PATH.exists():
        print(f"Settings file not found: {SETTINGS_PATH}")
        exit(1)
    with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_settings(settings):
    """Save Claude Code settings"""
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
    print("✓ Settings saved")


def load_keys():
    """Load saved API keys"""
    if not KEYS_PATH.exists():
        return {}
    with open(KEYS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_keys(keys):
    """Save API keys"""
    with open(KEYS_PATH, "w", encoding="utf-8") as f:
        json.dump(keys, f, indent=2, ensure_ascii=False)


def make_link(url, label=None):
    """Return an OSC 8 clickable hyperlink if terminal likely supports it, else plain URL"""
    label = label or url
    # OSC 8 hyperlink: ESC ] 8 ; ; url ST label ESC ] 8 ; ; ST
    return f"\033]8;;{url}\033\\{label}\033]8;;\033\\"


def show_current(settings, keys):
    """Display current configuration"""
    env = settings.get("env", {})
    current_key = env.get("ANTHROPIC_AUTH_TOKEN", "Not set")
    base_url = env.get("ANTHROPIC_BASE_URL", OFFICIAL_BASE_URL)

    # Find key name and full info
    key_name = "Unknown"
    current_info = {}
    for name, info in keys.items():
        if info["key"] == current_key:
            key_name = name
            current_info = info
            break

    print("\nCurrent configuration:")
    print(f"  Name: {key_name}")
    print(f"  API Key: {current_key[:20]}...{current_key[-10:] if len(current_key) > 30 else ''}")
    print(f"  Endpoint: {base_url}")
    is_official = base_url == OFFICIAL_BASE_URL or "ANTHROPIC_BASE_URL" not in env
    print(f"  Type: {'Official' if is_official else 'Proxy'}")
    if is_official:
        print(f"  Usage: {make_link('https://console.anthropic.com/settings/billing')}")
    else:
        quota_url = current_info.get("quota_url", "")
        if quota_url:
            print(f"  Quota: {make_link(quota_url)}")
    print()


def apply_key(settings, name, info):
    """Apply an API key to settings with default and API-specific env settings"""
    if "env" not in settings:
        settings["env"] = {}

    # Apply default settings
    for key, value in DEFAULT_ENV_SETTINGS.items():
        settings["env"][key] = value

    # Apply API-specific settings
    if name in API_SPECIFIC_SETTINGS:
        for key, value in API_SPECIFIC_SETTINGS[name].items():
            settings["env"][key] = value
    else:
        # Remove API-specific settings if switching away
        for api_settings in API_SPECIFIC_SETTINGS.values():
            for key in api_settings.keys():
                settings["env"].pop(key, None)

    # Remove settings that should be absent for this API
    for key in API_REMOVE_SETTINGS.get(name, []):
        settings["env"].pop(key, None)

    # Set token and URL
    settings["env"]["ANTHROPIC_AUTH_TOKEN"] = info["key"]

    if info.get("official", True):
        settings["env"].pop("ANTHROPIC_BASE_URL", None)
    else:
        base_url = info.get("base_url", "")
        if base_url:
            settings["env"]["ANTHROPIC_BASE_URL"] = base_url


def switch_to_saved_key(settings, keys):
    """Switch to a saved key"""
    if not keys:
        print("\nNo saved API keys")
        return False

    key_list = list(keys.items())
    choices = []
    for name, info in key_list:
        key_preview = info["key"][:20] + "..." + info["key"][-10:]
        key_type = "Official" if info.get("official", True) else "Proxy"
        choices.append(f"{name} ({key_type}) - {key_preview}")

    if INTERACTIVE:
        choices.append("← Back")
        selected = questionary.select(
            "Select API key:",
            choices=choices,
            style=custom_style
        ).ask()

        if not selected or selected == "← Back":
            return False

        idx = choices.index(selected)
    else:
        print("\nSaved API keys:")
        for i, choice in enumerate(choices, 1):
            print(f"  {i}. {choice}")
        print("  0. Back")

        choice = input("\nSelect key (enter number): ").strip()
        if choice == "0":
            return False
        try:
            idx = int(choice) - 1
            if not (0 <= idx < len(key_list)):
                print("Invalid selection")
                return False
        except ValueError:
            print("Please enter a number")
            return False

    name, info = key_list[idx]
    apply_key(settings, name, info)
    save_settings(settings)
    print(f"✓ Switched to: {name}")
    return True


def switch_to_official(settings):
    """Switch to official API - keep optimization settings, remove auth/proxy"""
    if "env" not in settings:
        settings["env"] = {}

    # Preserve default optimization settings
    for key, value in DEFAULT_ENV_SETTINGS.items():
        settings["env"][key] = value

    # Remove auth and proxy settings
    settings["env"].pop("ANTHROPIC_AUTH_TOKEN", None)
    settings["env"].pop("ANTHROPIC_BASE_URL", None)

    # Remove all API-specific settings
    for api_settings in API_SPECIFIC_SETTINGS.values():
        for key in api_settings.keys():
            settings["env"].pop(key, None)

    save_settings(settings)
    print("✓ Switched to official Anthropic API")
    return True


def add_new_key(settings, keys):
    """Add a new API key"""
    if INTERACTIVE:
        name = questionary.text("Name (e.g., work, personal) [leave empty to cancel]:", style=custom_style).ask()
        if not name:
            return False

        key = questionary.password("API Key: [leave empty to cancel]:", style=custom_style).ask()
        if not key:
            return False

        if not key.startswith("sk-"):
            print("⚠ Warning: API key usually starts with sk-")

        key_type = questionary.select(
            "Type:",
            choices=["Official Anthropic API", "Third-party proxy", "← Back"],
            style=custom_style
        ).ask()

        if not key_type or key_type == "← Back":
            return False

        is_official = key_type == "Official Anthropic API"
        base_url = ""

        if not is_official:
            base_url = questionary.text("Proxy URL (e.g., https://api.example.com):", style=custom_style).ask()
            if base_url is None:
                return False

        quota_url = questionary.text("Quota URL (optional, e.g. https://example.com/quota):", style=custom_style).ask() or ""

        keys[name] = {
            "key": key,
            "official": is_official,
            "base_url": base_url,
            "quota_url": quota_url
        }
        save_keys(keys)

        use_now = questionary.confirm(f"Use {name} now?", default=True, style=custom_style).ask()
    else:
        print("\nAdd new API key")
        name = input("Name (e.g., work, personal) [0 to go back]: ").strip()
        if name == "0":
            return False
        if not name:
            print("Name cannot be empty")
            return False

        key = input("API Key: ").strip()
        if not key.startswith("sk-"):
            print("Warning: API key usually starts with sk-")

        key_type = input("Type (1=Official, 2=Proxy, 0=Back) [1]: ").strip() or "1"
        if key_type == "0":
            return False
        is_official = key_type == "1"

        base_url = ""
        if not is_official:
            base_url = input("Proxy URL (e.g., https://api.example.com): ").strip()

        quota_url = input("Quota URL (optional, press Enter to skip): ").strip()

        keys[name] = {
            "key": key,
            "official": is_official,
            "base_url": base_url,
            "quota_url": quota_url
        }
        save_keys(keys)

        use_now = input("\nUse this key now? (y/n) [y]: ").strip().lower() != "n"

    if use_now:
        apply_key(settings, name, keys[name])
        save_settings(settings)
        print(f"✓ Added and switched to: {name}")
    else:
        print(f"✓ Added: {name}")
    return True


def main():
    print("Claude Code API Key Switcher\n")

    settings = load_settings()
    keys = load_keys()

    while True:
        os.system("clear" if os.name != "nt" else "cls")
        print("Claude Code API Key Switcher\n")
        show_current(settings, keys)

        if INTERACTIVE:
            action = questionary.select(
                "What would you like to do?",
                choices=[
                    "Switch to saved key",
                    "Switch to official API",
                    "Add new API key",
                    "Exit"
                ],
                style=custom_style
            ).ask()

            if not action or action == "Exit":
                break

            if action == "Switch to saved key":
                if switch_to_saved_key(settings, keys):
                    keys = load_keys()  # Reload in case new key was added
            elif action == "Switch to official API":
                switch_to_official(settings)
            elif action == "Add new API key":
                if add_new_key(settings, keys):
                    keys = load_keys()
        else:
            print("Options:")
            print("  1. Switch to saved key")
            print("  2. Switch to official API")
            print("  3. Add new API key")
            print("  4. Exit")

            choice = input("\nSelect (1-4): ").strip()

            if choice == "1":
                if switch_to_saved_key(settings, keys):
                    keys = load_keys()
            elif choice == "2":
                switch_to_official(settings)
            elif choice == "3":
                if add_new_key(settings, keys):
                    keys = load_keys()
            elif choice == "4":
                print("Goodbye")
                break
            else:
                print("Invalid selection")


if __name__ == "__main__":
    main()
