#!/usr/bin/env python3
"""
Gist Tunnel - Local HTTP/HTTPS Proxy
Uses 'gh api' with GitHub Issues API for command/response.
Acts as a transparent HTTP proxy on 127.0.0.1:8080
Supports both HTTP and HTTPS CONNECT tunneling.
"""
import subprocess, json, time, base64, uuid, sys, os, threading, socket as sock_mod
import http.server, socketserver, urllib.parse, select, tempfile

# ===== CONFIGURATION =====
REPO = "masoudshafiee/web-proxy"
COMMAND_ISSUE = 1  # Issue #1 for commands
RESPONSE_ISSUE = 2  # Issue #2 for responses
LISTEN_PORT = 8080
POLL_INTERVAL = 1.5
MAX_RESPONSE_SIZE = 2_000_000
# =========================

def gh_api(method, endpoint, data=None):
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
                try: os.unlink(temp_path)
                except: pass
            if result.returncode == 0:
                return json.loads(result.stdout)
            if attempt < 2:
                time.sleep(2 ** attempt)
        except Exception as e:
            if temp_path:
                try: os.unlink(temp_path)
                except: pass
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"gh api error after 3 retries")

def push_command(job_id, host, port, payload=b''):
    """Write command to Issue #1 body."""
    payload_b64 = base64.b64encode(payload).decode()
    content = json.dumps({
        "id": job_id,
        "host": host,
        "port": port,
        "payload": payload_b64
    })
    gh_api("PATCH", f"/repos/{REPO}/issues/{COMMAND_ISSUE}", data={"body": content})

def get_response(job_id, timeout_sec=60):
    """Poll Issue #2 for response matching our job_id."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            result = gh_api("GET", f"/repos/{REPO}/issues/{RESPONSE_ISSUE}")
            body = result.get("body", "")
            if body:
                resp = json.loads(body)
                if resp.get("id") == job_id:
                    resp_b64 = resp.get("response", "")
                    if resp_b64:
                        return base64.b64decode(resp_b64)
                    return b""
        except Exception as e:
            pass
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"No response for job {job_id} within {timeout_sec}s")

def fetch_via_tunnel(host, port, payload=b''):
    job_id = f"req-{uuid.uuid4().hex[:8]}"
    push_command(job_id, host, port, payload)
    return get_response(job_id)

class TunnelProxyHandler(http.server.BaseHTTPRequestHandler):
    """HTTP Proxy handler that forwards requests through tunnel."""
    
    def do_CONNECT(self):
        """Handle HTTPS CONNECT method."""
        try:
            host, port_str = self.path.split(':')
            port = int(port_str)
        except (ValueError, IndexError):
            self.send_error(400, "Bad CONNECT request")
            return
        
        print(f"[CONNECT] {host}:{port}")
        
        try:
            self.send_response(200, "Connection Established")
            self.end_headers()
            
            client = self.connection
            client.settimeout(15)
            
            while True:
                try:
                    data = client.recv(65536)
                    if not data:
                        break
                    
                    job_id = f"rel-{uuid.uuid4().hex[:8]}"
                    push_command(job_id, host, port, payload=data)
                    response = get_response(job_id, timeout_sec=30)
                    
                    if response:
                        client.sendall(response)
                    else:
                        break
                        
                except socket.timeout:
                    break
                except TimeoutError:
                    break
                except Exception as e:
                    print(f"[CONNECT] relay error: {e}")
                    break
            
            try:
                client.close()
            except:
                pass
                
        except Exception as e:
            print(f"[CONNECT] error: {e}")
            try:
                self.send_error(502, str(e))
            except:
                pass
    
    def _handle_http_request(self, method):
        """Handle regular HTTP requests."""
        parsed = urllib.parse.urlparse(self.path)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        
        if not host:
            self.send_error(400, "No host in URL")
            return
        
        path = parsed.path or '/'
        if parsed.query:
            path += '?' + parsed.query
        
        raw_request = f"{method} {path} HTTP/1.1\r\n".encode()
        for key, value in self.headers.items():
            if key.lower() not in ('proxy-connection', 'proxy-authorization'):
                raw_request += f"{key}: {value}\r\n".encode()
        raw_request += b"\r\n"
        
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length > 0:
            body = self.rfile.read(content_length)
            raw_request += body
        
        try:
            response_data = fetch_via_tunnel(host, port, raw_request)
            if response_data:
                self.connection.sendall(response_data)
            else:
                self.send_error(502, "Empty response from tunnel")
        except Exception as e:
            print(f"[HTTP] error {method} {host}:{port}: {e}")
            try:
                self.send_error(502, str(e))
            except:
                pass
    
    def do_GET(self): self._handle_http_request('GET')
    def do_POST(self): self._handle_http_request('POST')
    def do_PUT(self): self._handle_http_request('PUT')
    def do_DELETE(self): self._handle_http_request('DELETE')
    def do_HEAD(self): self._handle_http_request('HEAD')
    def do_OPTIONS(self): self._handle_http_request('OPTIONS')
    def do_PATCH(self): self._handle_http_request('PATCH')
    
    def log_message(self, format, *args):
        pass

def main():
    try:
        result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            print("ERROR: 'gh' CLI not authenticated. Run 'gh auth login' first.")
            sys.exit(1)
    except FileNotFoundError:
        print("ERROR: 'gh' CLI not found. Install GitHub CLI first.")
        sys.exit(1)
    
    # Verify issues exist
    try:
        gh_api("GET", f"/repos/{REPO}/issues/{COMMAND_ISSUE}")
        print(f"[+] Issue #{COMMAND_ISSUE} accessible")
    except Exception as e:
        print(f"[-] Cannot access Issue #{COMMAND_ISSUE}: {e}")
        sys.exit(1)
    
    print(f"[*] Starting Tunnel Proxy on 127.0.0.1:{LISTEN_PORT}")
    print(f"[*] Set your browser/proxy to HTTP proxy 127.0.0.1:{LISTEN_PORT}")
    print(f"[*] Supports HTTP + HTTPS (CONNECT)")
    print(f"[*] Press Ctrl+C to stop")
    
    class ThreadedHTTPServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True
    
    server = ThreadedHTTPServer(('127.0.0.1', LISTEN_PORT), TunnelProxyHandler)
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Shutting down...")
        server.shutdown()

if __name__ == "__main__":
    main()
