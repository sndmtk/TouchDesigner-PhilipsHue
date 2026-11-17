"""Executed inside TouchDesigner (Textport):

    exec(open(r'<repo>/src/build_entertainment.py', encoding='utf-8').read())

Creates /project1/HueEntertainment, or updates it in place (keeping its keys,
settings, area and mapping), and exports an unpaired HueEntertainment.tox to the
repo root.

The callbacks DAT lives next to the component, docked to it. A TOX does not
include docked operators, so the exported TOX creates it itself when it is
dropped into a network.
"""
from pathlib import Path
import json
import traceback

ROOT = Path(r'C:/Users/sndmt/Documents/Github Projects/TouchDesigner-PhilipsHue')
NAME = 'HueEntertainment'
MODULES = ('dtls_psk', 'ent_core', 'ent_runtime')
TABLES = {
    'mapping': ['pixel', 'channel_id', 'name', 'light_key'],
}
# Operators from earlier versions that are no longer used. Status is now a
# parameter, the light lists are parameters, and there are no DAT outputs.
OBSOLETE = ('hue_core', 'discovery', 'bridges', 'channels', 'out_channels',
            'status', 'out_status', 'out_mapping', 'area_lights')
PULSES = 'Connect Forgetkey Discover Refreshareas Createarea Updatearea Deletearea Searchlights Deletedevice Newcallbacks'
LAYOUT = {
    'rgba': (-600, 300), 'README': (-350, 300), 'errors': (-100, 300),
    'dtls_psk': (-600, 100), 'ent_core': (-350, 100), 'ent_runtime': (-100, 100),
    'lifecycle': (-600, -100), 'controls': (-350, -100), 'mapping': (-100, -100),
}
LIFECYCLE = '''controller = None
def get_controller():
    global controller
    if controller is None:
        controller = parent().op('ent_runtime').module.Controller(parent())
    return controller
def onFrameStart(frame):
    try:
        get_controller().tick()
    except Exception as e:
        get_controller().error(str(e))
def onExit():
    if controller is not None:
        controller.close()
'''
CONTROLS = '''"""Pulse callbacks for HueEntertainment. Destructive actions ask first."""
def onPulse(par):
    ctl = parent().op('lifecycle').module.get_controller()
    if par.name == 'Deletearea':
        area = ctl.selected_area()
        if area is None:
            ctl.error('Select an area to delete')
            return
        msg = ('Delete this Entertainment area from the Hue Bridge?\\n\\n' + area['name'] +
               '\\n' + str(len(area['channels'])) + ' channel(s)\\n\\nThe lights themselves are not removed.')
        if ui.messageBox('Delete Entertainment area', msg, buttons=['Cancel', 'Delete Area']) != 1:
            ctl.status('areas', 'Cancelled')
            return
        ctl.pulse(par.name, confirmed=True)
    elif par.name == 'Deletedevice':
        device = ctl.selected_device()
        if device is None:
            ctl.error('Select a light in Device first')
            return
        areas = ctl.device_areas(device['id'])
        msg = ('Remove this light from the Hue Bridge?\\n\\n' + device['name'] +
               ('\\n\\nIt will also leave: ' + ', '.join(areas) if areas else '') +
               '\\n\\nTo use it again it has to be searched and paired again.')
        if ui.messageBox('Delete light', msg, buttons=['Cancel', 'Delete Light']) != 1:
            ctl.status('lights', 'Cancelled')
            return
        ctl.pulse(par.name, confirmed=True)
    else:
        ctl.pulse(par.name)
'''


def snapshot(c):
    """Values of the existing custom parameters (pulses excluded)."""
    values = {}
    for p in getattr(c, 'customPars', []):
        # Pulses carry no value; Status and the per-light rows are rebuilt by the runtime.
        if p.style not in ('Pulse', 'Header') and p.name != 'Status' and not p.name.startswith(('Light', 'Use', 'Pos')):
            # OP/DAT parameters keep their path text, not the operator.
            values[p.name] = p.val if p.style in ('OP', 'DAT', 'COMP', 'TOP', 'CHOP', 'SOP') else p.eval()
    return values


def reset_pars(c):
    """Remove all custom parameters so pages can be rebuilt in the new layout."""
    try:
        c.destroyCustomPars()
        return True
    except Exception:
        try:
            for pg in list(c.customPages):
                pg.destroy()
            return True
        except Exception:
            return False


def page(c, name):
    found = [p for p in c.customPages if p.name == name]
    return found[0] if found else c.appendCustomPage(name)


