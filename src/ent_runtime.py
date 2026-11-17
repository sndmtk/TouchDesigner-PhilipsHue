"""HueEntertainment runtime. Network work runs on worker threads; all OP access is on TD's thread.

Flow: pair (with client key) -> read Entertainment areas -> PUT action:start on the
selected area -> DTLS handshake on UDP 2100 -> stream HueStream frames at a fixed
rate from a sender thread -> PUT action:stop when streaming is disabled.
"""
import time
import ipaddress
import threading
from concurrent.futures import ThreadPoolExecutor


FETCHES = ('entertainment_configuration', 'entertainment', 'device', 'zigbee_connectivity')
MANAGEMENT = ('create_area', 'update_area', 'delete_area', 'delete_device', 'search_resources', 'search_start')
FAILED = {
    'create_area': 'Could not create the area: see errors DAT',
    'update_area': 'Could not update the area: see errors DAT',
    'delete_area': 'Could not delete the area: see errors DAT',
    'delete_device': 'Could not delete the light: see errors DAT',
    'search_resources': 'Light search failed: see errors DAT',
    'search_start': 'Light search failed: see errors DAT',
    'search_poll': 'Lost track of the light search: see errors DAT',
}


CALLBACKS_TEMPLATE = '''"""HueEntertainment callbacks.

Each function is called by the HueEntertainment component it is docked to, on
TouchDesigner's main thread. Delete the ones you do not need. Errors raised here
are written to the component's errors DAT.

	comp     - the HueEntertainment component
	area     - {'id', 'name', 'lights': [light names, left to right]}
	light    - {'id', 'name', 'status'}   (status: Zigbee 'connected', 'connectivity_issue', ...)
	mapping  - [{'pixel', 'name', 'channel_id'}, ...] in pixel order
"""

def onBridgesDiscovered(comp, bridges):
	# bridges: [{'id', 'ip'}, ...] found by Discover Bridges
	return

def onPaired(comp, bridgeIp):
	# New API key and client key received (save the TOE to keep them)
	return

def onConnected(comp, bridgeIp):
	# Areas and lights were read for the first time after connecting
	return

def onAreasChanged(comp, areas):
	# Called after connecting and whenever the Bridge's Entertainment areas change
	return

def onLightsChanged(comp, lights):
	# Called after connecting and whenever the Bridge's lights change (added, removed, renamed)
	return

def onLightStatusChanged(comp, light, status, previous):
	# A light's Zigbee status changed, e.g. 'connected' -> 'connectivity_issue'
	return

def onSearchComplete(comp, newLights):
	# Search New Lights finished; newLights: lights that were not registered before
	return

def onAreaCreated(comp, area):
	return

def onAreaUpdated(comp, area):
	return

def onAreaDeleted(comp, areaName):
	return

def onLightDeleted(comp, lightName):
	return

def onStreamStart(comp, area):
	# The DTLS stream is up; lights now follow the input TOP
	return

def onStreamStop(comp, area, reason):
	# reason: 'disabled' (Enable Streaming off), 'area changed', 'area update', 'area deleted',
	# 'light deleted', 'reconnect', or 'lost: <error>' (the stream retries after 3 s)
	return

def onMappingChanged(comp, mapping):
	# Pixel order changed (lights added or removed, area updated, or pixels edited by hand)
	return

def onError(comp, message):
	return
'''


def _optype(name):
    """TouchDesigner operator type by name (plain strings outside TouchDesigner, for tests)."""
    try:
        import td
        return getattr(td, name)
    except Exception:
        return name


def _par_op(comp, name):
    p = getattr(comp.par, name, None)
    if p is None:
        return None
    try:
        o = p.eval()
    except Exception:
        return None
    return o if o is not None and getattr(o, 'valid', True) else None


def _owned(comp, o):
    """False for a DAT docked to another component (e.g. the original after a copy)."""
    dock = getattr(o, 'dock', None)
    return dock is None or dock == comp


def _docked(comp, suffix):
    """A DAT already docked to this component, e.g. the copy TouchDesigner makes
    when the component is copied together with its docked DATs."""
    for o in getattr(comp, 'docked', []) or []:
        if o.name.rstrip('0123456789').endswith(suffix):
            return o
    return None


def _new_docked(comp, typename, base):
    """Create a DAT next to the component, docked to it, just below it."""
    parent = comp.parent()
    name, i = base, 1
    while parent.op(name) is not None:
        name, i = '%s%d' % (base, i), i + 1
    o = parent.create(_optype(typename), name)
    o.dock = comp
    o.nodeX = comp.nodeX
    o.nodeY = comp.nodeY - int(getattr(o, 'nodeHeight', 100) or 100) - 40
    return o


def _point(comp, par_name, o):
    try:
        path = comp.relativePath(o)
    except Exception:
        path = o.path
    getattr(comp.par, par_name).val = path


def ensure_callbacks(comp):
    """Create the callbacks DAT the first time the component runs (i.e. when the
    TOX is placed; a TOX does not store docked operators). Only once per
    component: deleting the DAT or clearing Callbacks DAT turns callbacks off.
    A component copied together with its docked DAT switches to its own copy."""
    par = getattr(comp.par, 'Callbacksdat', None)
    if par is None:
        return None
    o = _par_op(comp, 'Callbacksdat')
    if o is not None and _owned(comp, o):
        return o
    found = _docked(comp, '_callbacks')
    if found is not None:
        _point(comp, 'Callbacksdat', found)
        return found
    if not comp.fetch('callbacks_created', False):
        return new_callbacks(comp)
    return o


