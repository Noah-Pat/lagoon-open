"""
Lagoon Open model builder (runs inside Blender).

    blender -b --factory-startup -P models/build_models.py                    (rebuild everything from this script)
    blender -b --factory-startup -P models/build_models.py -- --from-blend    (export your hand edits in the .blend)
    blender -b --factory-startup -P models/build_models.py -- --no-embed      (build + export, leave the HTML alone)

Builds every model from code, then:
  1. saves models/lagoon-models.blend   (open it in Blender to look at or tweak the models)
  2. exports models/lagoon-models.glb
  3. embeds the GLB (gzip + base64) into lagoon-open-3d-models.html (the test copy of the game), inside
     <script id="lagoon-models" type="application/octet-stream">

Every model is authored in game units (metres) at the origin the game expects.
Inside this script points are written in game coordinates (Y up, +Z toward the far court)
and converted to Blender's Z-up space when the vertices are created; the glTF
exporter converts them back, so the .blend file looks upright in Blender.

Coloured models (scenery, shoes) carry a "Col" vertex colour attribute in linear RGB.
Character parts and the racket carry no colours: the game gives them per-player materials.
"""
import base64
import gzip
import math
import os
import re
import sys

import bmesh
import bpy
from mathutils import Matrix, Vector

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HTML = os.path.join(ROOT, "lagoon-open-3d-models.html")   # the test version of the game; lagoon-open-3d.html stays untouched
BLEND = os.path.join(HERE, "lagoon-models.blend")
GLB = os.path.join(HERE, "lagoon-models.glb")
TAU = math.tau


def lerp(a, b, t):
    return a + (b - a) * t


def lerpc(a, b, t):
    return [lerp(a[i], b[i], t) for i in range(3)]


def smoothstep(a, b, x):
    t = max(0.0, min(1.0, (x - a) / (b - a)))
    return t * t * (3 - 2 * t)


def ihash(i, s):
    """Deterministic 0..1 hash (stable across runs)."""
    x = math.sin(i * 127.1 + s * 311.7) * 43758.5453
    return x - math.floor(x)


def gv(p):
    """Game coords (x, y-up, z) -> Blender coords (x, -z, y)."""
    return Vector((p[0], -p[2], p[1]))


def frames(pts):
    """Parallel-transport frames (tangent, normal, binormal) along a polyline."""
    n = len(pts)
    T = []
    for i in range(n):
        d = pts[min(i + 1, n - 1)] - pts[max(i - 1, 0)]
        T.append(d.normalized())
    nrm = T[0].cross(Vector((0, 0, 1))) if abs(T[0].z) < 0.9 else T[0].cross(Vector((1, 0, 0)))
    nrm.normalize()
    N = [nrm.copy()]
    for i in range(1, n):
        axis = T[i - 1].cross(T[i])
        if axis.length > 1e-9:
            nrm = Matrix.Rotation(T[i - 1].angle(T[i]), 3, axis.normalized()) @ nrm
        N.append(nrm.copy())
    B = [T[i].cross(N[i]) for i in range(n)]
    return T, N, B


class MB:
    """Small mesh builder on top of bmesh. Positions are in game space."""

    def __init__(self, colored=True, bm=None):
        self.bm = bm if bm is not None else bmesh.new()
        if not colored:
            self.col = None
        else:
            self.col = self.bm.loops.layers.float_color.get("Col") or self.bm.loops.layers.float_color.new("Col")

    def v(self, p):
        return self.bm.verts.new(gv(p))

    def face(self, vs, col=None, smooth=True):
        try:
            f = self.bm.faces.new(vs)
        except ValueError:
            return None
        f.smooth = smooth
        if self.col is not None and col is not None:
            c = (col[0], col[1], col[2], 1.0)
            for loop in f.loops:
                loop[self.col] = c
        return f

    def _c(self, col, i, j, pts):
        if callable(col):
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            cz = sum(p[2] for p in pts) / len(pts)
            return col(i, j, Vector((cx, cy, cz)))
        return col

    def grid(self, rows, col, closed_u=False, closed_v=False, smooth=True):
        """rows: list of rows of game-space points. Quads between consecutive rows."""
        R = [[self.v(p) for p in row] for row in rows]
        n = len(rows[0])
        nr = len(rows)
        for i in range(nr - (0 if closed_v else 1)):
            i2 = (i + 1) % nr
            for j in range(n - (0 if closed_u else 1)):
                j2 = (j + 1) % n
                pts = [rows[i][j], rows[i][j2], rows[i2][j2], rows[i2][j]]
                self.face([R[i][j], R[i][j2], R[i2][j2], R[i2][j]], self._c(col, i, j, pts), smooth)
        return R

    def fan(self, ring, center, col, smooth=True):
        c = self.v(center)
        n = len(ring)
        for j in range(n):
            a, b = ring[j], ring[(j + 1) % n]
            pa = Vector(a.co)
            self.face([c, a, b], self._c(col, -1, j, [Vector(center), Vector((pa.x, pa.z, -pa.y))]), smooth)
        return c

    def lathe(self, prof, col, segs, center=(0, 0, 0), cap0=False, cap1=False, sx=1.0, sz=1.0, smooth=True, phase=0.0):
        """prof: [(radius, y), ...] around the Y axis."""
        rows = []
        for r, y in prof:
            rows.append([(center[0] + math.cos(a) * r * sx, center[1] + y, center[2] + math.sin(a) * r * sz)
                         for a in [phase + k / segs * TAU for k in range(segs)]])
        R = self.grid(rows, col, closed_u=True, smooth=smooth)
        if cap0:
            self.fan(R[0], (center[0], center[1] + prof[0][1], center[2]), self._c(col, 0, 0, [rows[0][0]]), smooth)
        if cap1:
            self.fan(R[-1], (center[0], center[1] + prof[-1][1], center[2]), self._c(col, len(rows) - 1, 0, [rows[-1][0]]), smooth)
        return R

    def ellipsoid(self, c, r, col, segs=16, rings=10, smooth=True):
        prof = []
        for k in range(1, rings):
            t = k / rings * math.pi
            prof.append((math.sin(t), -math.cos(t)))
        rows = [[(c[0] + math.cos(a) * s * r[0], c[1] + y * r[1], c[2] + math.sin(a) * s * r[2])
                 for a in [k / segs * TAU for k in range(segs)]] for s, y in prof]
        R = self.grid(rows, col, closed_u=True, smooth=smooth)
        cc = self._c(col, 0, 0, [c])
        self.fan(R[0], (c[0], c[1] - r[1], c[2]), cc, smooth)
        self.fan(R[-1], (c[0], c[1] + r[1], c[2]), cc, smooth)
        return R

    def tube(self, pts, radius, col, segs=8, cap0=True, cap1=True, ellip=(1.0, 1.0), smooth=True, twist=0.0):
        pts = [Vector(p) for p in pts]
        T, N, B = frames(pts)
        rows = []
        n = len(pts)
        for i, p in enumerate(pts):
            u = i / (n - 1)
            r = radius(u) if callable(radius) else radius
            rows.append([p + N[i] * math.cos(a) * r * ellip[0] + B[i] * math.sin(a) * r * ellip[1]
                         for a in [twist + k / segs * TAU for k in range(segs)]])
        R = self.grid(rows, col, closed_u=True, smooth=smooth)
        if cap0:
            self.fan(R[0], pts[0], self._c(col, 0, 0, [pts[0]]), smooth)
        if cap1:
            self.fan(R[-1], pts[-1], self._c(col, n - 1, 0, [pts[-1]]), smooth)
        return R

    def box(self, c, h, col, rot=None, top_scale=1.0):
        """Axis box (half extents h) optionally rotated by a 3x3 game-space matrix; top_scale tapers the top face."""
        cv = Vector(c)
        pts = []
        for y in (-1, 1):
            s = top_scale if y > 0 else 1.0
            for x, z in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                p = Vector((x * h[0] * s, y * h[1], z * h[2] * s))
                if rot is not None:
                    p = rot @ p
                pts.append(cv + p)
        V = [self.v(p) for p in pts]
        quads = [(0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1), (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0)]
        for q in quads:
            self.face([V[k] for k in q], self._c(col, 0, 0, [pts[k] for k in q]), smooth=False)

    def finish(self, name, sharp_deg=40.0, location=(0, 0, 0)):
        bm = self.bm
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
        me = bpy.data.meshes.new(name)
        bm.to_mesh(me)
        bm.free()
        if sharp_deg is not None:
            me.set_sharp_from_angle(angle=math.radians(sharp_deg))
        if "Col" in me.color_attributes:
            attr = me.color_attributes["Col"]
            for setter in (lambda: setattr(me.color_attributes, "active_color", attr),
                           lambda: setattr(me.color_attributes, "active_color_name", "Col"),
                           lambda: setattr(me.color_attributes, "render_color_index", list(me.color_attributes).index(attr)),
                           lambda: setattr(me.color_attributes, "default_color_name", "Col")):
                try:
                    setter()
                except Exception:
                    pass
        obj = bpy.data.objects.new(name, me)
        obj.location = location
        bpy.context.scene.collection.objects.link(obj)
        return obj


