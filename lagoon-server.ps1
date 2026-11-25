# Lagoon Open LAN server
# Relays WebSocket messages between the host and guest browsers and serves the game page.
# Start it with Start-Lagoon-Server.bat (or: powershell -ExecutionPolicy Bypass -File lagoon-server.ps1)
param([int]$Port = 8765)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$html = Join-Path $here "lagoon-open-3d.html"

Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Threading;

public static class LagoonRelay {
  class Client {
    public TcpClient Tcp;
    public NetworkStream S;
    public object L = new object();
    public volatile bool Alive = true;
  }

  static readonly List<Client> clients = new List<Client>();
  static string htmlPath;
  static TcpListener listener;

  public static string[] LanIPs() {
    var list = new List<string>();
    try {
      foreach (var ni in NetworkInterface.GetAllNetworkInterfaces()) {
        if (ni.OperationalStatus != OperationalStatus.Up) continue;
        if (ni.NetworkInterfaceType == NetworkInterfaceType.Loopback) continue;
        foreach (var ua in ni.GetIPProperties().UnicastAddresses) {
          if (ua.Address.AddressFamily != AddressFamily.InterNetwork) continue;
          if (IPAddress.IsLoopback(ua.Address)) continue;
          string s = ua.Address.ToString();
          if (s.StartsWith("169.254.")) continue;
          if (!list.Contains(s)) list.Add(s);
        }
      }
    } catch (Exception) { }
    list.Sort((a, b) => Rank(a).CompareTo(Rank(b)));
    return list.ToArray();
  }

  // Typical home-LAN ranges first, VPN / virtual adapters last
  static int Rank(string ip) {
    if (ip.StartsWith("192.168.")) return 0;
    if (ip.StartsWith("10.")) return 1;
    if (ip.StartsWith("172.")) {
      int second;
      string[] p = ip.Split('.');
      if (p.Length > 1 && int.TryParse(p[1], out second) && second >= 16 && second <= 31) return 3;
    }
    return 2;
  }

  public static void Start(int port, string html) {
    htmlPath = html;
    listener = new TcpListener(IPAddress.Any, port);
    listener.Start();
    var t = new Thread(AcceptLoop);
    t.IsBackground = true;
    t.Start();
  }

  static void AcceptLoop() {
    while (true) {
      TcpClient tcp;
      try { tcp = listener.AcceptTcpClient(); } catch (Exception) { return; }
      var t = new Thread(Handle);
      t.IsBackground = true;
      t.Start(tcp);
    }
  }

  static int Count() { lock (clients) { return clients.Count; } }

