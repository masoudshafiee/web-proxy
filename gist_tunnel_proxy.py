#!/usr/bin/env python3
"""
Gist Tunnel Proxy - GitHub Actions Relay
Use GitHub Actions as a free proxy/relay to access blocked sites.
Runner fetches URLs and returns results via Gist API.
"""
import json, base64, time, sys, os, subprocess, tempfile, uuid

# Configuration
COMMAND_GIST_ID = "5e6abed0b61ab902b6efd837e57cd3e2"
GH_TOKEN = os.environ.get("GH_TOKEN", "")

def gh_api(method, endpoint, data=None):
    """Run gh CLI command to interact with GitHub API"""
    cmd = ["gh", "api", "--method", method, endpoint]
    temp_path = None
    
    if data:
        fd, temp_path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, 'w') as f:
            f.write(json.dumps(data))
        cmd.extend(["--input", temp_path])
    
    for attempt in range(3):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if temp_path:
                os.unlink(temp_path)
            if result.returncode == 0:
                return json.loads(result.stdout)
            if attempt < 2:
                time.sleep(2 ** attempt)
        except Exception as e:
            if temp_path:
                os.unlink(temp_path)
            if attempt < 2:
                time.sleep(2 ** attempt)
    
    print(f"gh api error after 3 retries: {result.stderr[:200] if 'result' in dir() else 'timeout'}")
    return None

def send_command(job_id, host, port, payload_b64=""):
    """Send a command to the Gist for the Runner to process"""
    command = {
        "id": job_id,
        "host": host,
        "port": port,
        "payload": payload_b64
    }
    
    data = {
        "files": {
            "command.json": {
                "content": json.dumps(command)
            }
        }
    }
    
    result = gh_api("PATCH", f"gists/{COMMAND_GIST_ID}", data)
    if result:
        print(f"[+] Command sent: {host}:{port} (job: {job_id})")
        return True
    return False

def wait_for_response(job_id, timeout=120):
    """Wait for the Runner to process and return response"""
    start = time.time()
    while time.time() - start < timeout:
        result = gh_api("GET", f"gists/{COMMAND_GIST_ID}")
        if result and "files" in result:
            files = result["files"]
            if "response.json" in files:
                try:
                    resp_content = json.loads(files["response.json"]["content"])
                    if resp_content.get("id") == job_id:
                        resp_b64 = resp_content.get("response", "")
                        if resp_b64:
                            resp_data = base64.b64decode(resp_b64).decode('utf-8', errors='replace')
                            print(f"[+] Response received ({len(resp_data)} bytes)")
                            return resp_data
                        else:
                            print("[!] Empty response")
                            return ""
                except:
                    pass
        
        time.sleep(3)
    
    print(f"[-] Timeout waiting for response (job: {job_id})")
    return None

def fetch_url(url, timeout=120):
    """Fetch a URL through the Gist Tunnel"""
    if url.startswith("https://"):
        host = url[8:].split("/")[0]
        port = 443
        path = "/" + "/".join(url[8:].split("/")[1:])
    elif url.startswith("http://"):
        host = url[7:].split("/")[0]
        port = 80
        path = "/" + "/".join(url[7:].split("/")[1:])
    else:
        print(f"[-] Invalid URL: {url}")
        return None
    
    http_request = f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
    payload_b64 = base64.b64encode(http_request.encode()).decode()
    
    job_id = f"req-{uuid.uuid4().hex[:8]}"
    
    if not send_command(job_id, host, port, payload_b64):
        return None
    
    return wait_for_response(job_id, timeout)

def main():
    if len(sys.argv) < 2:
        print("Usage: python gist_tunnel_proxy.py <url>")
        print("Example: python gist_tunnel_proxy.py https://google.com")
        sys.exit(1)
    
    url = sys.argv[1]
    print(f"[*] Fetching: {url}")
    print("[*] This may take 10-30 seconds...")
    
    response = fetch_url(url)
    
    if response:
        if "\r\n\r\n" in response:
            headers, body = response.split("\r\n\r\n", 1)
            print(f"\n=== HTTP Headers ===")
            for line in headers.split("\r\n"):
                print(f"  {line}")
            print(f"\n=== Body ({len(body)} bytes) ===")
            print(body[:2000])
            if len(body) > 2000:
                print(f"... (truncated, {len(body)} bytes total)")
        else:
            print(response[:2000])

if __name__ == "__main__":
    main()