def new_callbacks(comp):
    """Create Callbacks DAT: a docked Text DAT (Python) with the template."""
    callbacks = _new_docked(comp, 'textDAT', comp.name + '_callbacks')
    callbacks.text = CALLBACKS_TEMPLATE
    try:
        callbacks.par.language = 'python'   # Content Language
    except Exception:
        pass
    _point(comp, 'Callbacksdat', callbacks)
    comp.store('callbacks_created', True)
    return callbacks


class Sender(threading.Thread):
    """Owns the DTLS session. Re-sends the newest frame at `fps` even when
    TouchDesigner stalls, so the Bridge never hits its stream idle timeout."""

    def __init__(self, dtls, host, identity, psk, fps):
        super().__init__(daemon=True, name='HueStream')
        self.dtls = dtls
        self.host = host
        self.identity = identity
        self.psk = psk
        self.fps = fps
        self.payload = None
        self.state = 'connecting'
        self.error = ''
        self.frames = 0
        self._halt = threading.Event()  # not "_stop": Thread has an internal _stop() method

    def run(self):
        client = self.dtls.DtlsPskClient(self.host, self.identity, self.psk)
        try:
            client.connect()
            self.state = 'streaming'
            due = time.monotonic()
            while not self._halt.is_set():
                frame = self.payload
                if frame is not None:
                    client.send(frame)
                    self.frames += 1
                alert = client.poll_alert()
                if alert:
                    raise self.dtls.DtlsError(alert)
                due += 1.0 / max(1.0, min(60.0, float(self.fps)))
                delay = due - time.monotonic()
                if delay < -0.25:  # fell far behind (e.g. machine paused): resync
                    due = time.monotonic()
                    delay = 0
                if delay > 0:
                    self._halt.wait(delay)
        except Exception as e:
            self.error = str(e) or e.__class__.__name__
        finally:
            client.close()
            self.state = 'stopped'

    def stop(self):
        self._halt.set()


