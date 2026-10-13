import hashlib
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

HEADER_SIGN = "X-QU-Sign-Ed25519"
HEADER_KID = "X-QU-Kid"
HEADER_SHA = "X-QU-Sha256"


class Signer:
    """Ed25519 over the sha256 of an artifact. The private key is the 32-byte
    seed from the environment; the public key is what the app ships."""

    def __init__(self, seed_hex: str, kid: str):
        seed = bytes.fromhex(seed_hex)
        if len(seed) != 32:
            raise ValueError("QU_SIGNING_KEY_HEX must be 32 bytes in hex")
        self._key = Ed25519PrivateKey.from_private_bytes(seed)
        self.kid = kid

    @property
    def public_key_hex(self) -> str:
        raw = self._key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
        return raw.hex()

    def sign_digest(self, sha256_hex: str) -> str:
        return self._key.sign(bytes.fromhex(sha256_hex)).hex()

    def sign_file(self, path: Path) -> tuple[str, str]:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 16), b""):
                digest.update(chunk)
        sha = digest.hexdigest()
        return sha, self.sign_digest(sha)

    def headers_for(self, path: Path) -> dict:
        sha, signature = self.sign_file(path)
        return {HEADER_SHA: sha, HEADER_SIGN: signature, HEADER_KID: self.kid}

    def describe(self) -> dict:
        return {"kid": self.kid, "algorithm": "ed25519-sha256", "public_key": self.public_key_hex}
