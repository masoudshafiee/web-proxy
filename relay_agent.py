#!/usr/bin/env python3
"""
Gist Tunnel Relay Agent - runs on GitHub Actions runner
Uses GitHub Issues API for command/response.
GET uses GITHUB_TOKEN (read-only), PATCH uses GIST_PAT (write).
"""
import requests, json, time, base64, sys, os, socket

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

github_token = os.environ.get('GITHUB_TOKEN', '')
gist_pat = os.environ.get('GIST_PAT', '')

log(f'GITHUB_TOKEN length: {len(github_token)} chars')
log(f'GIST_PAT length: {len(gist_pat)} chars')

# Read-only session (GITHUB_TOKEN)
read_session = requests.Session()
read_session.headers.update({
    'Authorization': f'token {github_token}',
    'Accept': 'application/vnd.github.v3+json',
    'User-Agent': 'gist-tunnel-relay/1.0'
})

# Write session (GIST_PAT)
write_session = requests.Session()
write_session.headers.update({
    'Authorization': f'token {gist_pat}',
    'Accept': 'application/vnd.github.v3+json',
    'User-Agent': 'gist-tunnel-relay/1.0'
})

REPO = 'masoudshafiee/web-proxy'
COMMAND_ISSUE = 1
RESPONSE_ISSUE = 2

def get_command():
    """Read command from Issue #1 body using GITHUB_TOKEN."""
    try:
        r = read_session.get(
            f'https://api.github.com/repos/{REPO}/issues/{COMMAND_ISSUE}',
            timeout=15
        )
        if r.status_code == 403:
            log(f'403 on GET issue: {r.text[:100]}')
            time.sleep(5)
            return None
        r.raise_for_status()
        body = r.json().get('body', '')
        if not body:
            return None
        data = json.loads(body)
        if not data.get('id'):
            return None
        return data
    except Exception as e:
        log(f'get_command error: {e}')
        return None

def send_response(job_id, response_b64):
    """Write response to Issue #2 body using GIST_PAT."""
    try:
        payload = json.dumps({'id': job_id, 'response': response_b64})
        r = write_session.patch(
            f'https://api.github.com/repos/{REPO}/issues/{RESPONSE_ISSUE}',
            json={'body': payload},
            timeout=30
        )
        log(f'Issue PATCH status: {r.status_code}')
        if r.status_code >= 400:
            log(f'Issue PATCH error: {r.text[:200]}')
        else:
            log(f'Response sent for {job_id}: {len(response_b64)} bytes')
    except Exception as e:
        log(f'send_response error: {e}')

processed = set()
log('Runner started, polling...')
while True:
    cmd = get_command()
    if not cmd:
        time.sleep(2)
        continue
    job_id = cmd['id']
    if job_id in processed:
        time.sleep(2)
        continue
    processed.add(job_id)
    host = cmd['host']
    port = int(cmd['port'])
    payload_b64 = cmd.get('payload') or ''
    payload = base64.b64decode(payload_b64) if payload_b64 else b''

    log(f'Processing job {job_id}: {host}:{port}')

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(30)
    response = b''
    try:
        sock.settimeout(10)
        sock.connect((host, port))
        log(f'Connected to {host}:{port}')
        if payload:
            sock.sendall(payload)
            log(f'Sent {len(payload)} bytes payload')
        sock.settimeout(2)
        while True:
            try:
                chunk = sock.recv(8192)
                if not chunk:
                    break
                response += chunk
                if len(response) > 2_000_000:
                    break
            except socket.timeout:
                if len(response) > 0:
                    break
                else:
                    continue
        log(f'Received {len(response)} bytes from {host}:{port}')
    except Exception as e:
        response = f'ERROR:{e}'.encode()
        log(f'Connection error: {e}')
    finally:
        sock.close()

    response_b64 = base64.b64encode(response).decode()
    send_response(job_id, response_b64)
    log(f'Job {job_id} completed: {len(response)} bytes')
    time.sleep(1)
