"""Block signature providers: public Ed25519 or shared-secret HMAC-SHA256."""
import hashlib
import hmac
import os
from pathlib import Path
import secrets

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ED25519 = 'ed25519'
HMAC_SHA256 = 'hmac-sha256'
MIN_SECRET_BYTES = 32


class SignerConfigError(ValueError):
    pass


class Ed25519Verifier:
    """Needs only the public key, so anyone holding the chain can check it."""
    algorithm = ED25519

    def __init__(self, public_key: str):
        self.public_key = public_key
        raw = bytes.fromhex(public_key)
        self.fingerprint = hashlib.sha256(raw).hexdigest()
        self._public = Ed25519PublicKey.from_public_bytes(raw)

    def verify(self, digest: str, signature: str) -> bool:
        try:
            self._public.verify(bytes.fromhex(signature), bytes.fromhex(digest))
            return True
        except (InvalidSignature, ValueError):
            return False


class Ed25519Signer(Ed25519Verifier):
    def __init__(self, seed: bytes):
        self._private = Ed25519PrivateKey.from_private_bytes(seed)
        super().__init__(self._private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex())

    def sign(self, digest: str) -> str:
        return self._private.sign(bytes.fromhex(digest)).hex()


class HmacSigner:
    """No new key material leaves the server, but every verifier must hold the secret."""
    algorithm = HMAC_SHA256
    public_key = None

    def __init__(self, secret: bytes):
        if len(secret) < MIN_SECRET_BYTES:
            raise SignerConfigError('Chain HMAC secret must contain at least 32 bytes')
        self._secret = secret
        self.fingerprint = hmac.new(secret, b'oguzki-chain-key-fingerprint', hashlib.sha256).hexdigest()

    def sign(self, digest: str) -> str:
        return hmac.new(self._secret, bytes.fromhex(digest), hashlib.sha256).hexdigest()

    def verify(self, digest: str, signature: str) -> bool:
        try:
            return hmac.compare_digest(self.sign(digest), signature)
        except (ValueError, TypeError):
            return False


def describe(key) -> dict:
    """Public description stored in genesis and key.rotated blocks; never secret material."""
    return {'algorithm': key.algorithm, 'public_key': key.public_key, 'fingerprint': key.fingerprint}


def secret_keyring() -> dict:
    values = [os.environ.get('COPILOT_CHAIN_SECRET', '')]
    values += os.environ.get('COPILOT_CHAIN_PREVIOUS_SECRETS', '').split(',')
    signers = [HmacSigner(value.encode()) for value in values if value]
    return {signer.fingerprint: signer for signer in signers}


def verifier_for(description: dict):
    """Return a verifier for a stored key, or None when a shared secret is not available."""
    if description.get('algorithm') == ED25519:
        verifier = Ed25519Verifier(description['public_key'])
        if verifier.fingerprint != description.get('fingerprint'):
            raise SignerConfigError('Public key does not match its fingerprint')
        return verifier
    if description.get('algorithm') == HMAC_SHA256:
        return secret_keyring().get(description.get('fingerprint'))
    raise SignerConfigError('Unknown chain signature algorithm')


def write_new_key(path: Path) -> Ed25519Signer:
    """Create an owner-readable seed; never overwrite a key that signed existing blocks."""
    seed = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='ascii') as stream:
        stream.write(seed.hex() + '\n')
    return Ed25519Signer(seed)


def load_signer(key_file: Path, *, create: bool):
    """Resolve the configured signer. Returns None only when create is false and no key exists."""
    mode = os.environ.get('COPILOT_CHAIN_SIGNER', ED25519)
    if mode == 'hmac':
        secret = os.environ.get('COPILOT_CHAIN_SECRET', '')
        if not secret:
            raise SignerConfigError('COPILOT_CHAIN_SECRET is required for HMAC signing')
        return HmacSigner(secret.encode())
    if mode != ED25519:
        raise SignerConfigError('COPILOT_CHAIN_SIGNER must be ed25519 or hmac')
    configured = os.environ.get('COPILOT_CHAIN_PRIVATE_KEY', '')
    if configured:
        return Ed25519Signer(bytes.fromhex(configured))
    path = Path(os.environ.get('COPILOT_CHAIN_KEY_FILE') or key_file)
    if path.exists():
        return Ed25519Signer(bytes.fromhex(path.read_text(encoding='ascii').strip()))
    return write_new_key(path) if create else None
