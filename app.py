#!/usr/bin/env python3
"""
تولید جفت‌کلید X25519 برای Reality — دقیقا هم‌فرمت با خروجی `xray x25519`.
نیاز: pip install cryptography

اجرا:
    python3 gen_reality_keys.py
"""
import base64
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


def b64url_nopad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def main():
    priv = X25519PrivateKey.generate()
    pub = priv.public_key()

    priv_bytes = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_bytes = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    print("REALITY_PRIVATE_KEY =", b64url_nopad(priv_bytes))
    print("REALITY_PUBLIC_KEY  =", b64url_nopad(pub_bytes))
    print()
    print("پیشنهاد Short ID (بدم به REALITY_SHORT_IDS):")
    print(" -", os.urandom(8).hex())


if __name__ == "__main__":
 
    main()
