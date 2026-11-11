"""Operator-only offline issuer. Never distribute private keys."""
import argparse
import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

p = argparse.ArgumentParser()
s = p.add_subparsers(dest='command', required=True)
g = s.add_parser('keygen'); g.add_argument('--private', required=True); g.add_argument('--public', required=True)
i = s.add_parser('issue'); i.add_argument('--private', required=True); i.add_argument('--customer', required=True); i.add_argument('--expires', required=True)
a = p.parse_args()
if a.command == 'keygen':
    private, public = Path(a.private), Path(a.public)
    if private.exists() or public.exists():
        p.error('Refusing to overwrite an existing key')
    key = Ed25519PrivateKey.generate()
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
else:
    expires = datetime.fromisoformat(a.expires.replace('Z', '+00:00'))
    if expires.tzinfo is None:
        p.error('--expires must include timezone, e.g. 2027-01-01T00:00:00Z')
    raw = json.dumps(dict(product='huoke-assistant-v1', customer=a.customer, issued=int(datetime.now(timezone.utc).timestamp()), expires=int(expires.timestamp())), separators=(',', ':')).encode()
    key = serialization.load_pem_private_key(Path(a.private).read_bytes(), password=None)
    print(base64.urlsafe_b64encode(raw).decode() + '.' + base64.urlsafe_b64encode(key.sign(raw)).decode())
