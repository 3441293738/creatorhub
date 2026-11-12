import json
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from app.licensing import LicenseClient, KEY
from license_service.core import Store

@pytest.fixture
def env(tmp_path):
    key=Ed25519PrivateKey.generate(); stamp=[1000,1000]; saved={}
    server=Store(tmp_path/'db',key,clock=lambda:10000)
    issued=server.create(20000)
    def request(path,body):
        action='activate' if path.endswith('activate') else 'check'
        return server.respond(action,body.get('code',body.get('credential')),body['device'],body['nonce'])
    client=LicenseClient(device=lambda:'a'*64,timer=lambda:tuple(stamp),request=request,public_key=key.public_key(),read=lambda k:saved.get(k,''),write=lambda k,v:saved.update({k:v}))
    return client,server,issued,stamp,saved,request

def test_activation_deadline_and_retries(env):
    c,s,i,t,_,_=env
    assert c.validate(i['code'])['valid']
    deadline=c.deadline
    c.request=lambda *a: (_ for _ in ()).throw(OSError())
    t[:]=[1100,1100]
    assert c.validate()['valid']
    assert c.deadline==deadline
    t[:]=[2800,2800]
    assert not c.validate()['valid']

def test_restart_and_copied_credential(env):
    c,s,i,t,saved,request=env
    c.validate(i['code'])
    other=LicenseClient(device=lambda:'b'*64,timer=lambda:tuple(t),request=request,public_key=s.key.public_key(),read=lambda k:saved[k],write=lambda *a:None)
    assert not other.status()['valid']
    assert not other.validate()['valid']

def test_revocation_and_rebind(env):
    c,s,i,*_=env
    c.validate(i['code']); s.admin('unbind',i['license_id'])
    assert not c.validate()['valid']
    assert c.status()['status']=='revoked'

def test_suspend_requires_online(env):
    c,s,i,t,_,request=env
    c.validate(i['code']); t[:]=[1500,1001]
    c.request=lambda *a: (_ for _ in ()).throw(OSError())
    assert not c.allowed()
    c.request=request
    assert c.allowed()

def test_replay_and_tampering(env):
    c,s,i,t,_,request=env
    captured=[]
    def capture(*args):
        r=request(*args);captured.append(r);return r
    c.request=capture;c.validate(i['code']); deadline=c.deadline
    c.request=lambda *a:captured[0]
    t[:]=[1100,1100]
    c.validate();assert c.deadline==deadline
    assert not c.validate(i['code'])['valid']

def test_bad_activation_does_not_use_old_cache(env):
    c,s,i,*_=env
    assert c.validate(i['code'])['valid']
    assert not c.validate('bad')['valid']

def test_expiry_bound(env):
    c,s,i,t,*_=env
    s.admin('renew',i['license_id'],10005)
    c.validate(i['code']);t[:]=[1005,1005]
    assert not c.allowed()
@pytest.mark.parametrize('field,value', [('nonce','wrong'),('device','b'*64),('binding_version',999),('license_id','wrong'),('credential','wrong')])
def test_signed_response_mismatch(env,field,value):
    import base64
    c,s,i,t,_,request=env
    c.validate(i['code']); deadline=c.deadline
    def wrong(*args):
        response=request(*args)
        data=json.loads(base64.urlsafe_b64decode(response['payload']+'=='))
        data[field]=value
        raw=json.dumps(data).encode()
        return dict(payload=base64.urlsafe_b64encode(raw).decode(),signature=base64.urlsafe_b64encode(s.key.sign(raw)).decode())
    c.request=wrong;t[:]=[1100,1100]
    assert c.validate()['status']=='offline'
    assert c.deadline==deadline

def test_signature_tamper(env):
    c,s,i,t,_,request=env
    def tamper(*args):
        r=request(*args);r['signature']='A'*86;return r
    c.request=tamper
    assert not c.validate(i['code'])['valid']

def test_wall_clock_irrelevant(env,monkeypatch):
    import time
    c,s,i,t,*_=env
    c.validate(i['code']);deadline=c.deadline
    for wall in (0, 99999999999):
        monkeypatch.setattr(time,'time',lambda:wall)
        assert c.status()['valid']
        assert c.deadline==deadline
    t[:]=[2800,2800]
    assert not c.status()['valid']

def test_offline_restart(env):
    c,s,i,t,saved,request=env
    c.validate(i['code'])
    offline=LicenseClient(device=c.device,timer=c.timer,request=lambda *a:(_ for _ in ()).throw(OSError()),public_key=s.key.public_key(),read=lambda k:saved[k],write=lambda *a:None)
    assert not offline.validate()['valid']

def test_device_read_failure(env):
    c,s,i,*_=env
    c.validate(i['code']);c.device=lambda:(_ for _ in ()).throw(OSError())
    assert not c.allowed()

@pytest.mark.skipif(__import__('os').name != 'nt', reason='Windows native API')
def test_windows_native_timer_and_device():
    from app.licensing import clock, device_digest
    import re
    before, after = clock(), clock()
    assert after[0] >= before[0] and after[1] >= before[1]
    digest = device_digest()
    assert re.fullmatch('[0-9a-f]{64}', digest)
    assert digest == device_digest()

def test_background_refresh_at_ten_minutes_and_cached_reads(env, monkeypatch):
    from app import licensing
    c,s,i,t,_,request=env
    c.validate(i['code'])
    calls=[]
    def counted(*args):
        calls.append(t[0]);return request(*args)
    c.request=counted
    class Event:
        def __init__(self): self.iteration=0
        def clear(self): pass
        def set(self): pass
        def wait(self, seconds):
            self.iteration+=1
            if self.iteration==1:
                t[:]=[1599,1599]
                for _ in range(20):
                    assert c.status()['valid']
                    assert c.allowed()
                assert calls==[1000]
                return False
            if self.iteration==2:
                assert calls==[1000]
                t[:]=[1600,1600]
                return False
            assert calls==[1000,1600]
            return True
    class Thread:
        def __init__(self,target,**kwargs): self.target=target
        def start(self): self.target()
        def is_alive(self): return False
        def join(self,**kwargs): pass
    c.event=Event()
    monkeypatch.setattr(licensing.threading,'Thread',Thread)
    c.start()
    assert calls==[1000,1600]
    c.stop()

def test_signed_disabled_immediately_revokes_grace(env):
    c,s,i,t,*_=env
    assert c.validate(i['code'])['valid']
    assert c.deadline>t[0]
    s.admin('disable',i['license_id'])
    state=c.validate()
    assert state['status']=='disabled'
    assert not state['valid']
    assert not c.allowed()
    assert c.deadline==0
