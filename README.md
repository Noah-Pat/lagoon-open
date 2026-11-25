# Lagoon Open

Motion-controlled arcade tennis on a floating lagoon pier, rendered with [three.js](https://threejs.org/) in a single HTML file.

- Three CPU rivals (Sunny, Reef, Mako), each with its own lighting: resort noon, golden hour and twilight
- Timing-based shots: swing early for cross-court, late for down the line; hit the toss peak for a 130 MPH Rocket Serve
- Target Rings training mode (60 seconds, combo multiplier)
- **LAN multiplayer**: play a friend on the same network
- Wii Remote support through WebHID (Chrome / Edge on desktop), plus mouse, touch and keyboard
- PBR materials, soft shadows, sky-based lighting, reflective water, ambient occlusion and bloom, with Ultra / Balanced / Fast quality levels

## Play

Open `lagoon-open-3d.html` in a modern browser (Chrome, Edge, Firefox or Safari). Nothing to install.
The page loads three.js from the jsDelivr CDN, so it needs an internet connection when it opens.

`lagoon-open.html` is the earlier version with a hand-written WebGL2 renderer.

## Controls

| Input | Action |
| --- | --- |
| Mouse / touch | Flick across the screen to swing (up = topspin, down = slice), or click and hold to charge; right-click for slice |
| Keyboard | **Space** drive · **X / Shift** slice · **C** lob · **WASD / arrows** move around the court · **Esc** menu |
| Wii Remote | Swing for drive/topspin · hold **B** for slice · **A** toss / arm a lob · **D-pad** move |

## LAN multiplayer

One computer hosts a small relay server; the other player joins with the host's local address.

1. **Host** – keep `lagoon-server.py` next to `lagoon-open-3d.html` and start the server:
   - **macOS / Linux:** `python3 lagoon-server.py` in Terminal (or double-click `Start-Lagoon-Server.command` after running `chmod +x Start-Lagoon-Server.command` once)
   - **Windows:** double-click `Start-Lagoon-Server.bat` (uses Python if installed, otherwise the built-in PowerShell server `lagoon-server.ps1`)
2. Allow incoming connections if your firewall asks.
3. Open the game, choose **LAN Match**, press **Host Game**. The lobby shows the address to share (e.g. `192.168.1.23`).
4. **Guest** – open `lagoon-open-3d.html`, choose **LAN Match**, enter the host's address and press **Join Game**.
   Or skip the file entirely and browse to `http://<host-address>:8765`.
5. The host presses **Start Match**.

The server uses port 8765 (`python3 lagoon-server.py 9000` picks another port). The Python server needs only the Python 3 standard library.

## Files

| File | Purpose |
| --- | --- |
| `lagoon-open-3d.html` | The game (three.js version) |
| `lagoon-open.html` | Earlier WebGL2 version |
| `lagoon-server.py` | Cross-platform LAN relay server (Python 3) |
| `Start-Lagoon-Server.command` | macOS launcher for the server |
| `Start-Lagoon-Server.bat` | Windows launcher for the server |
| `lagoon-server.ps1` | Windows PowerShell fallback server (no Python needed) |

---

Lagoon Open is a fan-made game and is not affiliated with or endorsed by Nintendo. "Wii" is a trademark of Nintendo.
