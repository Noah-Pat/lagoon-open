#!/usr/bin/env python3
"""
Lagoon Open - LAN server (macOS / Windows / Linux, Python 3 standard library only)

Relays WebSocket messages between the host and guest browsers, and also serves the
game page so a buddy can simply browse to http://<host-address>:8765

Run it with:   python3 lagoon-server.py          (Mac / Linux, from this folder)
               py lagoon-server.py               (Windows)
Optional port: python3 lagoon-server.py 9000
Stop it with Ctrl+C (or close the window).
"""
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import threading

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
HERE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(HERE, "lagoon-open-3d.html")
WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME = 4 * 1024 * 1024

clients = []
clients_lock = threading.Lock()
print_lock = threading.Lock()


def log(msg):
    with print_lock:
        print(msg, flush=True)


# ---------------------------------------------------------------- LAN address discovery
def ip_rank(ip):
    if ip.startswith("192.168."):
        return 0
    if ip.startswith("10."):
        return 1
    if ip.startswith("172."):
        try:
            if 16 <= int(ip.split(".")[1]) <= 31:
                return 3  # usually virtual adapters (Docker / Hyper-V / WSL)
        except ValueError:
            pass
    return 2


def lan_ips():
    ips = set()
    # The OS picks the outgoing interface for a UDP "connect"; no packet is actually sent.
    for probe in ("10.255.255.255", "192.168.255.255", "8.8.8.8"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((probe, 1))
            ips.add(s.getsockname()[0])
            s.close()
        except OSError:
            pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            ips.add(ip)
    except OSError:
        pass
    ips = [ip for ip in ips if not ip.startswith(("127.", "169.254.", "0."))]
    return sorted(ips, key=lambda ip: (ip_rank(ip), ip))


# ---------------------------------------------------------------- WebSocket plumbing
class Client:
    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.lock = threading.Lock()
        self.alive = True

    def send(self, opcode, data):
        if not self.alive:
            return
        n = len(data)
        if n < 126:
            hdr = struct.pack("!BB", 0x80 | opcode, n)
        elif n < 65536:
            hdr = struct.pack("!BBH", 0x80 | opcode, 126, n)
        else:
            hdr = struct.pack("!BBQ", 0x80 | opcode, 127, n)
        try:
            with self.lock:
                self.sock.sendall(hdr + data)
        except OSError:
            self.alive = False
            try:
                self.sock.close()
            except OSError:
                pass


class Reader:
    """Buffered socket reader so bytes that arrive after the HTTP headers are never lost."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""

    def read_until(self, marker, limit=16384):
        while marker not in self.buf:
            if len(self.buf) > limit:
                return None
            chunk = self.sock.recv(4096)
            if not chunk:
                return None
            self.buf += chunk
        i = self.buf.index(marker) + len(marker)
        out, self.buf = self.buf[:i], self.buf[i:]
        return out

    def read_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self.buf)))
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


def unmask(data, mask):
    n = len(data)
    if n == 0:
        return data
    key = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")


def count():
    with clients_lock:
        return len(clients)


def sys_msg():
    return json.dumps({"t": "sys", "count": count(), "ips": lan_ips()}).encode("utf-8")


def broadcast(data):
    with clients_lock:
        snap = list(clients)
    for c in snap:
        c.send(1, data)


def relay(sender, opcode, data):
    with clients_lock:
        snap = list(clients)
    for c in snap:
        if c is not sender:
            c.send(opcode, data)


def ws_loop(client, reader):
    frag = bytearray()
    frag_op = 1
    while client.alive:
        b0, b1 = reader.read_exact(2)
        fin = b0 & 0x80
        op = b0 & 0x0F
        masked = b1 & 0x80
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack("!H", reader.read_exact(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", reader.read_exact(8))[0]
        if n > MAX_FRAME:
            return
        mask = reader.read_exact(4) if masked else None
        data = reader.read_exact(n)
        if mask:
            data = unmask(data, mask)
        if op == 8:  # close
            client.send(8, b"")
            return
        if op == 9:  # ping
            client.send(10, data)
            continue
        if op == 10:  # pong
            continue
        if op in (1, 2):
            frag = bytearray()
            frag_op = op
        frag += data
        if fin:
            relay(client, frag_op, bytes(frag))
            frag = bytearray()


def serve_http(sock, path):
    path = path.split("?", 1)[0]
    if path in ("/", "/index.html", "/lagoon-open-3d.html") and os.path.isfile(HTML):
        with open(HTML, "rb") as f:
            body = f.read()
        status, ctype = "200 OK", "text/html; charset=utf-8"
    else:
        body, status, ctype = b"Not found", "404 Not Found", "text/plain"
    head = (
        f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\nConnection: close\r\n\r\n"
    )
    sock.sendall(head.encode("ascii") + body)


def handle(sock, addr):
    client = None
    try:
        sock.settimeout(None)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        reader = Reader(sock)
        head = reader.read_until(b"\r\n\r\n")
        if head is None:
            return
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        path = parts[1] if len(parts) > 1 else "/"
        hdrs = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                hdrs[k.strip().lower()] = v.strip()
        if hdrs.get("upgrade", "").lower() == "websocket" and "sec-websocket-key" in hdrs:
            accept = base64.b64encode(hashlib.sha1(hdrs["sec-websocket-key"].encode("ascii") + WS_GUID).digest()).decode("ascii")
            sock.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                ).encode("ascii")
            )
            client = Client(sock, addr)
            with clients_lock:
                clients.append(client)
            log(f"[+] Player connected from {addr[0]}  ({count()} online)")
            broadcast(sys_msg())
            ws_loop(client, reader)
        else:
            serve_http(sock, path)
    except (OSError, ConnectionError, ValueError, struct.error):
        pass
    finally:
        if client is not None:
            client.alive = False
            with clients_lock:
                if client in clients:
                    clients.remove(client)
            log(f"[-] Player disconnected  ({count()} online)")
            broadcast(sys_msg())
        try:
            sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- main
def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name != "nt":
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("0.0.0.0", PORT))
    except OSError as e:
        print(f"\nCould not start the server on port {PORT}: {e}")
        print("Is another copy already running? Close it, or pass a different port number.")
        sys.exit(1)
    srv.listen(8)
    srv.settimeout(1.0)  # lets Ctrl+C stop the server on every OS

    ips = lan_ips()
    print()
    print(f"  LAGOON OPEN - LAN server running on port {PORT}")
    print("  ------------------------------------------------")
    print("  On THIS computer: open the game, choose 'LAN Match', press 'Host Game'.")
    if ips:
        print("  Your buddy chooses 'LAN Match' and joins with:  " + "   ".join(ips))
        print(f"  (or they can just browse to http://{ips[0]}:{PORT} )")
    else:
        print("  No LAN address found - are you connected to Wi-Fi / Ethernet?")
    if not os.path.isfile(HTML):
        print("  Note: lagoon-open-3d.html is not next to this script, so the page itself won't be served.")
    print("  If your firewall asks whether to accept incoming connections, choose Allow.")
    print("  Press Ctrl+C (or close this window) to stop the server.")
    print(flush=True)

    try:
        while True:
            try:
                sock, addr = srv.accept()
            except socket.timeout:
                continue
            threading.Thread(target=handle, args=(sock, addr), daemon=True).start()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        srv.close()


if __name__ == "__main__":
    main()
