"""macOS Keychain storage for the two secrets this tool needs.

Nothing secret is ever written to the config file, passed on a command line, or
echoed. ``security`` is invoked with ``-w`` last and no value, which makes it
prompt on the terminal - so the secret never enters argv and cannot be read out
of ``ps`` by another process on the machine.
"""

from __future__ import annotations

import shutil
import subprocess

from tathya.errors import ConfigError

SERVICE = "tathya-cli"


def _security_bin() -> str:
    path = shutil.which("security")
    if not path:
        raise ConfigError(
            "The macOS `security` CLI was not found, so Keychain storage is unavailable."
        )
    return path


def account_for_password(username: str) -> str:
    return f"password:{username}"


GCHAT_WEBHOOK_ACCOUNT = "gchat-webhook"


def get_secret(account: str) -> str | None:
    proc = subprocess.run(
        [_security_bin(), "find-generic-password", "-s", SERVICE, "-a", account, "-w"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    secret = proc.stdout.rstrip("\n")
    return secret or None


def set_secret_interactive(account: str, label: str) -> None:
    """Prompt on the tty and store the result. The value never touches argv."""
    print(f"Enter the {label}. Input is hidden and goes straight to the Keychain.")
    proc = subprocess.run(
        [
            _security_bin(),
            "add-generic-password",
            "-U",
            "-s",
            SERVICE,
            "-a",
            account,
            "-l",
            f"{SERVICE} ({account})",
            "-D",
            "application password",
            "-w",
        ],
        check=False,
    )
    if proc.returncode != 0:
        raise ConfigError(
            "Keychain write failed or was cancelled; nothing was stored.",
            account=account,
        )


def delete_secret(account: str) -> bool:
    proc = subprocess.run(
        [_security_bin(), "delete-generic-password", "-s", SERVICE, "-a", account],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


def has_secret(account: str) -> bool:
    return get_secret(account) is not None
