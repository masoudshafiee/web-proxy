#!/usr/bin/env python3
"""
Gist Tunnel - Local HTTP/HTTPS Proxy
Uses 'gh api' to communicate with GitHub Gists.
Acts as a transparent HTTP proxy on 127.0.0.1:8080
"""
import subprocess, json, time, base64, uuid, sys, os, threading, socket as sock_mod
import http.server, socketserver, urllib.parse, select

# ===== CONFIGURATION =====
COMMAND_GIST = "5e6abed0b61ab902b6efd837e57cd3e2"
RESPONSE_GIST = "5e6abed0b61ab902b6efd837e57cd3e2"  # same gist for simplicity
LISTEN_PORT = 8080
POLL_INTERVAL = 1.5  # seconds between polls
MAX_RESPONSE_SIZE = 2_000_000  # 2MB
# =========================

def gh_api(method, endpoint, data=None, raw=False):
    """Execute gh api command and return output."""
    cmd = ["gh", "api", "--method", method, endpoint]
    stdin_data = None
    if data is not None:
        stdin_data = json.dumps(data) if isinstance(data, dict) else data
    
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if stdin_data else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8'
    )
    out, err = proc.communicate(input=stdin_data)
    if proc.returncode != 0:
        raise RuntimeError(f"gh api error: {err.strip()[:200]}")
    if raw:
        return out.encode('utf-8') if isinstance(out, str) else out
    return out

def push_command(job_id, host, port, payload=b''):
    """Write a command to the Gist for the Runner to process."""
    payload_b64 = base64.b64encode(payload).decode()
    content = json.dumps({
        "id": job_id,
        "host": host,
        "port": port,
        "payload": payload_b64
    })
    patch_data = {"files": {"command.json": {"content": content}}}
    gh_api("PATCH", f"/gists/{COMMAND_GIST}", data=patch_data)

def get_response(job_id, timeout_sec=40):
    """Poll the response gist until our job_id appears or timeout."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            raw = gh_api("GET", f"/gists/{RESPONSE_GIST}/raw?file=response.json", raw=True)
            resp = json.loads(raw)
            if resp.get("id") == job_id:
                resp_b64 = resp.get("response", "")
                if resp_b64:
                    return base64.b64decode(resp_b64)
                return b""
        except Exception:
            pass
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"No response for job {job_id} within {timeout_sec}s")

def fetch_via_tunnel(host, port, payload=b''):
    """Send a TCP request through the Gist tunnel and return the response."""
    job_id = f"req-{uuid.uuid4().hex[:8]}"
    push_command(job_id, host, port, payload)
    return get_response(job_id)

class TunnelProxyHandler(http.server.BaseHTTPRequestHandler):
    """HTTP Proxy handler that forwards requests through Gist tunnel."""
    
    def do_CONNECT(self):
        """Handle HTTPS CONNECT method."""
        try:
            host, port_str = self.path.split(':')
            port = int(port_str)
        except (ValueError, IndexError):
            self.send_error(400, "Bad CONNECT request")
            return
        
        # Store target for potential reuse
        self.target_host = host
        self.target_port = port
        
        try:
            # First, establish connection by sending empty payload
            # This tells the runner to connect to the target
            job_id = f"con-{uuid.uuid4().hex[:8]}"
            push_command(job_id, host, port, b'')
            
            # Send 200 Connection Established to client
            self.send_response(200, "Connection Established")
            self.end_headers()
            
            # Now relay data bidirectionally
            self._relay_tunnel(job_id, host, port)
            
        except Exception as e:
            print(f"CONNECT error {host}:{port}: {e}")
            try:
                self.send_error(502, str(e))
            except:
                pass
    
    def _relay_tunnel(self, initial_job_id, host, port):
        """Relay data between client and remote via tunnel.
        
        Since each tunnel command opens a fresh TCP connection, we treat
        each request/response as a separate round-trip. This works for
        HTTP/1.1 pipelining and most HTTPS clients.
        """
        client = self.connection
        client.settimeout(10)
        
        while True:
            try:
                # Read data from client (TLS handshake, HTTP request, etc.)
                data = client.recv(65536)
                if not data:
                    break
                
                # Send through tunnel with a new job
                job_id = f"rel-{uuid.uuid4().hex[:8]}"
                push_command(job_id, host, port, payload=data)
                
                # Get response
                response = get_response(job_id)
                if response:
                    client.sendall(response)
                else:
                    break
                    
            except socket.timeout:
                # No more data from client, close tunnel
                break
            except TimeoutError:
                print(f"Tunnel timeout for {host}:{port}")
                break
            except Exception as e:
                print(f"Tunnel relay error: {e}")
                break
        
        try:
            client.close()
        except:
            pass
    
    def _handle_http_request(self, method):
        """Handle regular HTTP requests (GET, POST, etc.)."""
        parsed = urllib.parse.urlparse(self.path)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        
        if not host:
            self.send_error(400, "No host in URL")
            return
        
        # Reconstruct the raw HTTP request
        path = parsed.path or '/'
        if parsed.query:
            path += '?' + parsed.query
        
        # Build request line
        raw_request = f"{method} {path} HTTP/1.1\r\n".encode()
        
        # Add headers
        for key, value in self.headers.items():
            if key.lower() not in ('proxy-connection', 'proxy-authorization'):
                raw_request += f"{key}: {value}\r\n".encode()
        raw_request += b"\r\n"
        
        # Add body if present
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
            print(f"HTTP proxy error {method} {host}:{port}: {e}")
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
        """Suppress default logging."""
        pass

def main():
    # Verify gh CLI is authenticated
    try:
        result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
        if result.returncode != 0:
            print("ERROR: 'gh' CLI not authenticated. Run 'gh auth login' first.")
            sys.exit(1)
    except FileNotFoundError:
        print("ERROR: 'gh' CLI not found. Install GitHub CLI first.")
        sys.exit(1)
    
    # Verify we can reach the Gist
    try:
        gh_api("GET", f"/gists/{COMMAND_GIST}")
        print(f"[+] Gist {COMMAND_GIST} accessible")
    except Exception as e:
        print(f"[-] Cannot access Gist: {e}")
        sys.exit(1)
    
    print(f"[*] Starting Gist Tunnel Proxy on 127.0.0.1:{LISTEN_PORT}")
    print(f"[*] Set your browser/proxy to HTTP proxy 127.0.0.1:{LISTEN_PORT}")
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
