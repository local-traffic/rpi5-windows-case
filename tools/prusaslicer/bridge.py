#!/usr/bin/env python3
"""Host-side bridge: lets the sandboxed MCP server run PrusaSlicer.

Claude Code runs inside bwrap without /var/lib/flatpak or the D-Bus socket, but
shares the network namespace. Run this OUTSIDE the sandbox (a normal terminal):

    python3 tools/prusaslicer/bridge.py            # listens on 127.0.0.1:7879

POST /run  {"token": "...", "mode": "gui"|"cli", "args": [...], "timeout": 600}
  gui -> starts PrusaSlicer detached (use --single-instance to feed the open window)
  cli -> runs PrusaSlicer, returns {"code", "output"}

Guards (it is a hole in the sandbox, keep it narrow):
  - only runs PrusaSlicer, never a shell; 127.0.0.1 only; token in tools/prusaslicer/.bridge-token
  - file arguments must be inside this repo
  - refuses --post-process and any .3mf/.ini whose config sets post_process
    (PrusaSlicer would run that script on the host)
"""
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
CMD = shlex.split(os.environ.get('PRUSASLICER_CMD', 'flatpak run com.prusa3d.PrusaSlicer'))
PORT = int(os.environ.get('PRUSASLICER_BRIDGE_PORT', '7879'))
TOKEN_FILE = os.path.join(HERE, '.bridge-token')
LOG = os.path.join(HERE, '.prusaslicer-gui.log')
POST_RE = re.compile(r'^;?\s*post_process\s*=\s*(.+)$', re.M)


def check_args(args):
    for a in args:
        if not isinstance(a, str):
            raise ValueError('args must be strings')
        if a.startswith('--post-process') or a.startswith('--post_process'):
            raise ValueError('--post-process is not allowed through the bridge')
        v = a.split('=', 1)[1] if a.startswith('--') and '=' in a else a
        if v.startswith('-'):
            continue
        p = os.path.realpath(os.path.join(ROOT, os.path.expanduser(v)))
        if v.startswith(('/', '~')) or '..' in v.split('/') or os.path.exists(p):
            if not (p == ROOT or p.startswith(ROOT + os.sep)):
                raise ValueError('path outside repo: %s' % a)
            check_file(p)


def check_file(p):
    text = ''
    if p.endswith('.3mf') and os.path.exists(p):
        with zipfile.ZipFile(p) as z:
            for n in z.namelist():
                if n.endswith('.config') or n.endswith('.ini'):
                    text += z.read(n).decode('utf-8', 'replace')
    elif p.endswith('.ini') and os.path.exists(p):
        text = open(p, encoding='utf-8', errors='replace').read()
    for m in POST_RE.finditer(text):
        if m.group(1).strip() not in ('', '""'):
            raise ValueError('%s sets post_process = %s - refused' % (p, m.group(1).strip()))


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._send(200, {'ok': True, 'cmd': CMD})

    def do_POST(self):
        try:
            req = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            if req.get('token') != TOKEN:
                return self._send(403, {'error': 'bad token'})
            args = req.get('args', [])
            check_args(args)
            print('%s %s' % (req.get('mode'), ' '.join(shlex.quote(a) for a in args)), flush=True)
            if req.get('mode') == 'gui':
                log = open(LOG, 'ab')
                subprocess.Popen(CMD + args, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                 start_new_session=True, cwd=ROOT)
                return self._send(200, {'code': 0, 'output': 'started'})
            p = subprocess.run(CMD + args, capture_output=True, text=True, cwd=ROOT,
                               timeout=float(req.get('timeout', 600)))
            self._send(200, {'code': p.returncode, 'output': (p.stdout + p.stderr)[-6000:]})
        except Exception as e:
            self._send(400, {'error': '%s: %s' % (type(e).__name__, e)})

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    TOKEN = secrets.token_hex(16)
    with open(TOKEN_FILE, 'w') as f:
        f.write(TOKEN)
    os.chmod(TOKEN_FILE, 0o600)
    print('PrusaSlicer bridge on 127.0.0.1:%d -> %s (repo %s)' % (PORT, ' '.join(CMD), ROOT), flush=True)
    try:
        ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
