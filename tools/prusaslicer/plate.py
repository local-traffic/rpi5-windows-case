#!/usr/bin/env python3
"""Orient STL parts for printing and arrange them on the bed -> PrusaSlicer 3MF.

Pure standard library (numpy is broken in the agent sandbox), so it runs anywhere.

    tools/prusaslicer/plate.py stl/plate.json        # use a plan file
    tools/prusaslicer/plate.py --explain a.stl b.stl -o out.3mf      # ad hoc, auto-orient
    tools/prusaslicer/plate.py plan.json --only Case,Cover           # subset of a plan

Plan file (JSON, paths relative to the plan file):
    {
      "printer": "../tools/prusaslicer/printers/coreone.json",
      "output": "plates/plate.3mf",
      "gap": 6,
      "parts": [
        {"file": "case.stl", "name": "Case", "down": "-z"},
        {"file": "connector.stl", "down": "auto", "copies": 2}
      ]
    }

"down" = which side of the STL (as exported) goes onto the bed:
    -z keep as is, +z flip upside down, +x/-x/+y/-y lay on that side,
    [x,y,z] arbitrary direction, "auto" = pick automatically (see auto_down).
"settings" = per-object PrusaSlicer overrides written into the 3MF, e.g.
{"support_material": 1, "support_material_auto": 1, "support_material_buildplate_only": 1}.
"spin" = extra rotation about Z in degrees, or "auto" (default) = minimal
footprint rectangle, long side along X; the packer may add 90 degrees.
"""
import argparse
import json
import math
import os
import struct
import sys
import zipfile
import zlib

# ---------------------------------------------------------------- vector math


def sub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
def dot(a, b): return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
def cross(a, b): return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
def norm(a): return math.sqrt(dot(a, a))


def unit(a):
    n = norm(a)
    return (a[0] / n, a[1] / n, a[2] / n) if n > 1e-12 else (0.0, 0.0, 0.0)