# =========================================================================
# Scenery
# =========================================================================

def build_palm():
    m = MB()
    H = 5.4
    rings = 22
    fr = [0.0, 0.55, 0.85]          # sub-rows inside each bark ring: groove, body, ledge
    rad_k = [0.93, 1.0, 1.07]
    groove, body, ledge = [0.21, 0.13, 0.07], [0.36, 0.22, 0.12], [0.47, 0.34, 0.21]
    boot = [0.30, 0.27, 0.11]

    def trunk(t):
        return Vector((math.sin(t * 1.4) * 0.72, 0.65 + t * H, 0.0))

    pts, radii, cols = [Vector((0.0, 0.40, 0.0))], [0.30], [groove]
    for r in range(rings):
        for k, f in enumerate(fr):
            t = (r + f) / rings
            flare = 1.0 + max(0.0, 0.06 - t) / 0.06 * 0.45
            pts.append(trunk(t))
            radii.append(lerp(0.215, 0.095, t) * rad_k[k] * flare)
            c = [groove, body, ledge][k]
            if t > 0.88:
                c = lerpc(c, boot, smoothstep(0.88, 0.97, t))
            cols.append(c)
    top = trunk(1.0)
    pts.append(top)
    radii.append(0.10)
    cols.append(boot)

    seg = 10
    T, N, B = frames(pts)
    rows = []
    for i, p in enumerate(pts):
        rows.append([p + N[i] * math.cos(a) * radii[i] + B[i] * math.sin(a) * radii[i]
                     for a in [k / seg * TAU + 0.3 * (i % 2) for k in range(seg)]])
    R = m.grid(rows, lambda i, j, c: cols[i], closed_u=True)
    m.fan(R[0], pts[0], groove)
    crown = top + Vector((0.0, 0.06, 0.0))
    m.fan(R[-1], crown, boot)

    # Crown bulb and coconuts
    m.ellipsoid(top + Vector((0.0, 0.02, 0.0)), (0.17, 0.22, 0.17), boot, 12, 8)
    for i in range(6):
        a = i / 6 * TAU + 0.4
        rr = 0.17 + 0.03 * ihash(i, 2)
        c = [0.28, 0.20, 0.08] if i % 3 else [0.34, 0.42, 0.12]
        m.ellipsoid(top + Vector((math.cos(a) * rr, -0.16 - 0.06 * ihash(i, 3), math.sin(a) * rr)), (0.105, 0.115, 0.105), c, 10, 7)

    # Fronds: rachis + V-folded leaflets along both sides
    leaf_base, leaf_tip = [0.08, 0.30, 0.09], [0.28, 0.54, 0.15]
    rachis_col = [0.34, 0.40, 0.15]
    fronds = []
    for f in range(12):
        fronds.append(dict(yaw=f / 12 * TAU + (f % 2) * 0.2 + ihash(f, 9) * 0.15,
                           L=2.5 + (f % 2) * 0.45 + ihash(f, 4) * 0.35,
                           lift=0.52 + 0.18 * ihash(f, 6) - (f % 2) * 0.12,
                           dead=False))
    for f in range(4):
        fronds.append(dict(yaw=f / 4 * TAU + 0.6, L=1.9, lift=-0.05, dead=True))

    down = Vector((0.0, -1.0, 0.0))
    for fi, fd in enumerate(fronds):
        yaw, L, lift = fd["yaw"], fd["L"], fd["lift"]
        dirv = Vector((math.sin(yaw), 0.0, math.cos(yaw)))
        side = Vector((math.cos(yaw), 0.0, -math.sin(yaw)))

        def rach(t):
            u = 1 - t
            if fd["dead"]:
                ly = -t * L * 0.85
                lz = t * L * 0.38
            else:
                ly = 2 * u * t * (L * lift) + t * t * (-L * 0.36)
                lz = 2 * u * t * (L * 0.42) + t * t * L
            return crown + dirv * lz + Vector((0.0, ly, 0.0))

        S = 10
        rpts = [rach(i / S) for i in range(S + 1)]
        m.tube(rpts, lambda u: lerp(0.035, 0.008, u), [0.30, 0.22, 0.10] if fd["dead"] else rachis_col, segs=4, cap0=False)

        st = 14 if not fd["dead"] else 9
        for k in range(st):
            t = 0.10 + 0.88 * (k + 0.5) / st
            p = rach(t)
            tn = (rach(min(1.0, t + 0.02)) - rach(max(0.0, t - 0.02))).normalized()
            ll = 0.12 + 0.62 * math.sin(math.pi * min(1.0, t * 1.05)) ** 0.6
            if fd["dead"]:
                ll *= 0.7
            for s in (-1, 1):
                D = (side * s * 0.85 + tn * 0.5 + down * (0.15 + 0.5 * t)).normalized()
                W = tn
                up = D.cross(W).normalized()
                if up.y < 0:
                    up = -up
                base_c = lerpc(leaf_base, leaf_tip, t)
                if fd["dead"]:
                    base_c = lerpc([0.42, 0.30, 0.14], [0.55, 0.42, 0.22], t)
                elif fi % 2:
                    base_c = [base_c[0] * 1.08, base_c[1] * 1.02, base_c[2] * 0.9]
                stations = []
                for u, hw in ((0.0, 0.018), (0.5, 0.042), (1.0, 0.0)):
                    c = p + D * ll * u + down * (ll * 0.35 * u * u * (0.4 + t))
                    stations.append((c, hw))
                verts = []
                for c, hw in stations[:2]:
                    verts.append([m.v(c - W * hw), m.v(c + up * hw * 0.45), m.v(c + W * hw)])
                tip = m.v(stations[2][0])
                for a in range(2):
                    shade = 0.9 + 0.2 * a
                    cc = [base_c[0] * shade, base_c[1] * shade, base_c[2] * shade]
                    m.face([verts[0][a], verts[0][a + 1], verts[1][a + 1], verts[1][a]], cc)
                    m.face([verts[1][a], verts[1][a + 1], tip], [x * 1.12 for x in cc])
    return m.finish("palm", sharp_deg=70)