  static void Handle(object o) {
    var tcp = (TcpClient)o;
    tcp.NoDelay = true;
    NetworkStream s = tcp.GetStream();
    Client c = null;
    try {
      string head = ReadHeaders(s);
      if (head == null) { tcp.Close(); return; }
      string[] lines = head.Split(new string[] { "\r\n" }, StringSplitOptions.None);
      string[] req = lines[0].Split(' ');
      string path = req.Length > 1 ? req[1] : "/";
      string key = null;
      bool upgrade = false;
      foreach (var ln in lines) {
        int i = ln.IndexOf(':');
        if (i < 0) continue;
        string k = ln.Substring(0, i).Trim().ToLowerInvariant();
        string v = ln.Substring(i + 1).Trim();
        if (k == "sec-websocket-key") key = v;
        if (k == "upgrade" && v.ToLowerInvariant() == "websocket") upgrade = true;
      }
      if (upgrade && key != null) {
        string accept;
        using (var sha = SHA1.Create()) {
          accept = Convert.ToBase64String(sha.ComputeHash(Encoding.ASCII.GetBytes(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")));
        }
        byte[] resp = Encoding.ASCII.GetBytes("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + "\r\n\r\n");
        s.Write(resp, 0, resp.Length);
        c = new Client();
        c.Tcp = tcp;
        c.S = s;
        lock (clients) { clients.Add(c); }
        Console.WriteLine("[+] Player connected from " + tcp.Client.RemoteEndPoint + "  (" + Count() + " online)");
        Broadcast(SysMsg());
        Loop(c);
      } else {
        ServeHttp(s, path);
        tcp.Close();
      }
    } catch (Exception) {
    } finally {
      if (c != null) {
        c.Alive = false;
        lock (clients) { clients.Remove(c); }
        try { tcp.Close(); } catch (Exception) { }
        Console.WriteLine("[-] Player disconnected  (" + Count() + " online)");
        Broadcast(SysMsg());
      }
    }
  }

  static string SysMsg() {
    var ips = LanIPs();
    var sb = new StringBuilder();
    sb.Append("{\"t\":\"sys\",\"count\":").Append(Count()).Append(",\"ips\":[");
    for (int i = 0; i < ips.Length; i++) {
      if (i > 0) sb.Append(',');
      sb.Append('"').Append(ips[i]).Append('"');
    }
    sb.Append("]}");
    return sb.ToString();
  }

  static string ReadHeaders(NetworkStream s) {
    var sb = new StringBuilder();
    int b;
    while ((b = s.ReadByte()) != -1) {
      sb.Append((char)b);
      if (sb.Length > 16384) return null;
      int n = sb.Length;
      if (n >= 4 && sb[n - 4] == '\r' && sb[n - 3] == '\n' && sb[n - 2] == '\r' && sb[n - 1] == '\n') return sb.ToString();
    }
    return null;
  }

  static void ReadExact(NetworkStream s, byte[] buf, int len) {
    int off = 0;
    while (off < len) {
      int r = s.Read(buf, off, len - off);
      if (r <= 0) throw new IOException("closed");
      off += r;
    }
  }

  static void Loop(Client c) {
    var s = c.S;
    var hdr = new byte[8];
    var frag = new MemoryStream();
    int fragOp = 1;
    while (c.Alive) {
      ReadExact(s, hdr, 2);
      bool fin = (hdr[0] & 0x80) != 0;
      int op = hdr[0] & 0x0F;
      bool masked = (hdr[1] & 0x80) != 0;
      long len = hdr[1] & 0x7F;
      if (len == 126) { ReadExact(s, hdr, 2); len = (hdr[0] << 8) | hdr[1]; }
      else if (len == 127) { ReadExact(s, hdr, 8); len = 0; for (int i = 0; i < 8; i++) len = (len << 8) | hdr[i]; }
      if (len > 4 * 1024 * 1024) throw new IOException("frame too large");
      byte[] mask = new byte[4];
      if (masked) ReadExact(s, mask, 4);
      byte[] data = new byte[len];
      ReadExact(s, data, (int)len);
      if (masked) for (int i = 0; i < data.Length; i++) data[i] ^= mask[i & 3];
      if (op == 8) { Send(c, 8, new byte[0]); return; }
      if (op == 9) { Send(c, 10, data); continue; }
      if (op == 10) continue;
      if (op == 1 || op == 2) { frag.SetLength(0); fragOp = op; }
      frag.Write(data, 0, data.Length);
      if (fin) {
        byte[] msg = frag.ToArray();
        frag.SetLength(0);
        Relay(c, fragOp, msg);
      }
    }
  }

  static void Relay(Client from, int op, byte[] msg) {
    List<Client> snap;
    lock (clients) { snap = new List<Client>(clients); }
    foreach (var c in snap) if (c != from) Send(c, op, msg);
  }

  static void Broadcast(string text) {
    var b = Encoding.UTF8.GetBytes(text);
    List<Client> snap;
    lock (clients) { snap = new List<Client>(clients); }
    foreach (var c in snap) Send(c, 1, b);
  }

  static void Send(Client c, int op, byte[] data) {
    if (!c.Alive) return;
    var ms = new MemoryStream();
    ms.WriteByte((byte)(0x80 | op));
    if (data.Length < 126) ms.WriteByte((byte)data.Length);
    else if (data.Length < 65536) { ms.WriteByte(126); ms.WriteByte((byte)(data.Length >> 8)); ms.WriteByte((byte)data.Length); }
    else { ms.WriteByte(127); for (int i = 7; i >= 0; i--) ms.WriteByte((byte)(((long)data.Length) >> (8 * i))); }
    ms.Write(data, 0, data.Length);
    byte[] f = ms.ToArray();
    try { lock (c.L) { c.S.Write(f, 0, f.Length); } }
    catch (Exception) { c.Alive = false; try { c.Tcp.Close(); } catch (Exception) { } }
  }

  static void ServeHttp(NetworkStream s, string path) {
    int q = path.IndexOf('?');
    if (q >= 0) path = path.Substring(0, q);
    byte[] body;
    string status = "200 OK", type = "text/html; charset=utf-8";
    if ((path == "/" || path == "/index.html" || path == "/lagoon-open-3d.html") && File.Exists(htmlPath)) {
      body = File.ReadAllBytes(htmlPath);
    } else {
      status = "404 Not Found"; type = "text/plain"; body = Encoding.UTF8.GetBytes("Not found");
    }
    byte[] h = Encoding.ASCII.GetBytes("HTTP/1.1 " + status + "\r\nContent-Type: " + type + "\r\nContent-Length: " + body.Length + "\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n");
    s.Write(h, 0, h.Length);
    s.Write(body, 0, body.Length);
    s.Flush();
  }
}
'@

try {
  [LagoonRelay]::Start($Port, $html)
} catch {
  Write-Host ""
  Write-Host "Could not start the server on port $Port." -ForegroundColor Red
  Write-Host "Is another copy already running? ($($_.Exception.Message))" -ForegroundColor Red
  exit 1
}

$ips = [LagoonRelay]::LanIPs()
Write-Host ""
Write-Host "  LAGOON OPEN - LAN server running on port $Port" -ForegroundColor Yellow
Write-Host "  ------------------------------------------------"
Write-Host "  On THIS PC: open the game, choose 'LAN Match', press 'Host Game'."
if ($ips.Count -gt 0) {
  Write-Host "  Your buddy chooses 'LAN Match' and joins with:" -NoNewline
  foreach ($ip in $ips) { Write-Host "  $ip" -ForegroundColor Cyan -NoNewline }
  Write-Host ""
  Write-Host "  (or they can just browse to http://$($ips[0]):$Port )"
} else {
  Write-Host "  No LAN network address found - are you connected to Wi-Fi / Ethernet?" -ForegroundColor Red
}
Write-Host "  If Windows asks about the firewall, allow access on Private networks."
Write-Host "  Close this window to stop the server."
Write-Host ""
while ($true) { Start-Sleep -Seconds 1 }