def ensure_par(c, pg, kind, name, label, **attrs):
    p = getattr(c.par, name, None)
    if p is None:
        p = getattr(pg, 'append' + kind)(name, label=label)[0]
        if 'default' in attrs:
            p.val = attrs['default']
    p.label = label
    for k, v in attrs.items():
        setattr(p, k, v)
    return p


def menu(c, pg, kind, name, label, names, labels, default=None, **attrs):
    p = ensure_par(c, pg, kind, name, label, **attrs)
    if names is not None and (not p.menuNames or kind == 'Menu' and names[0] not in p.menuNames):
        p.menuNames = names
        p.menuLabels = labels
        if default is not None:
            p.val = default
    return p


def build_pars(c):
    pg = page(c, 'Bridge')
    menu(c, pg, 'StrMenu', 'Bridgeip', 'Bridge IP', None, None)
    ensure_par(c, pg, 'Pulse', 'Discover', 'Discover Bridges')
    ensure_par(c, pg, 'Pulse', 'Connect', 'Connect / Retry Pairing', startSection=True)
    ensure_par(c, pg, 'Pulse', 'Forgetkey', 'Forget Key / Pair Again')
    for name, label in (('Apikey', 'API Key'), ('Clientkey', 'Client Key (PSK)'), ('Keyhost', 'Authenticated Bridge IP')):
        ensure_par(c, pg, 'Str', name, label, readOnly=True)
    ensure_par(c, pg, 'Str', 'Status', 'Status', readOnly=True, startSection=True)

    pg = page(c, 'Area')
    menu(c, pg, 'Menu', 'Area', 'Entertainment Area', ['none'], ['Select an area'], 'none')
    ensure_par(c, pg, 'Pulse', 'Refreshareas', 'Refresh Areas')
    ensure_par(c, pg, 'Str', 'Newareaname', 'New Area Name', default='TouchDesigner', startSection=True)
    ensure_par(c, pg, 'Pulse', 'Createarea', 'Create Area')
    ensure_par(c, pg, 'Pulse', 'Updatearea', 'Update Selected Area')
    ensure_par(c, pg, 'Pulse', 'Deletearea', 'Delete Selected Area')

    # Per-light parameters (Light<n> on Lights, Use<n> on Area) are created by the
    # runtime once the Bridge's lights are known.
    pg = page(c, 'Lights')
    ensure_par(c, pg, 'Pulse', 'Searchlights', 'Search New Lights')
    menu(c, pg, 'Menu', 'Device', 'Device', ['none'], ['Select a light'], 'none', startSection=True)
    ensure_par(c, pg, 'Pulse', 'Deletedevice', 'Delete Selected Device')

    pg = page(c, 'Stream')
    ensure_par(c, pg, 'Toggle', 'Stream', 'Enable Streaming')
    ensure_par(c, pg, 'Toggle', 'Lightson', 'Lights On', default=True)
    ensure_par(c, pg, 'Float', 'Fps', 'Send Rate (fps)', default=50, min=10, max=60, normMin=10, normMax=60,
               clampMin=True, clampMax=True)
    ensure_par(c, pg, 'Float', 'Smoothing', 'Smoothing (s)', default=0, min=0, max=5, normMin=0, normMax=2,
               clampMin=True, clampMax=False)
    menu(c, pg, 'Menu', 'Colorspace', 'Input Color Space', ['srgb', 'linear'], ['sRGB', 'Linear sRGB'], 'srgb')
    menu(c, pg, 'Menu', 'Brightness', 'Brightness From', ['value', 'alpha'], ['Alpha x RGB Value', 'Alpha Only'], 'value')

    # Callbacks DAT docked next to the component (created when it is placed).
    pg = page(c, 'Setup')
    ensure_par(c, pg, 'DAT', 'Callbacksdat', 'Callbacks DAT')
    ensure_par(c, pg, 'Pulse', 'Newcallbacks', 'Create Callbacks DAT')


def restore(c, values):
    for name, value in values.items():
        p = getattr(c.par, name, None)
        if p is None:
            continue
        try:
            if p.isMenu and value not in p.menuNames:
                # Area/Device ids are filled in by the runtime; keep the saved choice valid until then.
                p.menuNames = list(p.menuNames) + [value]
                p.menuLabels = list(p.menuLabels) + [str(value)]
            p.val = value
        except Exception:
            pass