def build_sailboat():
    m = MB()
    white, boot, bottom, deck, trim = [0.95, 0.93, 0.88], [0.94, 0.30, 0.18], [0.06, 0.16, 0.22], [0.55, 0.34, 0.18], [0.16, 0.48, 0.50]
    z0, z1 = -1.5, 1.8
    NS = 18

    def beam(s):
        if s >= 0.4:
            return 0.58 * max(0.0, 1 - ((s - 0.4) / 0.6) ** 2) ** 0.7 + 0.004
        return 0.58 * (1 - ((0.4 - s) / 0.4) ** 2 * 0.25)

    def deck_y(s):
        return 0.34 + 0.13 * s ** 3

    def keel_y(s):
        y = -0.14 + 0.40 * max(0.0, (s - 0.62) / 0.38) ** 1.6
        if s < 0.2:
            y += 0.10 * (1 - s / 0.2)
        return y

    Q = [0.0, 0.18, 0.38, 0.58, 0.76, 0.9, 1.0]
    rows = []
    for i in range(NS + 1):
        s = i / NS
        z = lerp(z0, z1, s)
        w, yd, yk = beam(s), deck_y(s), keel_y(s)
        ring = [(0.0, yd + 0.025, z)]
        for q in Q:                               # starboard: deck edge -> keel
            ring.append((w * math.sqrt(max(0.0, 1 - q ** 2.2)), lerp(yd, yk, q), z))
        for q in reversed(Q[:-1]):                # port: keel -> deck edge
            ring.append((-w * math.sqrt(max(0.0, 1 - q ** 2.2)), lerp(yd, yk, q), z))
        rows.append(ring)
    nring = len(rows[0])

    def hull_col(i, j, c):
        if j == 0 or j == nring - 1:
            return deck
        if c.y < 0.0:
            return bottom
        if c.y < 0.07:
            return boot
        s = (c.z - z0) / (z1 - z0)
        if c.y > deck_y(s) - 0.05:
            return trim
        return white

    R = m.grid(rows, hull_col, closed_u=True)
    m.fan(R[0], (0.0, 0.2, z0), white)
    m.fan(R[-1], (0.0, deck_y(1.0) - 0.1, z1 + 0.01), white)

    # Cabin with windows, cockpit coaming
    cream, glass = [0.93, 0.90, 0.82], [0.07, 0.10, 0.13]
    m.box((0.0, 0.47, -0.05), (0.27, 0.13, 0.5), cream, top_scale=0.88)
    for sx in (-1, 1):
        for k in range(3):
            m.box((sx * 0.262, 0.49, -0.38 + k * 0.3), (0.012, 0.035, 0.09), glass)
    m.box((0.0, 0.45, 0.46), (0.17, 0.04, 0.012), glass)
    m.box((0.0, 0.40, -0.9), (0.3, 0.045, 0.32), deck)

    # Rig
    alu, line = [0.78, 0.79, 0.80], [0.20, 0.20, 0.22]
    mast_base, mast_top = Vector((0.0, 0.58, 0.15)), Vector((0.0, 4.1, 0.15))
    m.tube([mast_base, mast_top], lambda u: lerp(0.045, 0.026, u), alu, segs=8)
    boom0, boom1 = Vector((0.0, 0.72, 0.1)), Vector((0.26, 0.76, -1.42))
    m.tube([boom0, boom1], 0.026, alu, segs=6)
    bow, stern = Vector((0.0, deck_y(1.0), z1 - 0.05)), Vector((0.0, deck_y(0.0) + 0.02, z0 + 0.05))
    m.tube([mast_top, bow], 0.007, line, segs=3, cap0=False, cap1=False)
    m.tube([mast_top, stern], 0.007, line, segs=3, cap0=False, cap1=False)
    for sx in (-1, 1):
        m.tube([Vector((0.0, 3.2, 0.15)), Vector((sx * 0.5, deck_y(0.45), 0.15))], 0.006, line, segs=3, cap0=False, cap1=False)

    # Sails: cambered grids
    def sail(tack, head, clew, camber, colfn, nu=7, nv=10):
        rows = []
        for a in range(nv + 1):
            v = a / nv
            row = []
            for b in range(nu + 1):
                u = b / nu
                p = tack + (clew - tack) * u * (1 - v) + (head - tack) * v
                p = p + Vector((camber * math.sin(math.pi * u) * (1 - v) ** 0.7, 0.0, 0.0))
                row.append(p)
            rows.append(row)
        m.grid(rows, lambda i, j, c: colfn(i / nv, j / nu))

    sailc, band1, band2 = [0.98, 0.96, 0.90], [0.94, 0.30, 0.18], [0.16, 0.62, 0.62]

    def main_col(v, u):
        if 0.08 < v < 0.13:
            return band1
        if 0.13 <= v < 0.17:
            return band2
        return sailc

    sail(Vector((0.0, 0.76, 0.12)), Vector((0.0, 3.98, 0.13)), Vector((0.27, 0.8, -1.4)), 0.2, main_col)
    jib, jibe = [0.98, 0.78, 0.22], [0.98, 0.94, 0.86]
    sail(Vector((0.0, deck_y(1.0) + 0.12, 1.62)), Vector((0.0, 3.6, 0.35)), Vector((0.42, 0.7, 0.55)), -0.16,
         lambda v, u: jibe if u > 0.86 else jib)

    m.bm.verts.ensure_lookup_table()
    bmesh.ops.remove_doubles(m.bm, verts=m.bm.verts[:], dist=1e-5)
    return m.finish("sailboat", sharp_deg=50)


