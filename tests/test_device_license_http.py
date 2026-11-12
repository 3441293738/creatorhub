from fastapi.testclient import TestClient
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from app.licensing import LicenseClient
from license_service.core import Store, create_app


def test_http_signed_activation_unbind_and_switch(tmp_path):
    key = Ed25519PrivateKey.generate()
    store = Store(tmp_path / 'licenses.db', key, clock=lambda: 10000)
    issued = store.create(20000)
    http = TestClient(create_app(store))
    def request(path, body):
        response = http.post(path, json=body)
        response.raise_for_status()
        return response.json()
    def client(device):
        saved = {}
        return LicenseClient(device=lambda: device * 64, timer=lambda: (1000, 1000),
            request=request, public_key=key.public_key(), read=lambda k: saved.get(k, ''),
            write=lambda k, v: saved.update({k: v}))
    first, second = client('a'), client('b')
    assert first.validate(issued['code'])['valid']
    assert first.validate(issued['code'])['valid']
    assert not second.validate(issued['code'])['valid']
    store.admin('unbind', issued['license_id'])
    assert second.validate(issued['code'])['valid']
    assert not first.validate()['valid']
    store.admin('disable', issued['license_id'])
    assert not second.validate()['valid']
    assert http.get('/health').json() == {'status': 'ok'}