def populate(c, report=None, docked=True):
    report = {} if report is None else report
    values = snapshot(c)
    if values and reset_pars(c):
        report['pars'] = 'rebuilt pages, restored %d value(s)' % len(values)
    build_pars(c)
    restore(c, values)

    for name in OBSOLETE:
        o = c.op(name)
        if o is not None:
            o.destroy()
            report.setdefault('removed', []).append(name)
    if c.op('rgba') is None:
        c.create(inTOP, 'rgba')
    for name in MODULES:
        o = c.op(name) or c.create(textDAT, name)
        o.text = (ROOT / 'src' / (name + '.py')).read_text(encoding='utf-8')
    for name, header in TABLES.items():
        t = c.op(name) or c.create(tableDAT, name)
        if t.numRows == 0 or [t[0, i].val for i in range(t.numCols)] != header:
            # New table, or an older column layout: the runtime refills it from the Bridge.
            t.clear()
            t.appendRow(header)
    if c.op('errors') is None:
        t = c.create(fifoDAT, 'errors')
        t.par.clamp = True
        t.par.maxlines = 10
        t.par.firstrow = True
        t.clear()
        t.appendRow(['time', 'message'])
    # The component's node viewer shows the error log (no DAT outputs).
    c.par.opviewer.expr = "me.op('errors')"
    c.viewer = True
    o = c.op('README') or c.create(textDAT, 'README')
    o.text = (ROOT / 'README.md').read_text(encoding='utf-8')
    life = c.op('lifecycle') or c.create(executeDAT, 'lifecycle')
    life.text = LIFECYCLE
    life.par.framestart = True
    life.par.exit = True
    ctl = c.op('controls') or c.create(parameterexecuteDAT, 'controls')
    ctl.par.op = c
    ctl.par.pars = PULSES
    ctl.par.onpulse = True
    ctl.text = CONTROLS
    for name, (x, y) in LAYOUT.items():
        o = c.op(name)
        if o is not None:
            o.nodeX, o.nodeY = x, y
    if docked:
        # A docked mapping DAT from a short-lived earlier build is no longer used.
        for o in list(getattr(c, 'docked', [])):
            if o.name.rstrip('0123456789').endswith('_mapping'):
                report.setdefault('removed', []).append(o.name)
                o.destroy()
        callbacks = c.op('ent_runtime').module.ensure_callbacks(c)
        report['callbacks_dat'] = callbacks.path if callbacks is not None else None
    return report


def build():
    target = op('/project1')
    report = {}
    # 1) Distributable TOX from a fresh, never-paired copy. It is built inside a
    #    temporary container so its root can be named HueEntertainment (a dropped
    #    TOX takes its root's name) even while the working copy exists.
    holder = target.create(baseCOMP, '__hue_ent_export')
    try:
        tmp = holder.create(baseCOMP, NAME)
        populate(tmp, docked=False)   # the TOX creates its callbacks DAT when dropped
        tmp.save(str(ROOT / (NAME + '.tox')))
        report['tox'] = str(ROOT / (NAME + '.tox'))
        report['export_errors'] = tmp.errors(recurse=True)
    finally:
        holder.destroy()
    # 2) Working component in the project: create, or update in place.
    c = target.op(NAME)
    if c is not None and c.op('ent_runtime') is not None:
        life = c.op('lifecycle')
        life.par.framestart = False
        old = life.module.controller
        if old is not None:
            try:
                old.close()  # stops any running stream and releases the area
            except Exception as e:
                # Old code may be buggy (that is often why we rebuild); never let it block the update.
                report['old_close_error'] = repr(e)
                sender = getattr(old, 'sender', None)
                halt = getattr(sender, '_halt', None) or getattr(sender, '_stop', None)
                if hasattr(halt, 'set'):
                    halt.set()
        populate(c, report)
        life.module.controller = None
        life.par.framestart = True
        report['updated'] = c.path
    else:
        c = target.create(baseCOMP, NAME)
        c.nodeX, c.nodeY = 0, -300
        populate(c, report)
        report['created'] = c.path
    report['errors'] = c.errors(recurse=True)
    c.openParameters()
    (ROOT / 'build').mkdir(exist_ok=True)
    (ROOT / 'build/entertainment_build.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    err = ROOT / 'build/entertainment_build_error.txt'
    if err.exists():
        err.unlink()
    print('HueEntertainment build:', report)
    return c


try:
    built_entertainment = build()
except Exception:
    (ROOT / 'build').mkdir(exist_ok=True)
    (ROOT / 'build/entertainment_build_error.txt').write_text(traceback.format_exc(), encoding='utf-8')
    print(traceback.format_exc())