def build_lighthouse():
    m = MB()
    white, red, dark, stone, glass_frame = [0.92, 0.90, 0.85], [0.80, 0.15, 0.10], [0.10, 0.12, 0.14], [0.52, 0.50, 0.47], [0.15, 0.17, 0.19]

    def body_r(y):
        return 0.95 - 0.33 * y / 7.0

    # Stone plinth
    m.lathe([(1.18, -0.8), (1.28, -0.05), (1.28, 0.10), (1.12, 0.2), (0.96, 0.22)], stone, 28, cap0=True)
    # Tower with red bands
    ys = [0.22, 1.0, 1.0, 2.2, 2.2, 3.8, 3.8, 5.0, 5.0, 6.55]
    prof = []
    for k, y in enumerate(ys):
        prof.append((body_r(y) + (0.012 if 1.0 <= y <= 2.2 or 3.8 <= y <= 5.0 else 0.0), y))
    prof = [(body_r(0.22), 0.22), (body_r(1.0), 1.0), (body_r(1.0) + 0.012, 1.0), (body_r(2.2) + 0.012, 2.2), (body_r(2.2), 2.2),
            (body_r(3.8), 3.8), (body_r(3.8) + 0.012, 3.8), (body_r(5.0) + 0.012, 5.0), (body_r(5.0), 5.0), (body_r(6.55), 6.55),
            (0.72, 6.8), (0.98, 6.98), (1.0, 7.0)]

    def tower_col(i, j, c):
        if (1.0 < c.y < 2.2) or (3.8 < c.y < 5.0):
            return red
        return white

    m.lathe(prof, tower_col, 32)
    # Gallery deck
    m.lathe([(1.0, 7.0), (1.16, 7.0), (1.16, 7.1), (0.0, 7.1)], dark, 32)
    # Railing: posts + top & mid rails
    for k in range(20):
        a = k / 20 * TAU
        m.box((math.cos(a) * 1.11, 7.33, math.sin(a) * 1.11), (0.018, 0.23, 0.018), dark)
    for y, r in ((7.56, 0.022), (7.33, 0.012)):
        rows = []
        for k in range(40):
            a = k / 40 * TAU
            cx, cz = math.cos(a), math.sin(a)
            rows.append([(cx * (1.11 + math.cos(b) * r), y + math.sin(b) * r, cz * (1.11 + math.cos(b) * r))
                         for b in [q / 6 * TAU for q in range(6)]])
        m.grid(rows, dark, closed_u=True, closed_v=True)
    # Lantern room: low wall, mullions (the game's glowing lamp sits inside), top ring
    m.lathe([(0.0, 7.1), (0.68, 7.1), (0.68, 7.3), (0.62, 7.3), (0.0, 7.3)], dark, 28)
    for k in range(8):
        a = k / 8 * TAU + 0.2
        m.box((math.cos(a) * 0.64, 7.6, math.sin(a) * 0.64), (0.022, 0.3, 0.022), glass_frame)
    m.lathe([(0.6, 7.88), (0.72, 7.88), (0.72, 7.96), (0.6, 7.96)], dark, 28)
    # Domed roof, ventilator ball and lightning rod
    roof = []
    for k in range(9):
        t = k / 8
        roof.append((0.84 * (1 - t) ** 0.85 + 0.0005, 7.96 + 0.8 * math.sin(t * math.pi / 2) ** 0.9))
    m.lathe(roof, red, 28, cap0=True)
    m.ellipsoid((0.0, 8.82, 0.0), (0.09, 0.09, 0.09), dark, 10, 7)
    m.tube([Vector((0.0, 8.85, 0.0)), Vector((0.0, 9.25, 0.0))], lambda u: lerp(0.014, 0.004, u), dark, segs=5)
    # Door and windows on the side facing the courts (-Z)
    door = [0.10, 0.26, 0.28]
    m.box((0.0, 0.62, -body_r(0.62) + 0.02), (0.24, 0.42, 0.07), door)
    m.box((0.0, 1.07, -body_r(1.07) + 0.03), (0.3, 0.04, 0.08), white)
    for y in (2.95, 5.65):
        r = body_r(y)
        m.box((0.0, y, -r + 0.02), (0.12, 0.2, 0.05), dark)
        m.box((0.0, y - 0.22, -r + 0.03), (0.16, 0.025, 0.07), white)
    return m.finish("lighthouse", sharp_deg=35)


