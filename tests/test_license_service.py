import base64,json,tempfile,unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from license_service.core import Store,create_app

def decode(value): return base64.urlsafe_b64decode(value+'='*(-len(value)%4))
class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.now=1000; self.key=Ed25519PrivateKey.generate()
        self.store=Store(Path(self.tmp.name)/'db',self.key,lambda:self.now)
        self.issued=self.store.create(5000)
    def request(self,action,value,device='a'*64):
        envelope=self.store.respond(action,value,device,'nonce-1234567890123456')
        raw=decode(envelope['payload']); self.key.public_key().verify(decode(envelope['signature']),raw)
        result=json.loads(raw); self.assertEqual(result['nonce'],'nonce-1234567890123456'); return result
    def test_activation_idempotency_and_cross_device(self):
        first=self.request('activate',self.issued['code']); second=self.request('activate',self.issued['code'])
        self.assertEqual(first,second); self.assertEqual(first['valid_until'],2800)
        self.assertEqual(self.request('activate',self.issued['code'],'b'*64)['status'],'bound')
        self.assertEqual(self.request('check',first['credential'])['status'],'active')
        self.assertEqual(self.request('check',first['credential'],'b'*64)['status'],'device_mismatch')
    def test_concurrent_binding(self):
        with ThreadPoolExecutor(2) as pool: results=list(pool.map(lambda d:self.request('activate',self.issued['code'],d),['a'*64,'b'*64]))
        self.assertEqual(sorted(r['status'] for r in results),['active','bound'])
    def test_expiration_renew_disable(self):
        active=self.request('activate',self.issued['code']); self.now=5000
        self.assertEqual(self.request('check',active['credential'])['status'],'expired')
        self.store.admin('renew',self.issued['license_id'],5100)
        self.assertEqual(self.request('check',active['credential'])['valid_until'],5100)
        self.store.admin('disable',self.issued['license_id'])
        self.assertEqual(self.request('check',active['credential'])['status'],'disabled')
    def test_unbind_replace_and_revoke(self):
        old=self.request('activate',self.issued['code']); self.store.admin('unbind',self.issued['license_id'])
        self.assertEqual(self.request('check',old['credential'])['status'],'revoked')
        new=self.request('activate',self.issued['code'],'b'*64)
        self.assertGreater(new['binding_version'],old['binding_version'])
        self.assertEqual(self.request('check',old['credential'])['status'],'revoked')
    def test_http_validation_and_no_admin_api(self):
        client=TestClient(create_app(self.store))
        self.assertEqual(client.get('/health').json(),{'status':'ok'})
        self.assertEqual(client.post('/v1/activate',json={'code':'x','device':'invalid','nonce':'short'}).status_code,422)
        self.assertEqual(client.post('/admin/unbind').status_code,404)
        response=client.post('/v1/activate',json={'code':self.issued['code'],'device':'a'*64,'nonce':'0123456789abcdef'})
        self.assertEqual(response.status_code,200)
    def test_codes_not_stored_plaintext(self):
        with self.store.connect() as db: self.assertNotEqual(db.execute('SELECT code_hash FROM licenses').fetchone()[0],self.issued['code'])
if __name__=='__main__': unittest.main()
