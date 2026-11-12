# Internal license service

Use a dedicated `aispace-license` service account, `/opt/aispace-license` for code/venv and `/var/lib/aispace-license` mode 0700 for persistent data. Install `requirements.txt` into that venv. Copy the included unit to `/etc/systemd/system/aispace-license.service`.

Initialize once (never overwrite signing.pem):
```sh
cd /opt/aispace-license
sudo -u aispace-license venv/bin/python -m license_service.cli init
sudo systemctl daemon-reload
sudo systemctl enable --now aispace-license
```
Copy **only public.pem** into the client's trusted public-key configuration. Never package signing.pem or licenses.sqlite3. Production must replace test keys and use HTTPS; HTTP internal testing does not provide confidentiality.

Local administration, from `/opt/aispace-license`:
```sh
sudo -u aispace-license venv/bin/python -m license_service.cli create --expires 2026-12-31T23:59:59Z
sudo -u aispace-license venv/bin/python -m license_service.cli status --id LICENSE_ID
sudo -u aispace-license venv/bin/python -m license_service.cli renew --id LICENSE_ID --expires 2027-01-31T23:59:59Z
sudo -u aispace-license venv/bin/python -m license_service.cli disable --id LICENSE_ID
sudo -u aispace-license venv/bin/python -m license_service.cli unbind --id LICENSE_ID
journalctl -u aispace-license -n 100 --no-pager
sudo systemctl stop aispace-license
```
Create prints the code once. Protect that terminal output. No admin HTTP routes exist. Access logging is disabled by the unit; do not enable request-body logging in a proxy.

Unbind increments binding generation and removes the previous bearer credential. Same-device activation returns the existing credential. Disable immediately rejects subsequent checks. Existing signed runtime permission can last up to its original 30-minute/expiry deadline if offline. Renewal does not re-enable disabled licenses; issue a new code if needed.

Rollback: stop the unit, restore the previous code directory/version while retaining the protected persistent directory and restart. No legacy license migration is supported or required. Before opening port 8088, limit its security-group/firewall source to test public IPs and preserve SSH. Confirm NTP and `/health` from the test machine.

## First installation commands (after SSH/preflight succeeds)
These commands have not been executed on the target server. Upload only the license_service source directory to a staging directory first.
```sh
cat /etc/os-release
sudo ss -lntp
sudo ufw status verbose
timedatectl status
sudo systemctl list-units --type=service --state=running --no-pager
# Stop if either target directory or service belongs to an existing installation.
sudo test ! -e /opt/aispace-license
sudo test ! -e /var/lib/aispace-license
sudo useradd --system --user-group --home-dir /var/lib/aispace-license --shell /usr/sbin/nologin aispace-license
sudo install -d -m 0755 /opt/aispace-license
sudo install -d -m 0700 -o aispace-license -g aispace-license /var/lib/aispace-license
sudo cp -R ~/aispace-license-stage/license_service /opt/aispace-license/
sudo python3 -m venv /opt/aispace-license/venv
sudo /opt/aispace-license/venv/bin/pip install -r /opt/aispace-license/license_service/requirements.txt
cd /opt/aispace-license
sudo -u aispace-license venv/bin/python -m license_service.cli init
sudo install -m 0644 license_service/aispace-license.service /etc/systemd/system/aispace-license.service
sudo systemctl daemon-reload
sudo systemctl enable --now aispace-license
curl --fail http://127.0.0.1:8088/health
sudo systemctl is-enabled aispace-license
sudo systemctl is-active aispace-license
```
If python3-venv is unavailable, install that OS package after inspecting the OS. Do not change firewall until its current rules and SSH path are confirmed. Permit TCP8088 only from tester public /32 addresses. Do not overwrite pre-existing directories or keys.