def build_umbrella(name, col):
    m = MB()
    cream = [0.96, 0.93, 0.86]
    ribs, sub, rings = 16, 4, 7
    R_EDGE, Y_TOP, DROP = 1.38, 2.86, 0.5
    nu = ribs * sub

    def panel_col(j):
        return col if (j // sub) % 2 else cream

    def canopy_pt(k, j):
        rho = R_EDGE * k / rings
        frac = (j % sub) / sub
        a = j / nu * TAU
        sag = math.sin(math.pi * frac)
        rr = rho * (1 - 0.035 * sag * (rho / R_EDGE))
        y = Y_TOP - DROP * (rho / R_EDGE) ** 1.35 - 0.035 * sag * (rho / R_EDGE)
        return (math.cos(a) * rr, y, math.sin(a) * rr)

    rows = [[canopy_pt(k, j) for j in range(nu)] for k in range(1, rings + 1)]
    R = m.grid(rows, lambda i, j, c: panel_col(j), closed_u=True)
    top = m.fan(R[0], (0.0, Y_TOP, 0.0), cream)
    # Re-colour the centre fan per panel
    for f in top.link_faces:
        p = f.calc_center_median()
        a = math.atan2(-p.y, p.x) % TAU
        c = panel_col(int(a / TAU * nu))
        for loop in f.loops:
            loop[m.col] = (c[0], c[1], c[2], 1.0)
    # Scalloped valance
    edge = [canopy_pt(rings, j) for j in range(nu)]
    vrows = [edge, []]
    for j in range(nu):
        frac = (j % sub) / sub
        e = edge[j]
        vrows[1].append((e[0] * 1.01, e[1] - 0.075 - 0.065 * math.sin(math.pi * frac), e[2] * 1.01))
    m.grid(vrows, lambda i, j, c: panel_col(j), closed_u=True)
    # Ribs, stretchers, runner, hub, finial, pole top
    wood = [0.32, 0.22, 0.14]
    for r in range(ribs):
        j = r * sub
        pts = []
        for k in range(1, rings + 1):
            p = canopy_pt(k, j)
            pts.append(Vector((p[0], p[1] - 0.018, p[2])))
        m.tube(pts, 0.009, wood, segs=4)
        mid = Vector(canopy_pt(3, j)) + Vector((0.0, -0.02, 0.0))
        a = j / nu * TAU
        m.tube([Vector((math.cos(a) * 0.05, 2.25, math.sin(a) * 0.05)), mid], 0.007, wood, segs=4)
    m.lathe([(0.0, 2.18), (0.055, 2.18), (0.06, 2.26), (0.0, 2.27)], wood, 12)
    m.tube([Vector((0.0, 2.55, 0.0)), Vector((0.0, 2.84, 0.0))], 0.034, cream, segs=10)
    m.ellipsoid((0.0, 2.86, 0.0), (0.07, 0.045, 0.07), cream, 12, 6)
    m.ellipsoid((0.0, 2.95, 0.0), (0.045, 0.06, 0.045), col, 10, 7)
    return m.finish(name, sharp_deg=50)


# =========================================================================
# Racket (racket-bone space: handle along +Y, head centred at y = 0.515, string bed in the XY plane)
# =========================================================================

def head_xy(th):
    a, b = 0.122, 0.162
    return Vector((a * math.cos(th) * (1 + 0.05 * math.sin(th)), 0.515 + b * math.sin(th), 0.0))


def build_racket():
    # Frame: rounded-rectangle section swept around the egg-shaped head
    m = MB(colored=False)
    n = 72
    rows = []
    for i in range(n):
        th = i / n * TAU
        p = head_xy(th)
        tng = (head_xy(th + 0.01) - head_xy(th - 0.01)).normalized()
        out = Vector((tng.y, -tng.x, 0.0))
        if out.dot(p - Vector((0.0, 0.515, 0.0))) < 0:
            out = -out
        ring = []
        for k in range(12):
            a = k / 12 * TAU
            ca, sa = math.cos(a), math.sin(a)
            ex = math.copysign(abs(ca) ** 0.55, ca) * 0.0075
            ez = math.copysign(abs(sa) ** 0.55, sa) * 0.0125
            q = p + out * ex + Vector((0.0, 0.0, ez))
            ring.append((q.x, q.y, q.z))
        rows.append(ring)
    m.grid(rows, None, closed_u=True, closed_v=True)
    frame = m.finish("racket_frame", sharp_deg=60, location=(-0.6, 0, 0))

    # Throat arms + bumper guard (dark composite)
    m = MB(colored=False)
    for s in (-1, 1):
        th = -math.pi / 2 + s * 0.72
        end = head_xy(th)
        p0 = Vector((s * 0.004, 0.232, 0.0))
        ctrl = Vector((s * 0.012, 0.31, 0.0))
        pts = []
        for i in range(9):
            t = i / 8
            pts.append(p0 * (1 - t) ** 2 + ctrl * 2 * (1 - t) * t + end * t * t)
        m.tube(pts, lambda u: lerp(0.0105, 0.0085, u), None, segs=8, ellip=(0.95, 1.25))
    rows = []
    for i in range(15):
        th = math.pi / 2 - 0.55 + 1.1 * i / 14
        p = head_xy(th)
        out = (p - Vector((0.0, 0.515, 0.0))).normalized()
        rows.append([(p + out * (0.0085 + 0.0035 * max(0.0, math.cos(a))) * (1 if math.cos(a) > 0 else 0.3) + Vector((0.0, 0.0, 0.0135 * math.sin(a)))).to_tuple()
                     for a in [k / 10 * TAU for k in range(10)]])
    R = m.grid(rows, None, closed_u=True)
    m.fan(R[0], head_xy(math.pi / 2 - 0.55), None)
    m.fan(R[-1], head_xy(math.pi / 2 + 0.55), None)
    throat = m.finish("racket_throat", sharp_deg=60, location=(-0.6, 0, 0))

    # Octagonal grip with flared butt
    m = MB(colored=False)
    prof = []
    for y, r in ((0.012, 0.0195), (0.03, 0.0185), (0.2, 0.0172), (0.235, 0.0135), (0.25, 0.009)):
        prof.append((r, y))
    m.lathe(prof, None, 8, cap1=True, phase=math.pi / 8)
    grip = m.finish("racket_grip", sharp_deg=30, location=(-0.6, 0, 0))

    m = MB(colored=False)
    m.lathe([(0.0, -0.006), (0.019, -0.006), (0.0215, 0.0), (0.0215, 0.012), (0.019, 0.016), (0.0, 0.016)], None, 16, phase=math.pi / 8)
    cap = m.finish("racket_cap", sharp_deg=50, location=(-0.6, 0, 0))
    return [frame, throat, grip, cap]


# =========================================================================
# Character parts (smooth organic shapes: unions of ellipsoids, voxel remeshed)
# =========================================================================

def blob(name, parts, voxel, smooth_iters, target_tris, location):
    m = MB(colored=False)
    for c, r in parts:
        m.ellipsoid(c, r, None, 28, 18)
    obj = m.finish(name, sharp_deg=None, location=location)
    rm = obj.modifiers.new("Remesh", 'REMESH')
    rm.mode = 'VOXEL'
    rm.voxel_size = voxel
    rm.use_smooth_shade = True
    sm = obj.modifiers.new("Smooth", 'SMOOTH')
    sm.factor = 0.5
    sm.iterations = smooth_iters
    dg = bpy.context.evaluated_depsgraph_get()
    tris = sum(len(p.vertices) - 2 for p in obj.evaluated_get(dg).data.polygons)
    dec = obj.modifiers.new("Decimate", 'DECIMATE')
    dec.ratio = min(1.0, target_tris / max(1, tris))
    # Bake the modifier stack so the .blend shows the final mesh
    dg = bpy.context.evaluated_depsgraph_get()
    me = bpy.data.meshes.new_from_object(obj.evaluated_get(dg))
    old = obj.data
    obj.modifiers.clear()
    obj.data = me
    bpy.data.meshes.remove(old)
    me.name = name
    for p in me.polygons:
        p.use_smooth = True
    return obj


def capsule(p0, p1, r0, r1, n=8):
    """Spheres along a segment; voxel remeshing fuses them into a smooth capsule."""
    out = []
    for i in range(n + 1):
        t = i / n
        r = lerp(r0, r1, t)
        out.append(((lerp(p0[0], p1[0], t), lerp(p0[1], p1[1], t), lerp(p0[2], p1[2], t)), (r, r, r)))
    return out


def post(obj, colfn=None, extra=None, cut=None):
    """Edit a baked blob: cut it with a plane (keeps the side the normal points to),
    paint vertex colours from colfn(game_pos) and/or add extra coloured geometry via extra(mb)."""
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    if cut is not None:
        p0, nrm = cut
        bmesh.ops.bisect_plane(bm, geom=bm.verts[:] + bm.edges[:] + bm.faces[:], dist=1e-6,
                               plane_co=gv(p0), plane_no=gv(Vector(nrm).normalized()), clear_inner=True)
    if colfn is not None or extra is not None:
        m = MB(colored=True, bm=bm)
        for f in bm.faces:
            for loop in f.loops:
                co = loop.vert.co
                c = colfn(Vector((co.x, co.z, -co.y))) if colfn else (1.0, 1.0, 1.0)
                loop[m.col] = (c[0], c[1], c[2], 1.0)
        if extra is not None:
            extra(m)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    for f in bm.faces:
        f.smooth = True
    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()
    if "Col" in obj.data.color_attributes:
        try:
            obj.data.color_attributes.active_color = obj.data.color_attributes["Col"]
        except Exception:
            pass
    return obj


# Crowd spectators (seat space: origin on the bench, +Z faces the court, seated pose).
# Every part is instanced 72 times and tinted per person by the game (skin, hair, shirt, shorts, hat colours),
# so vertex colours here are multipliers: white keeps the person's colour, darker values shade it.
CROWD_LOC = (0, -4, 0)


def build_crowd():
    objs = []
    # Head with neck, ears, nose + painted face
    head = blob("crowd_head", [
        ((0.0, 0.69, 0.0), (0.105, 0.112, 0.1)),
        ((0.0, 0.635, 0.02), (0.085, 0.07, 0.08)),
        ((-0.103, 0.675, -0.005), (0.02, 0.03, 0.016)),
        ((0.103, 0.675, -0.005), (0.02, 0.03, 0.016)),
        ((0.0, 0.662, 0.097), (0.016, 0.02, 0.018)),
    ] + capsule((0.0, 0.5, -0.005), (0.0, 0.61, 0.0), 0.045, 0.045, 4), 0.0055, 3, 620, CROWD_LOC)

    def face(m):
        eye = [0.05, 0.045, 0.045]
        for sx in (-1, 1):
            m.ellipsoid((sx * 0.036, 0.688, 0.088), (0.0135, 0.018, 0.012), eye, 8, 6)
            m.box((sx * 0.038, 0.718, 0.088), (0.017, 0.004, 0.006), [0.25, 0.2, 0.18],
                  rot=Matrix.Rotation(-sx * 0.15, 3, 'Z'))
        for k in range(5):
            u = (k - 2) / 2
            m.ellipsoid((u * 0.024, 0.624 + 0.011 * u * u, 0.094 - 0.01 * u * u), (0.008, 0.005, 0.006), [0.42, 0.14, 0.12], 6, 4)
    post(head, extra=face)
    objs.append(head)

    # Hair styles (instanced separately: each spectator gets one)
    cap = ((0.0, 0.70, -0.008), (0.116, 0.124, 0.112))
    short = blob("crowd_hair_short", [cap, ((0.0, 0.735, 0.03), (0.1, 0.07, 0.085))], 0.006, 2, 300, CROWD_LOC)
    post(short, cut=((0.0, 0.668, 0.045), (0.0, 0.6, -0.8)))
    long_ = blob("crowd_hair_long", [cap, ((0.0, 0.58, -0.065), (0.105, 0.15, 0.06)),
                                     ((-0.088, 0.6, -0.012), (0.034, 0.105, 0.05)), ((0.088, 0.6, -0.012), (0.034, 0.105, 0.05))],
                 0.006, 2, 380, CROWD_LOC)
    post(long_, cut=((0.0, 0.67, 0.05), (0.0, 0.3, -0.95)))
    bun = blob("crowd_hair_bun", [cap, ((0.0, 0.815, -0.07), (0.054, 0.05, 0.054))], 0.006, 2, 340, CROWD_LOC)
    post(bun, cut=((0.0, 0.668, 0.045), (0.0, 0.6, -0.8)))
    objs += [short, long_, bun]

    m = MB()
    band = [0.5, 0.45, 0.4]
    brim = []
    for k in range(5):
        t = k / 4
        brim.append((lerp(0.112, 0.215, t), 0.765 - 0.035 * t * t))
    m.lathe(brim + [(0.215, 0.755), (0.112, 0.752)], [1.0, 1.0, 1.0], 20, center=(0.0, 0.0, -0.01))
    crown = [(0.118, 0.752), (0.12, 0.79), (0.115, 0.82)]
    for k in range(1, 6):
        a = k / 5 * math.pi / 2
        crown.append((0.115 * math.cos(a), 0.82 + 0.05 * math.sin(a)))
    m.lathe(crown, lambda i, j, c: band if c.y < 0.79 else [1.0, 1.0, 1.0], 18, center=(0.0, 0.0, -0.01))
    hat = m.finish("crowd_hat", sharp_deg=60, location=CROWD_LOC)
    objs.append(hat)

    # Torso (shirt colour) with darker collar and hem
    shirt = blob("crowd_shirt", [
        ((0.0, 0.22, 0.0), (0.14, 0.12, 0.1)),
        ((0.0, 0.37, 0.0), (0.155, 0.16, 0.105)),
        ((-0.13, 0.49, 0.0), (0.062, 0.052, 0.072)),
        ((0.13, 0.49, 0.0), (0.062, 0.052, 0.072)),
        ((0.0, 0.13, 0.0), (0.13, 0.06, 0.09)),
        ((0.0, 0.52, -0.005), (0.07, 0.04, 0.06)),
    ], 0.007, 6, 460, CROWD_LOC)
    post(shirt, colfn=lambda p: (0.58, 0.58, 0.6) if p.y > 0.548 else ((0.84, 0.84, 0.86) if p.y < 0.115 else (1.0, 1.0, 1.0)))
    objs.append(shirt)

    # Shorts with thighs (shorts colour) + sneakers in a matching darker shade
    shorts = blob("crowd_shorts", [((0.0, 0.09, -0.01), (0.145, 0.08, 0.1))]
                  + capsule((-0.075, 0.075, 0.0), (-0.075, 0.075, 0.175), 0.068, 0.062, 5)
                  + capsule((0.075, 0.075, 0.0), (0.075, 0.075, 0.175), 0.068, 0.062, 5), 0.007, 5, 300, CROWD_LOC)

    def shoes(m):
        for sx in (-1, 1):
            m.ellipsoid((sx * 0.075, -0.385, 0.3), (0.05, 0.036, 0.085), [0.42, 0.42, 0.45], 10, 6)
            m.lathe([(0.0, -0.425), (0.05, -0.425), (0.054, -0.41), (0.0, -0.41)], [1.0, 1.0, 1.0], 12,
                    center=(sx * 0.075, 0.0, 0.302), sz=1.7)
    post(shorts, colfn=lambda p: (0.82, 0.82, 0.84) if p.y > 0.155 else (1.0, 1.0, 1.0), extra=shoes)
    objs.append(shorts)

    legs = blob("crowd_legs", capsule((-0.075, 0.07, 0.19), (-0.075, -0.36, 0.27), 0.05, 0.04, 8)
                + capsule((0.075, 0.07, 0.19), (0.075, -0.36, 0.27), 0.05, 0.04, 8), 0.008, 2, 220, CROWD_LOC)
    objs.append(legs)

    # Arms in shoulder-pivot space (they hang along -Y and the game rotates them to cheer)
    for side, nm in ((1, "r"), (-1, "l")):
        arm = blob(f"crowd_arm_{nm}", capsule((side * 0.035, -0.01, 0.0), (side * 0.045, -0.26, 0.02), 0.043, 0.035, 8) + [
            ((side * 0.047, -0.3, 0.025), (0.036, 0.042, 0.032)),
            ((side * 0.03, -0.288, 0.05), (0.013, 0.022, 0.013)),
        ], 0.007, 2, 260, CROWD_LOC)
        sleeve = blob(f"crowd_sleeve_{nm}", capsule((side * 0.03, 0.0, 0.0), (side * 0.036, -0.1, 0.006), 0.05, 0.047, 5),
                      0.007, 2, 120, CROWD_LOC)
        objs += [arm, sleeve]
    return objs


def build_head():
    parts = [
        ((0.0, 0.12, 0.0), (0.145, 0.155, 0.14)),        # cranium
        ((0.0, 0.055, 0.035), (0.11, 0.075, 0.1)),       # jaw
        ((0.0, 0.032, 0.07), (0.05, 0.04, 0.062)),       # chin
        ((-0.058, 0.072, 0.07), (0.052, 0.042, 0.05)),   # cheeks
        ((0.058, 0.072, 0.07), (0.052, 0.042, 0.05)),
        ((-0.142, 0.115, -0.005), (0.026, 0.042, 0.022)),  # ears
        ((0.142, 0.115, -0.005), (0.026, 0.042, 0.022)),
        ((0.0, 0.094, 0.139), (0.02, 0.028, 0.024)),     # nose
        ((0.0, 0.112, 0.128), (0.014, 0.03, 0.016)),     # nose bridge
    ]
    return blob("head", parts, 0.0035, 6, 2400, (1.0, 0, 0))


def build_hand():
    parts = [
        ((0.0, -0.262, 0.0), (0.036, 0.04, 0.03)),       # palm
        ((0.0, -0.298, 0.012), (0.035, 0.022, 0.03)),    # curled fingers
        ((0.0, -0.29, 0.034), (0.034, 0.016, 0.014)),    # knuckle row
        ((0.0, -0.268, 0.036), (0.012, 0.026, 0.012)),   # thumb
    ]
    for k in range(4):
        parts.append(((-0.026 + k * 0.0173, -0.302, 0.03), (0.0105, 0.012, 0.012)))
    return blob("hand", parts, 0.0026, 2, 900, (1.3, 0, 0))


def build_shoe():
    """Sneaker in lower-leg space: upper (vertex-coloured, multiplied by the player's shoe colour) + white/tan trim."""
    z0, z1 = -0.064, 0.184
    NS = 22
    y_sole_top = -0.433

    def hw(s):
        w = 0.051 + 0.009 * smoothstep(0.05, 0.6, s)
        if s < 0.16:
            w *= math.sqrt(max(0.0, 1 - (1 - s / 0.16) ** 2)) * 0.82 + 0.18
        if s > 0.72:
            w *= math.sqrt(max(0.0, 1 - ((s - 0.72) / 0.28) ** 2)) * 0.88 + 0.12
        return w

    def spring(s):
        return 0.014 * max(0.0, (s - 0.78) / 0.22) ** 2

    def top(s):
        if s < 0.32:
            h = 0.088
        elif s < 0.76:
            h = lerp(0.082, 0.05, smoothstep(0.32, 0.76, s))
        else:
            h = 0.05 * math.sqrt(max(0.0, 1 - ((s - 0.76) / 0.24) ** 2)) * 0.8 + 0.01
        return -0.445 + h + spring(s)

    m = MB()
    rows = []
    NR = 18
    NU = 26
    for i in range(NU + 1):
        s = 0.5 - 0.5 * math.cos(math.pi * i / NU)        # denser stations at heel and toe
        z = lerp(z0, z1, s)
        w = 0.051 + 0.009 * smoothstep(0.05, 0.6, s)
        yb = -0.44 + spring(s)
        h = 0.088 if s < 0.32 else lerp(0.082, 0.05, smoothstep(0.32, 0.76, s))
        yt = -0.445 + h + spring(s)
        cy = yb + 0.38 * (yt - yb)
        if s < 0.14:                                    # rounded heel counter
            ex = 0.3 + 0.7 * math.sqrt(max(0.0, 1 - (1 - s / 0.14) ** 2))
            ey = 0.75 + 0.25 * ex
        elif s > 0.7:                                   # dome-shaped toe box closing to a point
            ex = math.sqrt(max(0.0, 1 - ((s - 0.7) / 0.3) ** 2))
            ey = ex
        else:
            ex = ey = 1.0
        ring = []
        for k in range(NR):
            a = k / NR * TAU
            ca, sa = math.cos(a), math.sin(a)
            x = math.copysign(abs(ca) ** 0.6, ca) * w * ex
            y = yb + (yt - yb) * (0.5 + 0.5 * math.copysign(abs(sa) ** 0.55, sa))
            ring.append((x, cy + (y - cy) * ey, z))
        rows.append(ring)
    opening, upper = [0.16, 0.16, 0.17], [1.0, 1.0, 1.0]

    def ucol(i, j, c):
        s = (c.z - z0) / (z1 - z0)
        if s < 0.33 and c.y > top(s) - 0.008 and abs(c.x) < hw(s) * 0.8:
            return opening
        return upper
    R = m.grid(rows, ucol, closed_u=True)
    m.fan(R[0], (0.0, rows[0][0][1] + 0.01, z0 - 0.003), upper)
    bmesh.ops.remove_doubles(m.bm, verts=m.bm.verts[:], dist=1e-6)
    up = m.finish("shoe_upper", sharp_deg=None, location=(1.6, 0, 0))

    # Trim: sole, toe bumper, laces, heel tab, side stripe
    m = MB()
    white = [1.0, 1.0, 1.0]
    outline = []
    for i in range(NS + 1):
        s = i / NS
        outline.append((s, hw(s) * 1.07 + 0.003))
    ring_pts = [(w, s) for s, w in outline] + [(-w, s) for s, w in reversed(outline)]
    sole_rows = []
    for yy, inset in ((-0.463, 0.006), (-0.458, 0.0), (y_sole_top, 0.0), (y_sole_top + 0.003, 0.004)):
        row = []
        for x, s in ring_pts:
            z = lerp(z0 - 0.003, z1 + 0.006, s)
            xx = x - math.copysign(inset, x)
            row.append((xx, yy + spring(s), z))
        sole_rows.append(row)
    R = m.grid(sole_rows, white, closed_u=True)
    m.fan(R[0], (0.0, -0.463, (z0 + z1) / 2), white)
    m.fan(R[-1], (0.0, y_sole_top + 0.003 + 0.004, (z0 + z1) / 2), white)
    # Laces across the instep
    for k in range(4):
        s = 0.40 + k * 0.085
        z = lerp(z0, z1, s)
        y = top(s) + 0.002
        m.box((0.0, y, z), (0.026, 0.0035, 0.0045), white)
    # Heel tab
    m.box((0.0, top(0.0) - 0.008, z0 + 0.004), (0.014, 0.014, 0.006), white)
    # Side stripes (one each side)
    for sx in (-1, 1):
        pts = []
        for k in range(7):
            s = 0.22 + 0.4 * k / 6
            y = lerp(-0.425, top(s) - 0.012, k / 6)
            pts.append(Vector((sx * (hw(s) + 0.0015), y, lerp(z0, z1, s))))
        m.tube(pts, 0.0045, white, segs=4, ellip=(0.35, 1.0))
    trim = m.finish("shoe_trim", sharp_deg=50, location=(1.6, 0, 0))
    return [up, trim]


# =========================================================================
# Export + embed
# =========================================================================

def export_glb(path):
    props = {p.identifier for p in bpy.ops.export_scene.gltf.get_rna_type().properties}
    opts = dict(filepath=path, export_format='GLB', use_selection=False, export_apply=True, export_yup=True,
                export_texcoords=False, export_normals=True, export_tangents=False, export_materials='NONE',
                export_vertex_color='ACTIVE', export_all_vertex_colors=False, export_animations=False,
                export_skins=False, export_morph=False, export_cameras=False, export_lights=False, export_extras=False,
                export_meshopt_compression_enable=True)  # EXT_meshopt_compression: the game decodes it with three's MeshoptDecoder
    bpy.ops.export_scene.gltf(**{k: v for k, v in opts.items() if k == 'filepath' or k in props})


def embed(glb_path, html_path):
    data = open(glb_path, "rb").read()
    packed = gzip.compress(data, compresslevel=9, mtime=0)
    b64 = base64.b64encode(packed).decode("ascii")
    lines = "\n".join(b64[i:i + 160] for i in range(0, len(b64), 160))
    tag = f'<script id="lagoon-models" type="application/octet-stream">\n{lines}\n</script>'
    html = open(html_path, "r", encoding="utf-8").read()
    pat = re.compile(r'<script id="lagoon-models" type="application/octet-stream">.*?</script>', re.S)
    if pat.search(html):
        html = pat.sub(lambda _m: tag, html, count=1)
    else:
        marker = '<script type="importmap">'
        if marker not in html:
            raise SystemExit("Could not find the import map in the HTML to place the models before it.")
        html = html.replace(marker, "<!-- Blender models: generated by models/build_models.py (gzip + base64 GLB). Do not edit by hand. -->\n" + tag + "\n" + marker, 1)
    with open(html_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(html)
    return len(data), len(packed), len(b64)


def main():
    if "--from-blend" in sys.argv:
        # Export whatever is in models/lagoon-models.blend (e.g. after editing it by hand in Blender).
        # Object names must stay the same: the game looks models up by name.
        bpy.ops.wm.open_mainfile(filepath=BLEND)
        export_glb(GLB)
        raw, packed, b64 = embed(GLB, HTML)
        print(f"Embedded {raw/1024:.0f} KB GLB from the .blend as {packed/1024:.0f} KB gzip into {os.path.basename(HTML)}")
        return
    bpy.ops.wm.read_factory_settings(use_empty=True)
    built = []
    built.append(build_palm())
    built.append(build_sailboat())
    built[-1].location = (6, 0, 0)
    built.append(build_lighthouse())
    built[-1].location = (-6, 0, 0)
    built.append(build_umbrella("umbrella_gold", [0.98, 0.74, 0.18]))
    built[-1].location = (0, 6, 0)
    built.append(build_umbrella("umbrella_teal", [0.18, 0.72, 0.68]))
    built[-1].location = (3.5, 6, 0)
    built += build_racket()
    built.append(build_head())
    built.append(build_hand())
    built += build_shoe()
    built += build_crowd()

    for o in built:
        tris = sum(len(p.vertices) - 2 for p in o.data.polygons)
        print(f"  {o.name:16s} {len(o.data.vertices):6d} verts {tris:6d} tris")

    bpy.ops.wm.save_as_mainfile(filepath=BLEND, compress=True)
    export_glb(GLB)
    if "--no-embed" in sys.argv:
        print("Skipped embedding (--no-embed).")
        return
    raw, packed, b64 = embed(GLB, HTML)
    print(f"Embedded {raw/1024:.0f} KB GLB as {packed/1024:.0f} KB gzip ({b64/1024:.0f} KB base64) into {os.path.basename(HTML)}")


main()
