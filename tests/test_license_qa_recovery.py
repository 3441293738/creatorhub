from app.licensing import LicenseClient

def test_activation_should_ignore_corrupt_saved_credential():
    called=[]
    client=LicenseClient(device=lambda:'a'*64,timer=lambda:(100,100),read=lambda k:'broken-json',write=lambda *a:None,request=lambda *a:called.append(a))
    client.validate('new-code')
    assert called, 'Explicit activation never reached server because saved credential JSON was corrupt'
