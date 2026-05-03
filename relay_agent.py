#!/usr/bin/env python3
"""
Gist Tunnel Relay Agent - runs on GitHub Actions runner
Polls command gist, fetches URLs via TCP, writes response to repo file
Uses repo file API instead of gist PATCH to avoid gist rate limits
"""
import requests, json, time, base64, sys, os, socket

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

command_gist = os.environ['COMMAND_GIST_ID']
response_gist = os.environ['RESPONSE_GIST_ID']
# Use GITHUB_TOKEN (default Actions token) - has repo scope, higher rate limits
token = os.environ.get('GITHUB_TOKEN', os.environ.get('GIST_PAT', ''))

log(f'COMMAND_GIST_ID: {command_gist}')
log(f'RESPONSE_GIST_ID: {response_gist}')
log(f'Using token: {"GITHUB_TOKEN" if "GITHUB_TOKEN" in os.environ else "GIST_PAT"}')

# For gist API calls, use GIST_PAT if available
gist_token = os.environ.get('GIST_PAT', token)

gist_session = requests.Session()
gist_session.headers.update({
    'Authorization': f'token {gist_token}',
    'Accept': 'application/vnd.github.v3+json',
    'User-Agent': 'gist-tunnel-relay/1.0'
})

# For repo API calls, use GITHUB_TOKEN (has repo scope)
repo_session = requests.Session()
repo_session.headers.update({
    'Authorization': f'token {token}',
    'Accept': 'application/vnd.github.v3+json',
    'User-Agent': 'gist-tunnel-relay/1.0'
})

REPO = 'masoudshafiee/web-proxy'
RESPONSE_FILE = 'response_data.json'

def get_command():
    try:
        r = gist_session.get(f'https://api.github.com/gists/{command_gist}', timeout=15)
        if r.status_code == 403:
            log(f'403 on GET gist: {r.text[:100]}')
            time.sleep(5)
            return None
        r.raise_for_status()
        gist = r.json()
        if 'command.json' not in gist.get('files', {}):
            return None
        raw_url = gist['files']['command.json']['raw_url']
        r = gist_session.get(raw_url, timeout=15)
        r.raise_for_status()
        data = r.json()
        if not data.get('id'):
            return None
        return data
    except Exception as e:
        log(f'get_command error: {e}')
        return None

def send_response_via_repo(job_id, response_b64):
    """Send response by writing to a file in the repo using GitHub API."""
    try:
        payload = json.dumps({'id': job_id, 'response': response_b64})
        
        # Try to get existing file SHA first
        sha = None
        r = repo_session.get(
            f'https://api.github.com/repos/{REPO}/contents/{RESPONSE_FILE}',
            timeout=15
        )
        if r.status_code == 200:
            sha = r.json().get('sha')
        
        # Create/update file
        data = {
            'message': f'Response {job_id}',
            'content': base64.b64encode(payload.encode()).decode(),
            'branch': 'main'
        }
        if sha:
            data['sha'] = sha
        
        r = repo_session.put(
            f'https://api.github.com/repos/{REPO}/contents/{RESPONSE_FILE}',
            json=data,
            timeout=30
        )
        log(f'Repo PUT status: {r.status_code}')
        if r.status_code >= 400:
            log(f'Repo PUT error: {r.text[:200]}')
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
    send_response_via_repo(job_id, response_b64)
    log(f'Job {job_id} completed: {len(response)} bytes')
    time.sleep(1)
