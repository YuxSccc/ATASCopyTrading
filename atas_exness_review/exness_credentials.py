"""Configure this Windows user's Exness Trading read credential for chart sync."""

import getpass
import json
import re
from urllib.parse import urlsplit

import pywintypes
import win32cred
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


TARGET = "AtasExnessReview/ExnessTraderRead"


def read_credentials():
    try:
        credential = win32cred.CredRead(TARGET, win32cred.CRED_TYPE_GENERIC)
    except pywintypes.error as exc:
        if exc.winerror == 1168:
            raise ValueError("No Exness credential configured for this Windows user.") from None
        raise
    return json.loads(credential["CredentialBlob"].decode("utf-16-le"))


def configure(account, gateway, api_key, private_key):
    if not re.fullmatch(r"[0-9]{1,20}", account):
        raise ValueError("Enter the numeric trading account ID.")
    gateway = gateway.strip().rstrip("/")
    gateway = gateway if "://" in gateway else "https://" + gateway
    parsed = urlsplit(gateway)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("Enter the HTTPS Gateway origin from Key Management.")
    if not api_key or not re.fullmatch(r"[0-9a-fA-F]{128}", private_key):
        raise ValueError("Enter the key ID and Exness-generated 128-character signing key.")
    raw = bytes.fromhex(private_key)
    if Ed25519PrivateKey.from_private_bytes(raw[:32]).public_key().public_bytes_raw() != raw[32:]:
        raise ValueError("Invalid signing key.")
    payload = dict(account_id=account, gateway=gateway, api_key=api_key, private_key=private_key)
    win32cred.CredWrite({
        "Type": win32cred.CRED_TYPE_GENERIC, "TargetName": TARGET, "UserName": account,
        "CredentialBlob": json.dumps(payload), "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
    }, 0)


if __name__ == "__main__":
    configure(input("Exness trading account ID: ").strip(), input("Gateway hostname: ").strip(),
              getpass.getpass("Key ID: ").strip(), getpass.getpass("Signing key: ").strip())
    print("Saved to Windows Credential Manager for the current Windows user.")
