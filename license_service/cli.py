import argparse,json,os
from datetime import datetime
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from .core import Store

def main():
    p=argparse.ArgumentParser(); p.add_argument('--data',default=os.getenv('LICENSE_DATA','/var/lib/aispace-license'))
    p.add_argument('action',choices=['init','create','status','renew','disable','unbind']); p.add_argument('--id'); p.add_argument('--expires')
    a=p.parse_args(); root=Path(a.data); keyfile=root/'signing.pem'
    if a.action=='init':
        root.mkdir(parents=True,exist_ok=True); os.chmod(root,0o700)
        key=Ed25519PrivateKey.generate()
        with keyfile.open('xb') as f: f.write(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        os.chmod(keyfile,0o600)
        (root/'public.pem').write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)); return
    key=serialization.load_pem_private_key(keyfile.read_bytes(),password=None); store=Store(root/'licenses.sqlite3',key)
    expires=None
    if a.action in ('create','renew'):
        if not a.expires: p.error('--expires is required (ISO8601 with UTC offset)')
        dt=datetime.fromisoformat(a.expires.replace('Z','+00:00'))
        if dt.tzinfo is None: p.error('--expires must include timezone')
        expires=dt.timestamp()
    if a.action!='create' and not a.id: p.error('--id is required')
    print(json.dumps(store.create(expires) if a.action=='create' else store.admin(a.action,a.id,expires)))
if __name__=='__main__': main()