def matmul(m, n):
    return [[sum(m[i][k] * n[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def apply(m, v):
    return (m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
            m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
            m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2])


def rot_z(deg):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return [[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]]


def rot_to_down(d):
    """Rotation that maps direction d onto -Z."""
    d = unit(d)
    t = (0.0, 0.0, -1.0)
    c = dot(d, t)
    if c > 1 - 1e-9:
        return [[1.0, 0, 0], [0, 1.0, 0], [0, 0, 1.0]]
    if c < -1 + 1e-9:  # d = +Z: flip about X
        return [[1.0, 0, 0], [0, -1.0, 0], [0, 0, -1.0]]
    ax = unit(cross(d, t))
    s = math.sqrt(1 - c * c)
    x, y, z = ax
    C = 1 - c
    return [[c + x * x * C, x * y * C - z * s, x * z * C + y * s],
            [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
            [z * x * C - y * s, z * y * C + x * s, c + z * z * C]]


# ---------------------------------------------------------------- STL io


def read_stl(path):
    """Return list of triangles ((x,y,z),(x,y,z),(x,y,z))."""
    data = open(path, 'rb').read()
    tris = []
    if len(data) >= 84:
        n = struct.unpack('<I', data[80:84])[0]
        if 84 + 50 * n == len(data):
            for i in range(n):
                v = struct.unpack_from('<12f', data, 84 + 50 * i)
                tris.append((v[3:6], v[6:9], v[9:12]))
            return tris
    # ASCII
    pts = []
    for line in data.decode('ascii', 'replace').splitlines():
        p = line.split()
        if p and p[0] == 'vertex':
            pts.append(tuple(float(x) for x in p[1:4]))
            if len(pts) == 3:
                tris.append(tuple(pts))
                pts = []
    return tris


# ---------------------------------------------------------------- orientation

AXES = {'-z': (0, 0, -1), '+z': (0, 0, 1), '-x': (-1, 0, 0), '+x': (1, 0, 0),
        '-y': (0, -1, 0), '+y': (0, 1, 0)}


def candidate_scores(tris, overhang_deg=45.0, tol=0.05):
    """Score each planar facing direction as a possible bed face.

    For each distinct facet normal n (clustered), the part rests on the faces
    with normal n that lie on the extreme plane max(v.n). Score:
      contact  = area touching the bed
      overhang = area of downward faces steeper than overhang_deg not on the bed
      height   = resulting Z height
    """
    clusters = {}
    for t in tris:
        nrm = cross(sub(t[1], t[0]), sub(t[2], t[0]))
        a = norm(nrm) / 2
        if a < 1e-9:
            continue
        n = unit(nrm)
        key = tuple(round(c, 2) for c in n)
        clusters.setdefault(key, [n, 0.0])[1] += a
    big = sorted(clusters.values(), key=lambda c: -c[1])[:24]
    verts = [v for t in tris for v in t]
    out = []
    cos_lim = math.cos(math.radians(90 - overhang_deg))
    for n, _ in big:
        dmax = max(dot(v, n) for v in verts)
        dmin = min(dot(v, n) for v in verts)
        contact = overhang = 0.0
        for t in tris:
            nrm = cross(sub(t[1], t[0]), sub(t[2], t[0]))
            a = norm(nrm) / 2
            if a < 1e-9:
                continue
            fn = unit(nrm)
            c = dot(fn, n)  # >0 means facing the bed
            on_bed = all(dmax - dot(v, n) < tol for v in t)
            if on_bed and c > 0.999:
                contact += a
            elif c > cos_lim and not on_bed:
                overhang += a
        out.append({'down': n, 'contact': contact, 'overhang': overhang, 'height': dmax - dmin})
    return out


def auto_down(tris):
    """Largest bed contact wins; among those within 15% of it, least overhang, then lowest."""
    cands = candidate_scores(tris)
    best = max(c['contact'] for c in cands)
    good = [c for c in cands if c['contact'] >= 0.85 * best]
    good.sort(key=lambda c: (round(c['overhang']), c['height']))
    return good[0], cands


def fmt_dir(n):
    for k, v in AXES.items():
        if dot(n, v) > 0.999:
            return k
    return '[%.2f, %.2f, %.2f]' % tuple(n)


# ---------------------------------------------------------------- 2D geometry


def hull2d(pts):
    pts = sorted(set((round(x, 3), round(y, 3)) for x, y in pts))
    if len(pts) < 3:
        return pts

    def half(seq):
        h = []
        for p in seq:
            while len(h) >= 2 and (h[-1][0] - h[-2][0]) * (p[1] - h[-2][1]) - (h[-1][1] - h[-2][1]) * (p[0] - h[-2][0]) <= 0:
                h.pop()
            h.append(p)
        return h
    lo, hi = half(pts), half(reversed(pts))
    return lo[:-1] + hi[:-1]


def min_area_spin(hull):
    """Z rotation (deg) giving the smallest bounding rectangle, long side along X."""
    best = None
    for i in range(len(hull)):
        a, b = hull[i], hull[(i + 1) % len(hull)]
        ang = -math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
        r = rot_z(ang)
        xs = [r[0][0] * x + r[0][1] * y for x, y in hull]
        ys = [r[1][0] * x + r[1][1] * y for x, y in hull]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        if h > w:
            ang += 90
            w, h = h, w
        if best is None or w * h < best[0] - 1e-6:
            best = (w * h, ang)
    return best[1] if best else 0.0


# ---------------------------------------------------------------- packing


def pack(rects, W, H, gap):
    """Skyline bottom-left packing. rects: list of (w,h). Returns list of (x,y,rotated) or None."""
    order = sorted(range(len(rects)), key=lambda i: -rects[i][0] * rects[i][1])
    sky = [(0.0, W + gap, 0.0)]  # segments (x, width, y); widths include one trailing gap
    res = [None] * len(rects)
    for i in order:
        best = None
        for rot in (False, True):
            w, h = rects[i][::-1] if rot else rects[i]
            w, h = w + gap, h + gap
            for s in range(len(sky)):
                x = sky[s][0]
                if x + w > W + gap + 1e-6:
                    continue
                # y = max of skyline over [x, x+w)
                y, j = 0.0, s
                while j < len(sky) and sky[j][0] < x + w - 1e-9:
                    y = max(y, sky[j][2])
                    j += 1
                if y + h > H + gap + 1e-6:
                    continue
                key = (y + h, x)
                if best is None or key < best[0]:
                    best = (key, x, y, w, h, rot)
        if best is None:
            continue
        _, x, y, w, h, rot = best
        res[i] = (x, y, rot)
        new = []
        for sx, sw, sy in sky:
            if sx + sw <= x or sx >= x + w:
                new.append((sx, sw, sy))
                continue
            if sx < x:
                new.append((sx, x - sx, sy))
            if sx + sw > x + w:
                new.append((x + w, sx + sw - x - w, sy))
        new.append((x, w, y + h))
        sky = sorted(new)
    return res


# ---------------------------------------------------------------- part prep


class Part:
    def __init__(self, name, tris, down, spin):
        self.name = name
        R = rot_to_down(down)
        tris = [tuple(apply(R, v) for v in t) for t in tris]
        hull = hull2d([(v[0], v[1]) for t in tris for v in t])
        ang = min_area_spin(hull) if spin == 'auto' else float(spin)
        Rz = rot_z(ang)
        self.rotation = matmul(Rz, R)
        self.tris = [tuple(apply(Rz, v) for v in t) for t in tris]
        self._normalize()

    def _normalize(self):
        vs = [v for t in self.tris for v in t]
        mn = [min(v[k] for v in vs) for k in range(3)]
        mx = [max(v[k] for v in vs) for k in range(3)]
        cx, cy = (mn[0] + mx[0]) / 2, (mn[1] + mx[1]) / 2
        self.tris = [tuple((v[0] - cx, v[1] - cy, v[2] - mn[2]) for v in t) for t in self.tris]
        self.size = (mx[0] - mn[0], mx[1] - mn[1], mx[2] - mn[2])
        self.hull = hull2d([(v[0], v[1]) for t in self.tris for v in t])

    def turn90(self):
        r = rot_z(90)
        self.rotation = matmul(r, self.rotation)
        self.tris = [tuple(apply(r, v) for v in t) for t in self.tris]
        self._normalize()


# ---------------------------------------------------------------- 3MF writer


def write_3mf(path, placed):
    """placed: list of (Part, x, y) with x,y = centre of the part on the bed."""
    objs, build, cfg = [], [], []
    for oid, (p, x, y) in enumerate(placed, 1):
        index, verts, faces = {}, [], []
        for t in p.tris:
            f = []
            for v in t:
                k = (round(v[0], 5), round(v[1], 5), round(v[2], 5))
                if k not in index:
                    index[k] = len(verts)
                    verts.append(k)
                f.append(index[k])
            if len(set(f)) == 3:
                faces.append(f)
        vx = ''.join('<vertex x="%g" y="%g" z="%g"/>' % v for v in verts)
        fx = ''.join('<triangle v1="%d" v2="%d" v3="%d"/>' % tuple(f) for f in faces)
        objs.append('<object id="%d" type="model"><mesh><vertices>%s</vertices><triangles>%s</triangles></mesh></object>'
                    % (oid, vx, fx))
        build.append('<item objectid="%d" transform="1 0 0 0 1 0 0 0 1 %.4f %.4f 0" printable="1"/>' % (oid, x, y))
        nm = xml_escape(p.name)
        extra = ''.join('  <metadata type="object" key="%s" value="%s"/>\n' % (xml_escape(k), xml_escape(str(v)))
                        for k, v in getattr(p, 'settings', {}).items())
        cfg.append(' <object id="%d" instances_count="1">\n  <metadata type="object" key="name" value="%s"/>\n' % (oid, nm)
                   + extra +
                   '  <volume firstid="0" lastid="%d">\n   <metadata type="volume" key="name" value="%s"/>\n'
                   '   <metadata type="volume" key="volume_type" value="ModelPart"/>\n  </volume>\n </object>\n'
                   % (len(faces) - 1, nm))
    model = ('<?xml version="1.0" encoding="UTF-8"?>\n'
             '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
             'xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06">\n'
             ' <metadata name="slic3rpe:Version3mf">1</metadata>\n <metadata name="Application">solidcraft-mcp tools/prusaslicer/plate.py</metadata>\n'
             ' <resources>' + ''.join(objs) + '</resources>\n <build>' + ''.join(build) + '</build>\n</model>\n')
    ctypes = ('<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
              '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
              '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
              '<Default Extension="png" ContentType="image/png"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
            '</Relationships>')
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', ctypes)
        z.writestr('_rels/.rels', rels)
        z.writestr('3D/3dmodel.model', model)
        z.writestr('Metadata/Slic3r_PE_model.config', '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n' + ''.join(cfg) + '</config>\n')


def xml_escape(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


# ---------------------------------------------------------------- PNG preview

PALETTE = [(66, 133, 244), (219, 68, 55), (244, 180, 0), (15, 157, 88), (171, 71, 188), (0, 172, 193),
           (255, 112, 67), (158, 157, 36), (92, 107, 192), (240, 98, 146)]


def write_png(path, W, H, placed, scale=3):
    w, h = int(W * scale), int(H * scale)
    img = bytearray([238, 238, 238] * w * h)

    def setpx(x, y, c):
        if 0 <= x < w and 0 <= y < h:
            i = ((h - 1 - y) * w + x) * 3
            img[i:i + 3] = bytes(c)
    for gx in range(0, int(W) + 1, 10):  # 10 mm grid
        for y in range(h):
            setpx(int(gx * scale), y, (205, 205, 205) if gx % 50 else (170, 170, 170))
    for gy in range(0, int(H) + 1, 10):
        for x in range(w):
            setpx(x, int(gy * scale), (205, 205, 205) if gy % 50 else (170, 170, 170))
    for n, (p, cx, cy) in enumerate(placed):
        col = PALETTE[n % len(PALETTE)]
        poly = [((x + cx) * scale, (y + cy) * scale) for x, y in p.hull]
        ys = [q[1] for q in poly]
        for py in range(max(0, int(min(ys))), min(h, int(max(ys)) + 1)):
            yy = py + 0.5
            xs = []
            for i in range(len(poly)):
                (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % len(poly)]
                if (y1 <= yy < y2) or (y2 <= yy < y1):
                    xs.append(x1 + (yy - y1) * (x2 - x1) / (y2 - y1))
            xs.sort()
            for a, b in zip(xs[::2], xs[1::2]):
                for px in range(int(a), int(b) + 1):
                    setpx(px, py, col)
        # mark centre and a dot pattern proportional to height is overkill: just a cross
        for d in range(-4, 5):
            setpx(int(cx * scale) + d, int(cy * scale), (0, 0, 0))
            setpx(int(cx * scale), int(cy * scale) + d, (0, 0, 0))
    raw = b''.join(b'\x00' + bytes(img[r * w * 3:(r + 1) * w * 3]) for r in range(h))

    def chunk(t, d):
        c = struct.pack('>I', len(d)) + t + d
        return c + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)) + \
        chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b'')
    open(path, 'wb').write(png)


# ---------------------------------------------------------------- main


def make_plate(inputs, output=None, printer=None, gap=None, only=None, explain=False):
    """Build a plate. inputs: plan .json and/or .stl paths. Returns dict with lines, output, png, placed, left."""
    here = os.path.dirname(os.path.abspath(__file__))
    plan = {'parts': []}
    base = os.getcwd()
    for inp in inputs:
        if inp.endswith('.json'):
            plan = json.load(open(inp))
            base = os.path.dirname(os.path.abspath(inp))
        else:
            plan['parts'].append({'file': os.path.abspath(inp)})
    printer_path = printer or (os.path.join(base, plan['printer']) if 'printer' in plan
                               else os.path.join(here, 'printers', 'coreone.json'))
    printer = json.load(open(printer_path))
    W, H = printer['bed'][0], printer['bed'][1]
    margin = printer.get('margin', 5)
    gap = gap if gap is not None else plan.get('gap', printer.get('gap', 6))
    out = output or (os.path.join(base, plan['output']) if 'output' in plan else os.path.join(base, 'plate.3mf'))
    out = os.path.abspath(out)
    if isinstance(only, str):
        only = set(only.split(','))
    lines = []

    parts = []
    for spec in plan['parts']:
        f = spec['file'] if os.path.isabs(spec['file']) else os.path.join(base, spec['file'])
        name = spec.get('name') or os.path.splitext(os.path.basename(f))[0]
        if only and name not in only:
            continue
        tris = read_stl(f)
        down = spec.get('down', 'auto')
        why = ''
        if down == 'auto':
            best, cands = auto_down(tris)
            d = best['down']
            why = 'auto: contact %.0f mm2, overhang %.0f mm2' % (best['contact'], best['overhang'])
            if explain:
                for c in sorted(cands, key=lambda c: -c['contact'])[:6]:
                    lines.append('    %-12s %-22s contact %7.1f  overhang %7.1f  height %5.1f' %
                                 (name, fmt_dir(c['down']), c['contact'], c['overhang'], c['height']))
        else:
            d = AXES[down] if isinstance(down, str) else tuple(down)
        n = spec.get('copies', 1)
        for k in range(n):
            p = Part(name if n == 1 else '%s_%d' % (name, k + 1), tris, d, spec.get('spin', 'auto'))
            p.note = '%s down %s %s' % (os.path.basename(f), fmt_dir(d), why)
            p.settings = spec.get('settings', {})
            if p.settings:
                p.note += ' settings: ' + ', '.join('%s=%s' % kv for kv in p.settings.items())
            parts.append(p)

    if not parts:
        raise ValueError('no parts selected')
    res = pack([(p.size[0], p.size[1]) for p in parts], W - 2 * margin, H - 2 * margin, gap)
    placed, left = [], []
    for p, r in zip(parts, res):
        if r is None:
            left.append(p)
            continue
        x, y, rot = r
        if rot:
            p.turn90()
        placed.append([p, x + p.size[0] / 2, y + p.size[1] / 2])
    if placed:  # centre the group on the bed
        x0 = min(cx - p.size[0] / 2 for p, cx, cy in placed)
        x1 = max(cx + p.size[0] / 2 for p, cx, cy in placed)
        y0 = min(cy - p.size[1] / 2 for p, cx, cy in placed)
        y1 = max(cy + p.size[1] / 2 for p, cx, cy in placed)
        dx, dy = (W - (x1 - x0)) / 2 - x0, (H - (y1 - y0)) / 2 - y0
        for q in placed:
            q[1] += dx
            q[2] += dy
    max_h = printer.get('max_height', 1e9)
    lines.append('printer %s, bed %gx%g, gap %g mm' % (printer.get('name', '?'), W, H, gap))
    for p, cx, cy in placed:
        warn = '  !! taller than %g' % max_h if p.size[2] > max_h else ''
        lines.append('  %-14s at (%6.1f, %6.1f)  %5.1f x %5.1f x %5.1f  %s%s' % (p.name, cx, cy, *p.size, p.note, warn))
    for p in left:
        lines.append('  %-14s DID NOT FIT (%5.1f x %5.1f) - place manually / next plate' % (p.name, p.size[0], p.size[1]))
    write_3mf(out, [tuple(q) for q in placed])
    png = os.path.splitext(out)[0] + '.png'
    write_png(png, W, H, placed)
    lines += ['wrote ' + out, 'wrote ' + png]
    return {'lines': lines, 'output': out, 'png': png, 'printer': printer,
            'placed': [p.name for p, _, _ in placed], 'left': [p.name for p in left]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('inputs', nargs='+', help='plan .json or .stl files')
    ap.add_argument('-o', '--output', help='output .3mf (default: from plan or plate.3mf)')
    ap.add_argument('--printer', help='printer json (default: plan or tools/prusaslicer/printers/coreone.json)')
    ap.add_argument('--gap', type=float, help='spacing between parts, mm')
    ap.add_argument('--only', help='comma separated part names to include')
    ap.add_argument('--explain', action='store_true', help='print orientation candidates')
    a = ap.parse_args()
    r = make_plate(a.inputs, a.output, a.printer, a.gap, a.only, a.explain)
    print('\n'.join(r['lines']))


if __name__ == '__main__':
    main()
