import os
from pathlib import Path
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from .core import Store,create_app
root=Path(os.environ.get('LICENSE_DATA','/var/lib/aispace-license'))
app=create_app(Store(root/'licenses.sqlite3',load_pem_private_key((root/'signing.pem').read_bytes(),password=None)))
