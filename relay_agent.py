#!/usr/bin/env python3
"""
Gist Tunnel Relay Agent - runs on GitHub Actions runner
Polls command gist, fetches URLs via TCP, writes response to response gist
"""
import requests, json, time, base64, sys, os, socket

# Flush output immediately
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

command_gist = os.environ['COMMAND_GIST_ID']
response_gist = os.environ['RESPONSE_GIST_ID']
token = os.environ['GITHUB_TOKEN']
session = requests.Session()
session.headers.update({
    'Authorization': f'token {token}',
    'Accept': 'application/vnd.github.v3+json',
    'User-Agent': 'gist-tunnel-relay/1.0'
})

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def get_command():
    try:
        r = session.get(f'https://api.github.com/gists/{command_gist}', timeout=15)
        r.raise_for_status()
        gist = r.json()
        if 'command.json' not in gist.get('files', {}):
            return None
        raw_url = gist['files']['command.json']['raw_url']
        r = session.get(raw_url, timeout=15)
        r.raise_for_status()
        data = r.json()
        if not data.get('id'):
            return None
        return data
    except Exception as e:
        log(f'get_command error: {e}')
        return None

def send_response(job_id, response_b64):
    payload = {'id': job_id, 'response': response_b64}
    content = json.dumps(payload)
    patch_data = {'files': {'response.json': {'content': content}}}
    try:
        log(f'Sending response for {job_id}...')
        r = session.patch(f'https://api.github.com/gists/{response_gist}', json=patch_data, timeout=30)
        log(f'PATCH status: {r.status_code}')
        if r.status_code >= 400:
            log(f'PATCH error body: {r.text[:500]}')
        r.raise_for_status()
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

    # TCP connection
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
        # Read until connection closes or buffer full
        sock.settimeout(2)
        while True:
            try:
                chunk = sock.recv(8192)
                if not chunk:
                    break
                response += chunk
                if len(response) > 2_000_000:  # max 2MB
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
