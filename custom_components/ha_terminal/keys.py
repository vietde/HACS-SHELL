"""Authorized-keys management for HA Terminal.

Pure logic for the ``/config/.ssh/authorized_keys`` file — no Home
Assistant imports, so it is unit-testable offline. The HTTP view in
views.py is a thin wrapper around these functions.

A pasted line may be a plain public key (``ssh-ed25519 AAAA… comment``)
or an authorized_keys line with options (``from="…" ssh-ed25519 …``).
Private keys are rejected, as is anything that does not parse as a
public key.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_KEY_LINE_RE = re.compile(
    r"(ssh-(?:ed25519|rsa|dss)"
    r"|ecdsa-sha2-nistp(?:256|384|521)"
    r"|sk-ssh-ed25519@openssh\.com"
    r"|sk-ecdsa-sha2-nistp256@openssh\.com)"
    r"\s+([A-Za-z0-9+/=]+)"
    r"(?:\s+(.*))?$"
)


def authorized_keys_path(config_dir: str | Path) -> Path:
    """``/config/.ssh/authorized_keys`` for the given config folder."""
    return Path(config_dir) / ".ssh" / "authorized_keys"


def _parse_key_line(line: str):
    """Validate one pasted line.

    Returns ``(key_type, blob, comment, fingerprint)`` or ``None`` when
    the line is not a usable public key. The blob (base64 key material)
    is what dedupe/removal match on.
    """
    import asyncssh

    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "PRIVATE KEY" in line or line.startswith("-----"):
        return None
    match = _KEY_LINE_RE.search(line)
    if match is None:
        return None
    key_type, blob, comment = match.group(1), match.group(2), (match.group(3) or "").strip()
    try:
        key = asyncssh.import_public_key(f"{key_type} {blob}")
    except Exception:  # noqa: BLE001 - bad key material
        return None
    try:
        # Validates the whole line, including any options prefix.
        asyncssh.import_authorized_keys(line)
    except Exception:  # noqa: BLE001 - bad options/format
        return None
    try:
        fingerprint = key.get_fingerprint()
    except Exception:  # noqa: BLE001 - fingerprint is display-only
        fingerprint = ""
    return key_type, blob, comment, fingerprint


def list_keys(path: str | Path) -> list[dict]:
    """Parse the file into display rows, skipping unparsable lines.

    Unparsable lines are left in the file untouched; they just do not
    show up here (and are preserved by remove_key).
    """
    path = Path(path)
    rows: list[dict] = []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return rows
    for line in lines:
        parsed = _parse_key_line(line)
        if parsed is None:
            continue
        key_type, blob, comment, fingerprint = parsed
        rows.append(
            {
                "key_type": key_type,
                "comment": comment,
                "fingerprint": fingerprint,
                "line": line.strip(),
            }
        )
    return rows


def add_key(path: str | Path, line: str) -> tuple[bool, str]:
    """Append a key line if it is valid and not already present."""
    parsed = _parse_key_line(line)
    if parsed is None:
        return False, "That does not look like a public key — paste a line like: ssh-ed25519 AAAA… you@laptop"
    _key_type, blob, _comment, _fingerprint = parsed
    path = Path(path)
    existing = list_keys(path)
    if any(_parse_key_line(row["line"])[1] == blob for row in existing):
        return False, "That key is already added."
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        with path.open("a", encoding="utf-8") as out:
            out.write(line.strip() + "\n")
        os.chmod(path, 0o600)
    except OSError as err:
        return False, f"Could not save the key: {err}"
    return True, "Key added — SSH login without a password should work now."


def remove_key(path: str | Path, fingerprint: str) -> tuple[bool, str]:
    """Remove the key with this fingerprint, keeping other lines intact."""
    path = Path(path)
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return False, "No keys file found."
    kept: list[str] = []
    removed = False
    for line in lines:
        parsed = _parse_key_line(line)
        if parsed is not None and parsed[3] == fingerprint:
            removed = True
            continue
        kept.append(line)
    if not removed:
        return False, "That key was not found."
    try:
        path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError as err:
        return False, f"Could not save the keys file: {err}"
    return True, "Key removed."
