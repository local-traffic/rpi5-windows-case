#!/usr/bin/env python3
"""Minimal MCP server (stdio, stdlib only) for PrusaSlicer.

PrusaSlicer has no remote-control API, so "live" means: build the plate here,
then hand the 3MF to the running PrusaSlicer window (--single-instance loads it
into the existing instance) and/or slice it with the CLI.

Claude Code runs in bwrap without flatpak access, so PrusaSlicer is run through
tools/prusaslicer/bridge.py (started on the host, 127.0.0.1:7879); direct run is the fallback.

Registered in ../.mcp.json as server "prusaslicer". Env overrides:
  PRUSASLICER_CMD   command to run PrusaSlicer (default: flatpak run com.prusa3d.PrusaSlicer)
"""
import base64
import json
import os
import re
import shlex
import subprocess
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import plate  # noqa: E402

CMD = shlex.split(os.environ.get('PRUSASLICER_CMD', 'flatpak run com.prusa3d.PrusaSlicer'))
LOG = os.path.join(HERE, '.prusaslicer-gui.log')


def resolve(p):
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


BRIDGE = os.environ.get('PRUSASLICER_BRIDGE', 'http://127.0.0.1:7879')
TOKEN_FILE = os.path.join(HERE, '.bridge-token')


def bridge(mode, args, timeout=600):
    """Run PrusaSlicer via the host-side bridge (tools/prusaslicer/bridge.py). None if unreachable."""
    import urllib.error
    import urllib.request
    try:
        token = open(TOKEN_FILE).read().strip()
    except OSError:
        return None
    req = urllib.request.Request(BRIDGE + '/run', json.dumps(
        {'token': token, 'mode': mode, 'args': args, 'timeout': timeout}).encode(),
        {'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout + 30) as r:
            res = json.loads(r.read())
    except urllib.error.HTTPError as e:
        res = json.loads(e.read() or b'{}')
    except (urllib.error.URLError, ConnectionError):
        return None
    if 'error' in res:
        raise RuntimeError('bridge: ' + res['error'])
    return res['code'], res['output']


NO_BRIDGE = ('PrusaSlicer bridge not reachable at %s and direct run failed. Start it OUTSIDE the Claude sandbox: '
             'python3 %s' % (BRIDGE, os.path.join(HERE, 'bridge.py')))


def gui_open(path):
    """Load a file into the running PrusaSlicer (starts one if none is running)."""
    if bridge('gui', ['--single-instance', path]) is not None:
        return 'sent %s to PrusaSlicer GUI via bridge (--single-instance); GUI log: %s' % (path, LOG)
    try:
        log = open(LOG, 'ab')
        start = log.tell()
        proc = subprocess.Popen(CMD + ['--single-instance', path], stdout=log, stderr=log,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return NO_BRIDGE
    # A failing launcher (e.g. flatpak inside bwrap) exits at once with an error; a new window keeps running,
    # a hand-off to a running window exits after logging "Another instance found".
    try:
        code = proc.wait(5)
    except subprocess.TimeoutExpired:
        code = None
    if code:
        with open(LOG, 'rb') as f:
            f.seek(start)
            out = f.read().decode(errors='replace')
        if 'Another instance found' not in out:
            return '%s\n%s' % (out.strip()[-500:], NO_BRIDGE)
    return 'sent %s to PrusaSlicer GUI directly (--single-instance; no bridge, fine outside bwrap); GUI log: %s' % (path, LOG)


def run_cli(args, timeout=600):
    r = bridge('cli', args, timeout)
    if r is not None:
        return r
    try:
        p = subprocess.run(CMD + args, capture_output=True, text=True, timeout=timeout)
    except OSError as e:
        return 127, '%s\n%s' % (e, NO_BRIDGE)
    out = (p.stdout + p.stderr).strip()
    if 'opendir(/var/lib/flatpak' in out:
        out += '\n' + NO_BRIDGE
    return p.returncode, out[-6000:]


def gcode_stats(path):
    stats = {}
    try:
        with open(path, 'rb') as f:
            f.seek(max(0, os.path.getsize(path) - 60000))
            tail = f.read().decode('utf-8', 'replace')
    except OSError:
        return stats
    for key in ('estimated printing time (normal mode)', 'filament used [g]', 'filament used [mm]',
                'filament used [cm3]', 'total filament used [g]', 'filament_type', 'layer_height',
                'printer_settings_id', 'print_settings_id', 'filament_settings_id'):
        m = re.search(r'^; ' + re.escape(key) + r' = (.*)$', tail, re.M)
        if m:
            stats[key] = m.group(1).strip()
    return stats


# ---------------------------------------------------------------- tools

def t_arrange(a):
    inputs = [resolve(x) for x in ([a['plan']] if a.get('plan') else []) + a.get('files', [])]
    if not inputs:
        raise ValueError('give "plan" (plan .json) and/or "files" (STL paths)')
    r = plate.make_plate(inputs, resolve(a['output']) if a.get('output') else None,
                         resolve(a['printer']) if a.get('printer') else None,
                         a.get('gap'), a.get('only'), a.get('explain', False))
    text = '\n'.join(r['lines'])
    if a.get('open', True) and r['placed']:
        text += '\n' + gui_open(r['output'])
    content = [{'type': 'text', 'text': text}]
    with open(r['png'], 'rb') as f:
        content.append({'type': 'image', 'mimeType': 'image/png', 'data': base64.b64encode(f.read()).decode()})
    return content


def t_open(a):
    return [{'type': 'text', 'text': gui_open(resolve(a['path']))}]


def t_slice(a):
    src = resolve(a['path'])
    out = resolve(a['output']) if a.get('output') else os.path.splitext(src)[0] + '.gcode'
    printer = json.load(open(resolve(a.get('printer', 'tools/prusaslicer/printers/coreone.json'))))
    pr = dict(printer.get('presets', {}))
    pr.update({k: a[k] for k in ('printer_profile', 'print_profile', 'material_profile') if a.get(k)})
    binary = a.get('binary', False)  # Prusa profiles default to .bgcode; text G-code keeps stats readable
    if binary:
        out = os.path.splitext(out)[0] + '.bgcode'
    args = ['--export-gcode', '--output', out] + ([] if binary else ['--no-binary-gcode'])
    if a.get('use_presets', True):
        if pr.get('printer'):
            args += ['--printer-profile', a.get('printer_profile', pr['printer'])]
        if pr.get('print'):
            args += ['--print-profile', a.get('print_profile', pr['print'])]
        if pr.get('filament'):
            args += ['--material-profile', a.get('material_profile', pr['filament'])]
    for ini in a.get('load', []):
        args += ['--load', resolve(ini)]
    args += a.get('extra_args', []) + [src]
    code, log = run_cli(args)
    text = '$ %s\nexit %d\n%s' % (' '.join(shlex.quote(x) for x in CMD + args), code, log)
    if code == 0 and os.path.exists(out) and not binary:
        text += '\n\n' + '\n'.join('%s: %s' % kv for kv in gcode_stats(out).items())
    return [{'type': 'text', 'text': text}]


def t_cli(a):
    code, log = run_cli([str(x) for x in a['args']], a.get('timeout', 600))
    return [{'type': 'text', 'text': 'exit %d\n%s' % (code, log)}]


TOOLS = {
    'arrange': (t_arrange, 'Orient STL parts for printing, pack them on the bed, write a PrusaSlicer 3MF + PNG top view, '
                'and (open=true, default) load it into the running PrusaSlicer window. Use a plan JSON '
                '(see tools/prusaslicer/plate.py docstring: per-part "down" = side on the bed: -z/+z/±x/±y/auto, "spin", "copies") '
                'or raw STL files (auto orientation). Paths relative to the repo root.', {
                    'plan': {'type': 'string', 'description': 'plan .json, e.g. stl/plate.json'},
                    'files': {'type': 'array', 'items': {'type': 'string'}, 'description': 'extra STL files (auto-oriented)'},
                    'only': {'type': 'string', 'description': 'comma separated part names to include'},
                    'output': {'type': 'string', 'description': 'output .3mf (default from plan)'},
                    'printer': {'type': 'string', 'description': 'printer json (default tools/prusaslicer/printers/coreone.json)'},
                    'gap': {'type': 'number', 'description': 'spacing between parts, mm'},
                    'explain': {'type': 'boolean', 'description': 'list orientation candidates for auto parts'},
                    'open': {'type': 'boolean', 'description': 'send result to PrusaSlicer GUI (default true)'}}, []),
    'open': (t_open, 'Load a file (3mf/stl/gcode) into the running PrusaSlicer GUI.', {
        'path': {'type': 'string'}}, ['path']),
    'slice': (t_slice, 'Slice a 3MF/STL with the PrusaSlicer CLI into G-code (system presets from the printer json, '
              'overridable) and report print time / filament.', {
                  'path': {'type': 'string'}, 'output': {'type': 'string'},
                  'printer': {'type': 'string', 'description': 'printer json with "presets"'},
                  'printer_profile': {'type': 'string'}, 'print_profile': {'type': 'string'},
                  'material_profile': {'type': 'string'},
                  'use_presets': {'type': 'boolean', 'description': 'pass --*-profile options (default true)'},
                  'binary': {'type': 'boolean', 'description': 'export .bgcode (Prusa native) instead of text G-code; no stats then'},
                  'load': {'type': 'array', 'items': {'type': 'string'}, 'description': '.ini configs to --load'},
                  'extra_args': {'type': 'array', 'items': {'type': 'string'}}}, ['path']),
    'cli': (t_cli, 'Run the PrusaSlicer CLI with arbitrary arguments (e.g. ["--help"], ["--info", "x.stl"]).', {
        'args': {'type': 'array', 'items': {'type': 'string'}}, 'timeout': {'type': 'number'}}, ['args']),
}


def handle(msg):
    m, mid = msg.get('method'), msg.get('id')
    if m == 'initialize':
        res = {'protocolVersion': msg.get('params', {}).get('protocolVersion', '2025-06-18'),
               'capabilities': {'tools': {}}, 'serverInfo': {'name': 'prusaslicer', 'version': '0.1'},
               'instructions': 'PrusaSlicer helper for a Prusa CORE One. arrange -> builds an oriented, packed plate and '
                               'loads it into the user\'s PrusaSlicer window (they watch it); slice -> G-code + stats; '
                               'cli -> raw CLI. The user finishes placement by hand in the GUI.'}
    elif m == 'tools/list':
        res = {'tools': [{'name': n, 'description': d, 'inputSchema': {'type': 'object', 'properties': p, 'required': r}}
                         for n, (_, d, p, r) in TOOLS.items()]}
    elif m == 'tools/call':
        name = msg['params']['name']
        try:
            res = {'content': TOOLS[name][0](msg['params'].get('arguments') or {})}
        except Exception as e:  # report to the model, keep serving
            res = {'content': [{'type': 'text', 'text': '%s: %s\n%s' % (type(e).__name__, e, traceback.format_exc()[-2000:])}],
                   'isError': True}
    elif m == 'ping':
        res = {}
    elif mid is None:
        return None  # notification
    else:
        return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': 'unknown method ' + str(m)}}
    return {'jsonrpc': '2.0', 'id': mid, 'result': res}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            out = handle(json.loads(line))
        except Exception as e:
            out = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': str(e)}}
        if out is not None:
            sys.stdout.write(json.dumps(out) + '\n')
            sys.stdout.flush()


if __name__ == '__main__':
    main()
