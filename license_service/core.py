import base64, hashlib, json, secrets, sqlite3, time
from contextlib import contextmanager
from fastapi import FastAPI
from pydantic import BaseModel, Field
from cryptography.hazmat.primitives.serialization import load_pem_private_key

def digest(value): return hashlib.sha256(value.encode()).hexdigest()
def b64(value): return base64.urlsafe_b64encode(value).decode().rstrip('=')

class Store:
    def __init__(self, path, key, clock=time.time):
        self.path, self.key, self.clock = str(path), key, clock
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS licenses (id TEXT PRIMARY KEY, code_hash TEXT UNIQUE, expires REAL, enabled INTEGER, device TEXT, version INTEGER, credential TEXT)')
    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path, timeout=30)
        db.row_factory=sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()
    def create(self, expires):
        code=secrets.token_urlsafe(32); lid=secrets.token_hex(12)
        with self.connect() as db: db.execute('INSERT INTO licenses VALUES (?,?,?,1,NULL,0,NULL)',(lid,digest(code),expires))
        return {'license_id':lid,'code':code}
    def admin(self, action, lid, expires=None):
        with self.connect() as db:
            if action=='renew': db.execute('UPDATE licenses SET expires=? WHERE id=?',(expires,lid))
            elif action=='disable': db.execute('UPDATE licenses SET enabled=0,version=version+1 WHERE id=?',(lid,))
            elif action=='unbind': db.execute('UPDATE licenses SET device=NULL,credential=NULL,version=version+1 WHERE id=?',(lid,))
            row=db.execute('SELECT id,expires,enabled,device,version FROM licenses WHERE id=?',(lid,)).fetchone()
            if row is None: raise ValueError('Unknown license')
            return dict(row)
    def respond(self, action, value, device, nonce):
        now=self.clock(); credential=value if action=='check' else None
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            column='code_hash' if action=='activate' else 'credential'
            row=db.execute(f'SELECT * FROM licenses WHERE {column}=?',(digest(value) if action=='activate' else value,)).fetchone()
            state='invalid' if action=='activate' else 'revoked'
            if row:
                if not row['enabled']: state='disabled'
                elif now>=row['expires']: state='expired'
                elif row['device'] and row['device']!=device: state='bound' if action=='activate' else 'device_mismatch'
                else:
                    state='active'
                    if action=='activate':
                        credential=row['credential'] or secrets.token_urlsafe(32)
                        # Same-device activation returns the existing credential and binding generation.
                        version=row['version']+(row['device'] is None)
                        db.execute('UPDATE licenses SET device=?,version=?,credential=? WHERE id=?',(device,version,credential,row['id']))
                        row=db.execute('SELECT * FROM licenses WHERE id=?',(row['id'],)).fetchone()
            payload=dict(protocol=1,status=state,license_id=row['id'] if row else '',device=device,binding_version=row['version'] if row else 0,server_time=now,expires_at=row['expires'] if row else 0,valid_until=min(now+1800,row['expires']) if row and state=='active' else now,nonce=nonce)
            if state=='active': payload['credential']=credential
        raw=json.dumps(payload,sort_keys=True,separators=(',',':')).encode()
        return dict(payload=b64(raw),signature=b64(self.key.sign(raw)))

class Request(BaseModel):
    device: str=Field(pattern=r'^[0-9a-f]{64}$')
    nonce: str=Field(min_length=16,max_length=128)
class Activation(Request):
    code: str=Field(min_length=1,max_length=256)
class Check(Request):
    credential: str=Field(min_length=1,max_length=256)
def create_app(store):
    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
    @app.get('/health')
    def health(): return {'status':'ok'}
    @app.post('/v1/activate')
    def activate(r:Activation): return store.respond('activate',r.code,r.device,r.nonce)
    @app.post('/v1/check')
    def check(r:Check): return store.respond('check',r.credential,r.device,r.nonce)
    return app