class Controller:
    def __init__(self, c):
        self.c = c
        self.ent = c.op('ent_core').module
        self.dtls = c.op('dtls_psk').module
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='HueHTTP')
        self.client = self.ent.Client()
        self.discovery_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='HueDiscovery')
        self.discovery_future = None
        self.discovery_ip = ''
        self.pending = None
        self.config = None
        self.generation = 0
        self.ready = False
        self.next_auth = 0
        self.deadline = 0
        self.auth_stopped = False
        self.last_error = ''
        self.raw = {}
        self.fetch_queue = []
        self.areas = []
        self.areas_dirty = True
        self.last_area_poll = 0
        self.sender = None
        self.stream_area = None
        self.stream_announced = False
        self.starting = False
        self.retry_at = 0
        self.stop_queue = []
        self.management = None          # queued create/delete area request
        self.management_inflight = False
        self.reset_use = True           # load the Use toggles from the selected area on next refresh
        self.use_area = None
        self.light_ids = []             # Light<n> / Use<n> parameter index -> entertainment service id
        self.light_signature = None
        self.remap_area = None          # rebuild this area's mapping left to right after create/update
        self.mapping_seen = None        # mapping rows as last normalised (to spot user edits)
        self.next_mapping_check = 0
        self.select_after_refresh = None
        self.search_id = None           # zigbee_device_discovery resource while searching
        self.search_next = 0
        self.search_deadline = 0
        self.reported_offline = set()
        self.next_dock_check = 0
        self.in_error_callback = False
        self.area_signature = None
        self.light_states = {}          # service id -> last Zigbee status (for onLightStatusChanged)
        self.lights_before_search = None
        self.search_finished = False
        self.lights_signature = None
        self.area_event = None          # ('onAreaCreated' | 'onAreaUpdated', area id) fired after the refresh
        self.stream_info = None
        self.power_seen = None          # Lights On value last acted on (None: not yet connected)
        self.power_queue = []           # light service ids waiting for an on/off request
        self.power_on = True
        self.power_next = 0
        self.smooth = {}        # channel_id -> (r, g, b, bri) last sent, linear light
        self.smooth_area = None
        self.smooth_time = 0
        self.status('state', 'Enter Bridge IP')

    # ------------------------------------------------------------ helpers
    def status(self, k, v):
        """Show the current message in the read-only Status parameter (Bridge page).
        `k` only documents where a message comes from; one line is shown at a time."""
        p = getattr(self.c.par, 'Status', None)
        if p is not None:
            p.val = str(v)

    def error(self, message):
        message = str(message)
        for secret in (self.c.par.Apikey.eval(), self.c.par.Clientkey.eval()):
            if secret:
                message = message.replace(secret, '[redacted]')
        if message != self.last_error:
            self.c.op('errors').appendRow([time.strftime('%Y-%m-%d %H:%M:%S'), message])
            self.last_error = message
            if not self.in_error_callback:
                self.in_error_callback = True
                try:
                    self.fire('onError', message)
                finally:
                    self.in_error_callback = False

    # ------------------------------------------------------------ callbacks
    def mapping_dat(self):
        return self.c.op('mapping')

    def check_callbacks(self, now):
        if now >= self.next_dock_check:
            self.next_dock_check = now + 1.0
            try:
                ensure_callbacks(self.c)
            except Exception as e:
                self.error('Could not create the callbacks DAT: ' + str(e))

    def fire(self, name, *args):
        """Call `name(comp, *args)` in the Callbacks DAT, if it defines it."""
        dat = _par_op(self.c, 'Callbacksdat')
        if dat is None:
            return
        try:
            fn = getattr(dat.module, name, None)
            if fn is not None:
                fn(self.c, *args)
        except Exception as e:
            if name != 'onError':
                self.error('Callback %s failed: %s' % (name, e))

    @staticmethod
    def area_info(area):
        return {'id': area['id'], 'name': area['name'],
                'lights': [ch['name'] for ch in sorted(area['channels'], key=lambda c: (c['x'], c['y'], c['channel_id']))]}

    def light_info(self, cand):
        return {'id': cand['id'], 'name': cand['name'], 'status': self.light_status(cand)}

    def bridge_ip(self):
        return self.c.par.Bridgeip.eval().strip()

    def credentials(self, host):
        p = self.c.par
        if p.Keyhost.eval() != host:
            return '', ''
        return p.Apikey.eval(), p.Clientkey.eval()

    def selected_area(self):
        area = self.c.par.Area.eval()
        return next((a for a in self.areas if a['id'] == area), None)

    def submit(self, kind, method, path, body=None, extra=None):
        host, insecure = self.config
        key = self.credentials(host)[0]
        f = self.pool.submit(self.client.request, host, key, method, path, body, insecure)
        self.pending = (f, kind, (self.config, self.generation), extra)

    # ------------------------------------------------------------ pulses
    def pulse(self, name, confirmed=False):
        if name in ('Createarea', 'Updatearea', 'Deletearea'):
            self.area_command(name, confirmed)
            return
        if name in ('Searchlights', 'Deletedevice'):
            self.light_command(name, confirmed)
            return
        if name == 'Newcallbacks':
            dat = new_callbacks(self.c)
            self.status('state', 'Created ' + dat.name)
            return
        if name == 'Discover':
            if self.discovery_future:
                return
            self.discovery_ip = self.bridge_ip()
            self.status('discovery', 'Searching...')
            self.discovery_future = self.discovery_pool.submit(self.ent.discover)
            return
        if name == 'Refreshareas':
            self.areas_dirty = True
            self.reset_use = True
            self.status('areas', 'Refreshing Entertainment areas...')
            return
        if name in ('Connect', 'Forgetkey'):
            self.stop_stream('reconnect')
            self.generation += 1
            self.ready = False
            self.auth_stopped = False
            self.deadline = 0
            self.next_auth = 0
            self.areas_dirty = True
            self.retry_at = 0
            self.management = None
            self.management_inflight = False
            self.search_id = None
        if name == 'Connect':
            self.status('state', 'Connecting to ' + (self.bridge_ip() or '(no Bridge IP)') + '...')
        elif name == 'Forgetkey':
            for p in ('Apikey', 'Clientkey', 'Keyhost'):
                getattr(self.c.par, p).val = ''
            self.status('credentials', 'Keys cleared; save your TOE to persist')

    # ------------------------------------------------------------ area management
    def area_command(self, name, confirmed):
        if not self.ready:
            self.error('Connect and pair with the Bridge first')
            return
        if self.management is not None:
            self.error('An area operation is already running')
            return
        if name in ('Createarea', 'Updatearea'):
            ids = self.chosen_lights()
            if not ids:
                self.error('No light is selected: turn lights on under Lights in Area on the Area page')
                return
            if len(ids) > 10:
                self.error('An Entertainment area takes at most 10 lights; turn some off under Lights in Area')
                return
            if name == 'Createarea':
                label = self.c.par.Newareaname.eval().strip() or 'TouchDesigner'
                body = self.ent.area_body(label, ids)
                self.management = ('create_area', 'POST', '/clip/v2/resource/entertainment_configuration', body, label)
                self.status('areas', 'Creating area "%s" with %d light(s)...' % (label, len(ids)))
                return
            area = self.selected_area()
            if area is None:
                self.error('Select the area to update')
                return
            # The Bridge does not change an area while it streams: stop first. With
            # Enable Streaming still on, streaming restarts by itself after the update.
            if self.sender is not None or self.stream_area == area['id']:
                self.stop_stream('area update')
            self.management = ('update_area', 'PUT', '/clip/v2/resource/entertainment_configuration/' + area['id'],
                               self.ent.update_body(ids), area['id'])
            self.status('areas', 'Updating area "%s" to %d light(s)...' % (area['name'], len(ids)))
        elif name == 'Deletearea' and confirmed:
            area = self.selected_area()
            if area is None:
                self.error('Select an area to delete')
                return
            if self.stream_area == area['id'] or self.sender is not None:
                self.stop_stream('area deleted')
            self.management = ('delete_area', 'DELETE', '/clip/v2/resource/entertainment_configuration/' + area['id'],
                               None, area['name'])
            self.status('areas', 'Deleting area "%s"...' % area['name'])

    # ------------------------------------------------------------ light management
    def light_command(self, name, confirmed):
        if not self.ready:
            self.error('Connect and pair with the Bridge first')
            return
        if self.management is not None or self.search_id:
            self.error('A light or area operation is already running')
            return
        if name == 'Searchlights':
            self.lights_before_search = {c['id'] for c in self.candidates()}
            self.search_finished = False
            self.management = ('search_resources', 'GET', '/clip/v2/resource/zigbee_device_discovery', None, None)
            self.status('lights', 'Searching for new lights: power them on near the Bridge...')
        elif confirmed:
            device = self.selected_device()
            if device is None:
                self.error('Select a light in Device first')
                return
            # The device leaves its areas; stop streaming so the area can change.
            # With Enable Streaming on, streaming restarts once the lists are refreshed.
            if self.sender is not None:
                self.stop_stream('light deleted')
            self.management = ('delete_device', 'DELETE', '/clip/v2/resource/device/' + device['id'], None, device['name'])
            self.status('lights', 'Deleting "%s" from the Bridge...' % device['name'])

    def selected_device(self):
        dev = self.c.par.Device.eval()
        return next((d for d in self.ent.light_devices(self.raw.get('device', [])) if d['id'] == dev), None)

    def device_areas(self, device_id):
        """Names of the areas a device belongs to (shown in the delete dialog)."""
        owner = self.ent.candidate_devices(self.raw.get('entertainment', []))
        return [a['name'] for a in self.areas
                if any(owner.get(sid) == device_id for ch in a['channels'] for sid in ch['services'])]

    def render_devices(self):
        menu = self.c.par.Device
        previous = menu.eval()
        devices = self.ent.light_devices(self.raw.get('device', []))
        menu.menuNames = ['none'] + [d['id'] for d in devices]
        menu.menuLabels = ['Select a light'] + [d['name'] for d in devices]
        menu.val = previous if previous in [d['id'] for d in devices] else 'none'

    # The light lists live in custom parameters, built at runtime:
    #   Lights page: one read-only row per registered light (Zigbee status, area pixel)
    #   Area page:   one toggle per light, on = in the area; order (= pixel order) is by name
    # Parameter names are index based (Light1.., Use1..); self.light_ids maps index -> service id.

    def candidates(self):
        """Lights that can join an area, sorted by name (this is also the area order)."""
        return self.ent.candidate_lights(self.raw.get('entertainment', []), self.raw.get('device', []))

    def light_status(self, cand):
        return self.ent.connectivity(self.raw.get('zigbee_connectivity', [])).get(cand['device'], '')

    def page(self, name):
        return next((p for p in self.c.customPages if p.name == name), None)

    def area_defaults(self, area):
        """Use toggles loaded from an area: on for its lights, off for the rest.
        Without an area every light starts on, so Create Area works right away."""
        if area is None:
            return {c['id']: True for c in self.candidates()}
        members = {sid for ch in area['channels'] for sid in ch['services']}
        return {c['id']: c['id'] in members for c in self.candidates()}

    def read_use(self):
        """Current Use toggles by service id (what the user set on the Area page)."""
        out = {}
        for i, sid in enumerate(self.light_ids):
            p = getattr(self.c.par, 'Use%d' % (i + 1), None)
            if p is not None:
                out[sid] = bool(p.eval())
        return out

    def chosen_lights(self):
        """Service ids switched on, in name order (left to right in the area)."""
        use = self.read_use()
        return [c['id'] for c in self.candidates() if use.get(c['id'])]

    def sync_light_pars(self, area, reset):
        """Keep the Lights list and the Use toggles in step with the Bridge.

        reset=True loads the toggles from the selected area (first load, Refresh
        Areas, another area selected, after Create/Update). Otherwise the user's
        toggles are kept, lights removed from the Bridge disappear, and lights that
        are new on the Bridge start switched on."""
        cands = self.candidates()
        use = dict(self.c.fetch('area_use', {}) or {})
        use.update(self.read_use())
        if reset:
            use = self.area_defaults(area)
        ids = [c['id'] for c in cands]
        signature = [(c['id'], c['name']) for c in cands]
        if signature != self.light_signature:
            self.rebuild_light_pars(cands)
            self.light_signature = signature
        for i, sid in enumerate(ids):
            want = use.get(sid, True)     # a light the Bridge just gained starts on
            use[sid] = want
            p = getattr(self.c.par, 'Use%d' % (i + 1), None)
            if p is not None and bool(p.eval()) != want:
                p.val = want
        self.c.store('area_use', {sid: use[sid] for sid in ids})
        self.render_light_list(area)

    def rebuild_light_pars(self, cands):
        """Recreate the per-light parameters. TouchDesigner cannot insert a parameter
        mid-page, so the Lights page is rebuilt in order: Search, list, Device, Delete."""
        self.light_ids = [c['id'] for c in cands]
        lights = self.page('Lights')
        if lights is not None:
            device = self.c.par.Device.eval() if getattr(self.c.par, 'Device', None) is not None else 'none'
            for p in list(lights.pars):
                p.destroy()
            lights.appendPulse('Searchlights', label='Search New Lights')
            p = lights.appendStr('Lightinfo', label='Registered Lights')[0]
            p.val = '%d light(s): Zigbee status | area pixel' % len(cands)
            p.readOnly = True
            p.startSection = True
            for i, c in enumerate(cands):
                p = lights.appendStr('Light%d' % (i + 1), label=c['name'])[0]
                p.readOnly = True
            p = lights.appendMenu('Device', label='Device')[0]
            p.startSection = True
            p.menuNames = ['none', device] if device != 'none' else ['none']
            p.menuLabels = ['Select a light', device] if device != 'none' else ['Select a light']
            p.val = device
            lights.appendPulse('Deletedevice', label='Delete Selected Device')
        area_page = self.page('Area')
        if area_page is not None:
            for p in list(area_page.pars):
                if p.name.startswith(('Use', 'Pos')):   # Pos*: per-light numbers of an older version
                    p.destroy()
            p = area_page.appendStr('Useinfo', label='Lights in Area')[0]
            p.val = 'On = in the area. Order (pixel 0, 1, ...) is by name'
            p.readOnly = True
            p.startSection = True
            for i, c in enumerate(cands):
                p = area_page.appendToggle('Use%d' % (i + 1), label=c['name'])[0]
                p.help = 'On = include this light in the area. Lights are placed left to right by name.'

    def render_light_list(self, area):
        """Read-only rows on the Lights page: Zigbee status and area membership."""
        pixel_of = {}
        if area is not None:
            m = self.mapping_dat()
            for r in range(1, m.numRows):
                for part in m[r, 3].val.split('|'):
                    pixel_of.setdefault(part.split('#')[0], m[r, 0].val)
        for i, c in enumerate(self.candidates()):
            p = getattr(self.c.par, 'Light%d' % (i + 1), None)
            if p is None:
                continue
            text = self.light_status(c) or 'status unknown'
            if c['id'] in pixel_of:
                text += ' | in area, pixel ' + str(pixel_of[c['id']])
            if p.eval() != text:
                p.val = text

    def report_connectivity(self, area):
        """One error line when a light of the selected area loses its Zigbee link."""
        if area is None:
            return
        cands = {c['id']: c for c in self.candidates()}
        offline = set()
        for ch in area['channels']:
            for sid in ch['services']:
                c = cands.get(sid)
                if c and self.light_status(c) not in ('', 'connected'):
                    offline.add(c['name'])
        for name in sorted(offline - self.reported_offline):
            self.error('Light "%s" is not reachable (Zigbee %s); it will not follow the stream'
                       % (name, self.light_status(next(c for c in cands.values() if c['name'] == name))))
        self.reported_offline = offline

    # ------------------------------------------------------------ streaming
    def stop_stream(self, reason='disabled'):
        info = self.stream_info if self.sender is not None and self.stream_announced else None
        if self.sender is not None:
            self.sender.stop()
            self.sender = None
        if self.stream_area:
            self.stop_queue.append(self.stream_area)
            self.stream_area = None
        self.starting = False
        self.stream_announced = False
        self.stream_info = None
        if info is not None:
            # Released lights go back to their state before streaming; keep them off if asked.
            if not self.par_value('Lightson', True):
                self.queue_power(False)
            self.fire('onStreamStop', info, reason)

    # ------------------------------------------------------------ Lights On
    def power_targets(self):
        """Light service ids for Lights On: the selected area's lights, or every
        light on the Bridge when no area is selected."""
        owners = {e['id']: e.get('owner', {}).get('rid') for e in self.raw.get('entertainment', [])}
        area = self.selected_area()
        if area is not None:
            devices = {owners.get(sid) for ch in area['channels'] for sid in ch['services']}
        else:
            devices = {c['device'] for c in self.candidates()}
        out = []
        for d in self.raw.get('device', []):
            if d['id'] in devices:
                out += [s['rid'] for s in d.get('services', []) if s.get('rtype') == 'light']
        return sorted(set(out))

    def queue_power(self, on):
        self.power_on = bool(on)
        self.power_queue = self.power_targets()
        if self.power_queue:
            self.status('lights', 'Turning %d light(s) %s' % (len(self.power_queue), 'on' if on else 'off'))

    def check_power(self):
        """Lights On while not streaming: switch the lights over HTTPS when the
        toggle changes (not on connect, so lights are left as they are)."""
        value = bool(self.par_value('Lightson', True))
        if self.power_seen is None:
            self.power_seen = value
            return
        if value == self.power_seen:
            return
        self.power_seen = value
        if self.sender is None and not self.starting and not self.stream_area:
            self.queue_power(value)
        # While streaming, frame() sends black instead.

    def frame(self, area, now=None):
        """Build one HueStream packet from the input TOP (TD thread).

        With Smoothing > 0 every channel eases toward its new value in linear
        light (about 95% after Smoothing seconds), like a Transition."""
        now = time.monotonic() if now is None else now
        mapping = self.mapping_dat()
        linear = self.c.par.Colorspace.eval() == 'linear'
        lights_on = bool(self.par_value('Lightson', True))
        mode = self.c.par.Brightness.eval()
        smoothing = float(self.par_value('Smoothing', 0.0))
        if self.smooth_area != area['id']:
            self.smooth = {}
            self.smooth_area = area['id']
        dt = now - self.smooth_time if self.smooth_time else 0.0
        self.smooth_time = now
        valid = {ch['channel_id'] for ch in area['channels']}
        top = self.c.op('rgba')
        pixels = None
        if self.c.inputs and top.height == 1:
            pixels = top.numpyArray(delayed=True)
        elif self.c.inputs:
            self.error('Input TOP must be N x 1 (height 1); sending black')
        channels = []
        for r in range(1, mapping.numRows):
            try:
                pixel = int(mapping[r, 0].val)
                channel = int(mapping[r, 1].val)
            except ValueError:
                continue
            if channel not in valid:
                self.error('Mapping contains a channel that is not in this area: ' + str(channel))
                continue
            if pixels is None or not lights_on:
                target = (0.0, 0.0, 0.0, 0.0)  # no input, or Lights On off: fade to black
            elif pixel < 0 or pixel >= pixels.shape[1]:
                self.error('Mapping pixel index outside TOP width: ' + str(pixel))
                continue
            else:
                target = self.ent.rgba_to_linear(pixels[0, pixel], linear, mode)
            value = self.ent.smooth_step(self.smooth.get(channel), target, dt, smoothing)
            self.smooth[channel] = value
            channels.append((channel,) + tuple(self.ent.linear_to_xyb(*value)))
        return self.ent.packet(area['id'], channels)

    def par_value(self, name, default):
        # Components built before a parameter existed keep working with its default.
        p = getattr(self.c.par, name, None)
        return default if p is None else p.eval()

    def watch_sender(self, now):
        s = self.sender
        if s is None:
            return
        s.fps = self.c.par.Fps.eval()
        if s.state == 'streaming' and not self.stream_announced:
            self.stream_announced = True
            area = self.selected_area()
            self.status('stream', 'Streaming %d channel(s) at %g fps' % (len(area['channels']) if area else 0, s.fps))
            self.stream_info = self.area_info(area) if area else {'id': self.stream_area, 'name': '', 'lights': []}
            self.fire('onStreamStart', self.stream_info)
        elif s.state == 'stopped':
            self.sender = None
            info = self.stream_info if self.stream_announced else None
            self.stream_announced = False
            self.stream_info = None
            self.error('Stream: ' + s.error)
            if info is not None:
                self.fire('onStreamStop', info, 'lost: ' + s.error)
            hint = ''
            if 'Finished' in s.error or 'timed out' in s.error:
                hint = ' (check that UDP 2100 is not blocked, or Forget Key and pair again)'
            self.status('stream', 'Stream lost: ' + s.error + hint + '; retrying in 3 s')
            self.retry_at = now + 3

    # ------------------------------------------------------------ areas
    def apply_areas(self):
        self.areas = self.ent.parse_areas(self.raw.get('entertainment_configuration', []),
                                          self.raw.get('entertainment', []), self.raw.get('device', []))
        menu = self.c.par.Area
        previous = menu.eval()
        ids = [a['id'] for a in self.areas]
        menu.menuNames = ['none'] + ids
        menu.menuLabels = ['Select an area'] + ['%s (%d ch)' % (a['name'], len(a['channels'])) for a in self.areas]
        if self.select_after_refresh in ids:
            menu.val = self.select_after_refresh
            self.select_after_refresh = None
        elif previous in ids:
            menu.val = previous
        elif len(ids) == 1:
            menu.val = ids[0]
        else:
            menu.val = 'none'
        area = self.selected_area()
        current = area['id'] if area is not None else None
        reset = self.reset_use or current != self.use_area or self.remap_area is not None
        if area is not None:
            self.sync_mapping(area)   # before the light list, which shows each light's pixel
        self.sync_light_pars(area, reset)
        self.reset_use = False
        self.use_area = current
        self.render_devices()
        self.report_connectivity(area)


    def fire_change_events(self):
        """Area, light, search and area-operation callbacks, after a full refresh."""
        areas = [self.area_info(a) for a in self.areas]
        sig = [(a['id'], a['name'], tuple(a['lights'])) for a in areas]
        if sig != self.area_signature:
            self.area_signature = sig
            self.fire('onAreasChanged', areas)
        cands = self.candidates()
        lights = [self.light_info(c) for c in cands]
        sig = [(l['id'], l['name']) for l in lights]
        if sig != self.lights_signature:
            self.lights_signature = sig
            self.fire('onLightsChanged', lights)
        for l in lights:
            previous = self.light_states.get(l['id'])
            if previous is not None and previous != l['status']:
                self.fire('onLightStatusChanged', l, l['status'], previous)
        self.light_states = {l['id']: l['status'] for l in lights}
        if self.search_finished:
            self.search_finished = False
            before = self.lights_before_search or set()
            self.fire('onSearchComplete', [l for l in lights if l['id'] not in before])
        if self.area_event is not None:
            event, area_id = self.area_event
            self.area_event = None
            area = next((a for a in areas if a['id'] == area_id), None)
            if area is not None:
                self.fire(event, area)

    def mapping_rows(self):
        m = self.mapping_dat()
        key_col = 3 if m.numRows and m.numCols > 3 else None
        return [(m[r, 0].val, m[r, key_col].val if key_col is not None else '') for r in range(1, m.numRows)]

    def sync_mapping(self, area, rebuild=False):
        """Keep `mapping` covering every light of the area once, pixels 0..N-1.

        Lights are tracked by light_key, so the Bridge renumbering channel ids
        does not move a pixel to another light. Rebuilt left to right when the area
        changes or after Create/Update Area."""
        if self.c.fetch('mapping_area', None) != area['id'] or self.remap_area == area['id']:
            rebuild = True
            self.remap_area = None
        rows = self.mapping_rows()
        table = self.ent.normalize_mapping(rows, area['channels'], rebuild=rebuild)
        m = self.mapping_dat()
        current = [[m[r, c].val for c in range(m.numCols)] for r in range(m.numRows)]
        wanted = [['pixel', 'channel_id', 'name', 'light_key']] + [[str(v) for v in row] for row in table]
        if current != wanted:
            m.clear()
            m.appendRow(wanted[0])
            for row in table:
                m.appendRow(row)
        self.c.store('mapping_area', area['id'])
        before = self.mapping_seen
        self.mapping_seen = self.mapping_rows()
        if before is not None and self.mapping_seen != before:
            self.fire('onMappingChanged', [{'pixel': int(r[0]), 'name': r[2], 'channel_id': int(r[1])} for r in table])

    def check_mapping_edits(self, now):
        """Renumber right after the user edits pixel values (at most 4x/s)."""
        if now < self.next_mapping_check:
            return
        self.next_mapping_check = now + 0.25
        area = self.selected_area()
        if area is not None and self.mapping_rows() != self.mapping_seen:
            self.sync_mapping(area)
            self.render_light_list(area)

    # ------------------------------------------------------------ responses
    def receive(self, now):
        f, kind, token, extra = self.pending
        if not f.done():
            return False
        self.pending = None
        if token != (self.config, self.generation):
            return True
        try:
            code, result = f.result()
            if kind == 'auth':
                if isinstance(result, list) and result and 'success' in result[0]:
                    ok = result[0]['success']
                    if not ok.get('clientkey'):
                        raise ValueError('Bridge returned no client key; Entertainment needs Bridge API 1.22 or newer')
                    self.c.par.Apikey.val = ok['username']
                    self.c.par.Clientkey.val = ok['clientkey']
                    self.c.par.Keyhost.val = self.config[0]
                    self.status('credentials', 'Paired with client key; save your TOE to persist')
                    self.deadline = 0
                    self.fire('onPaired', self.config[0])
                elif isinstance(result, list) and result and result[0].get('error', {}).get('type') == 101:
                    self.status('state', 'Press the physical Bridge link button')
                else:
                    raise ValueError('Pairing rejected: ' + str(result))
                self.next_auth = now + 2
                return True
            if code in (401, 403):
                self.ready = False
                self.auth_stopped = True
                raise ValueError('API key rejected. Use Forget Key to pair again.')
            if kind == 'power':
                if not isinstance(result, dict) or code >= 400 or result.get('errors'):
                    raise ValueError('Lights On: ' + str(result.get('errors') if isinstance(result, dict) else result))
                if not self.power_queue:
                    self.status('lights', 'All lights %s' % ('on' if self.power_on else 'off'))
                return True
            if kind == 'stop':
                return True  # best effort; the Bridge also stops on its own after a timeout
            if kind in MANAGEMENT:
                self.management = None
                self.management_inflight = False
            if not isinstance(result, dict) or code >= 400 or result.get('errors'):
                raise ValueError(str(result.get('errors') if isinstance(result, dict) else result))
            if kind in FETCHES:
                self.raw[kind] = result.get('data', [])
                if not self.fetch_queue:
                    self.apply_areas()
                    if not self.ready:  # announce once; periodic refreshes keep the current message
                        self.status('state', 'Ready: %d Entertainment area(s)' % len(self.areas) if self.areas else
                                    'No Entertainment areas yet: check Lights in Area, then press Create Area')
                    first = not self.ready
                    self.ready = True
                    self.areas_dirty = False
                    self.last_area_poll = now
                    if first:
                        self.fire('onConnected', self.config[0])
                    self.fire_change_events()
            elif kind == 'create_area':
                created = [d.get('rid') for d in result.get('data', []) if d.get('rid')]
                self.select_after_refresh = created[0] if created else None
                self.remap_area = self.select_after_refresh
                self.area_event = ('onAreaCreated', self.select_after_refresh)
                self.areas_dirty = True
                self.status('areas', 'Created area "%s"' % extra)
            elif kind == 'update_area':
                self.remap_area = extra   # pixels follow the new left-to-right order
                self.area_event = ('onAreaUpdated', extra)
                self.areas_dirty = True
                self.retry_at = 0         # Enable Streaming still on: restart right away
                self.status('areas', 'Updated area; mapping renumbered')
            elif kind == 'delete_area':
                self.areas_dirty = True
                self.status('areas', 'Deleted area "%s"' % extra)
                self.fire('onAreaDeleted', extra)
            elif kind == 'delete_device':
                self.areas_dirty = True
                self.retry_at = 0
                self.status('lights', 'Deleted "%s" from the Bridge' % extra)
                self.fire('onLightDeleted', extra)
            elif kind == 'search_resources':
                resources = result.get('data', [])
                if not resources:
                    raise ValueError('Bridge exposes no Zigbee discovery resource')
                self.search_id = resources[0]['id']
                self.search_deadline = now + 120
                self.search_next = now + 2
                if resources[0].get('status') == 'active':
                    self.status('lights', 'Bridge is already searching; monitoring')
                else:
                    self.management = ('search_start', 'PUT', '/clip/v2/resource/zigbee_device_discovery/' + self.search_id,
                                       {'action': {'action_type': 'search'}}, None)
            elif kind == 'search_start':
                self.search_next = now + 2
                self.status('lights', 'Searching for new lights (up to about 1 minute)...')
            elif kind == 'search_poll':
                resources = result.get('data', [])
                state = resources[0].get('status') if resources else 'unavailable'
                self.search_next = now + 2
                self.areas_dirty = True   # new lights appear on the Lights page as soon as they join
                if state != 'active':
                    self.search_id = None
                    self.search_finished = True   # onSearchComplete after the refresh lists the new lights
                    self.status('lights', 'Search complete; new lights are listed on the Lights page')
            elif kind == 'start':
                self.starting = False
                area = self.selected_area()
                if not self.c.par.Stream.eval() or area is None or area['id'] != extra:
                    # Streaming was switched off (or the area changed) while starting.
                    self.stop_queue.append(extra)
                    return True
                host = self.config[0]
                key, clientkey = self.credentials(host)
                self.sender = Sender(self.dtls, host, key, bytes.fromhex(clientkey), self.c.par.Fps.eval())
                self.stream_area = extra
                self.stream_announced = False
                self.sender.payload = self.frame(area)
                self.sender.start()
                self.status('stream', 'Area started; DTLS handshake...')
            self.last_error = ''
        except Exception as e:
            self.error(str(e))
            if kind == 'start':
                self.starting = False
                self.retry_at = now + 5
                self.status('stream', 'Could not start the area (in use by another app?): see errors DAT; retrying in 5 s')
            elif kind in MANAGEMENT or kind == 'search_poll':
                self.management = None
                self.management_inflight = False
                if kind.startswith('search'):
                    self.search_id = None
                self.status('areas' if kind.endswith('_area') else 'lights', FAILED[kind])
            elif kind not in ('stop', 'power'):
                self.fetch_queue = []
                self.next_auth = now + 5
        return True

    def receive_discovery(self):
        if not self.discovery_future or not self.discovery_future.done():
            return
        future = self.discovery_future
        self.discovery_future = None
        try:
            found, warning = future.result()
            menu = self.c.par.Bridgeip
            previous = menu.eval()
            names = [b['ip'] for b in found]
            labels = [b['ip'] + ' - ' + b['id'] for b in found]
            if previous and previous not in names:
                names.append(previous)
                labels.append(previous + ' (previous selection)')
            menu.menuNames = names
            menu.menuLabels = labels
            menu.val = previous
            self.status('discovery', str(len(found)) + ' Bridge(s) found' if found else 'No Bridge found; retry or enter IP manually')
            self.fire('onBridgesDiscovered', [{'id': b['id'], 'ip': b['ip']} for b in found])
            if warning:
                self.error(warning)
            if len(found) == 1 and not self.discovery_ip and not self.bridge_ip():
                self.c.par.Bridgeip.val = found[0]['ip']
        except Exception as e:
            self.status('discovery', 'Discovery failed')
            self.error(str(e))

    # ------------------------------------------------------------ main loop
    def tick(self):
        now = time.monotonic()
        self.check_callbacks(now)
        self.receive_discovery()
        host = self.bridge_ip()
        config = (host, True)
        if config != self.config:
            self.stop_stream('reconnect')
            self.stop_queue = []  # the old Bridge's area cannot be stopped through the new one
            self.generation += 1
            self.config = config
            self.ready = False
            self.auth_stopped = False
            self.deadline = 0
            self.next_auth = 0
            self.areas_dirty = True
            self.management = None
            self.management_inflight = False
        if self.pending:
            self.receive(now)
        self.watch_sender(now)
        if self.ready:
            self.check_mapping_edits(now)
            self.check_power()

        area = self.selected_area()
        want = bool(self.c.par.Stream.eval()) and self.ready and area is not None
        if self.sender is not None:
            if not want or area['id'] != self.stream_area:
                if not self.c.par.Stream.eval():
                    reason = 'disabled'
                elif area is None or area['id'] != self.stream_area:
                    reason = 'area changed'
                else:
                    reason = 'reconnect'
                self.stop_stream(reason)
            else:
                try:
                    self.sender.payload = self.frame(area)
                except Exception as e:
                    self.error(str(e))
        elif not want and self.stream_area:
            self.stop_stream()

        if not host or self.pending:
            return
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            self.error('Bridge IP must be an IPv4 address')
            return
        if now < self.next_auth or self.auth_stopped:
            return
        key, clientkey = self.credentials(host)
        if not key or not clientkey:
            if not self.deadline:
                self.deadline = now + 60
            if now > self.deadline:
                self.status('state', 'Pairing timed out; press Connect to retry')
                self.auth_stopped = True
                return
            self.submit('auth', 'POST', '/api', {'devicetype': 'touchdesigner#hue_ent', 'generateclientkey': True})
            return
        if self.stop_queue:
            area_id = self.stop_queue.pop(0)
            self.submit('stop', 'PUT', '/clip/v2/resource/entertainment_configuration/' + area_id, {'action': 'stop'})
            return
        if self.power_queue and now >= self.power_next:
            rid = self.power_queue.pop(0)
            self.power_next = now + 0.1   # about 10 light commands per second, as Hue recommends
            self.submit('power', 'PUT', '/clip/v2/resource/light/' + rid, {'on': {'on': self.power_on}})
            return
        if self.management is not None and not self.management_inflight:
            kind, method, path, body, label = self.management
            self.management_inflight = True
            self.submit(kind, method, path, body, extra=label)
            return
        if self.fetch_queue:
            kind = self.fetch_queue.pop(0)
            self.submit(kind, 'GET', '/clip/v2/resource/' + kind)
            return
        if self.search_id and self.management is None:
            if now >= self.search_deadline:
                self.search_id = None
                self.status('lights', 'Stopped monitoring the search (the Bridge may still be searching)')
            elif now >= self.search_next:
                self.search_next = now + 2
                self.submit('search_poll', 'GET', '/clip/v2/resource/zigbee_device_discovery/' + self.search_id)
                return
        # Lists and Zigbee status every 10 s, also while streaming (one small GET each).
        if not self.ready or self.areas_dirty or now - self.last_area_poll >= 10:
            self.fetch_queue = ['entertainment', 'device', 'zigbee_connectivity']
            self.last_area_poll = now
            self.submit('entertainment_configuration', 'GET', '/clip/v2/resource/entertainment_configuration')
            return
        if want and self.sender is None and not self.starting and now >= self.retry_at:
            self.starting = True
            self.submit('start', 'PUT', '/clip/v2/resource/entertainment_configuration/' + area['id'],
                        {'action': 'start'}, extra=area['id'])

    def close(self):
        """Stop streaming and release the area so lights return to normal control."""
        area = self.stream_area
        if self.sender is not None:
            self.sender.stop()
            self.sender.join(timeout=0.5)
            self.sender = None
        if area and self.config and self.config[0]:
            try:
                key = self.credentials(self.config[0])[0]
                self.ent.Client(timeout=2).request(self.config[0], key, 'PUT',
                    '/clip/v2/resource/entertainment_configuration/' + area, {'action': 'stop'}, True)
            except Exception:
                pass
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.discovery_pool.shutdown(wait=False, cancel_futures=True)
        self.client.close()
