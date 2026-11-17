"""HueEntertainment: packets, colour, areas, pairing/streaming state machine, DTLS."""
import os
import shutil
import socket
import subprocess
import sys
import time
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import ent_core
import dtls_psk
import ent_runtime


class Cell:
    def __init__(self, value):
        self.val = str(value)


class Table:
    """Minimal stand-in for a Table DAT."""
    def __init__(self, headers):
        self.rows = [headers]

    @property
    def numRows(self):
        return len(self.rows)

    @property
    def numCols(self):
        return max((len(r) for r in self.rows), default=0)

    def __getitem__(self, pos):
        return Cell(self.rows[pos[0]][pos[1]])

    def __setitem__(self, pos, val):
        self.rows[pos[0]][pos[1]] = str(val)

    def appendRow(self, row):
        self.rows.append(list(row))

    def deleteRow(self, row):
        del self.rows[row]

    def clear(self):
        self.rows = []


class Parameter:
    def __init__(self):
        self.val = ''

    def eval(self):
        return self.val

AREA_ID = '1a8d99cc-967b-44f2-9202-43f976c0fa6e'


def par(value):
    p = Parameter()
    p.val = value
    return p


class FakeClient:
    def __init__(self, *a, **k):
        self.sent = []
        FakeDtls.instances.append(self)

    def connect(self):
        if FakeDtls.fail:
            raise dtls_psk.DtlsError(FakeDtls.fail)

    def send(self, data):
        self.sent.append(data)

    def poll_alert(self):
        return None

    def close(self):
        pass


class FakeDtls:
    DtlsError = dtls_psk.DtlsError
    DtlsPskClient = FakeClient
    instances = []
    fail = ''


class Pixels:
    def __init__(self, colors):
        self.colors = colors
        self.shape = (1, len(colors), 4)

    def __getitem__(self, pos):
        return self.colors[pos[1]]


class Par(Parameter):
    """Custom parameter stand-in with the attributes the runtime touches."""
    def __init__(self, name, value='', page=None, comp=None):
        super().__init__()
        self.name, self.val, self.page, self.comp = name, value, page, comp
        self.menuNames, self.menuLabels = [], []

    def destroy(self):
        self.page.pars.remove(self)
        delattr(self.comp.par, self.name)


class Page:
    """Custom page stand-in: append<Style>(name, label) creates a parameter."""
    def __init__(self, comp, name):
        self.comp, self.name, self.pars = comp, name, []

    def __getattr__(self, attr):
        if not attr.startswith('append'):
            raise AttributeError(attr)

        def add(name, label=''):
            default = {'appendInt': 0, 'appendFloat': 0.0, 'appendToggle': False}.get(attr, '')  # as in TD
            p = Par(name, default, self, self.comp)
            p.label, p.style = label, attr[len('append'):]
            self.pars.append(p)
            setattr(self.comp.par, name, p)
            return [p]
        return add


class Component:
    """Stand-in for the HueEntertainment COMP with its pages, tables and modules."""
    LAYOUT = {
        'Bridge': dict(Bridgeip='192.0.2.5', Apikey='', Clientkey='', Keyhost='', Status=''),
        'Area': dict(Area='none', Newareaname='TD'),
        'Lights': dict(Device='none'),
        'Stream': dict(Stream=False, Fps=50, Colorspace='srgb', Brightness='value', Smoothing=0.0),
    }

    def __init__(self):
        self.storage = {}
        self.tables = {k: Table(v) for k, v in {
            'errors': ['time', 'message'], 'mapping': ['pixel', 'channel_id', 'name', 'light_key']}.items()}
        self.pixels = Pixels([(1, 0, 0, 1), (0, 0, 1, 1)])
        self.tables['rgba'] = SimpleNamespace(height=1, numpyArray=lambda delayed=True: self.pixels)
        self.inputs = [object()]
        self.par = SimpleNamespace()
        self.customPages = []
        for page_name, pars in self.LAYOUT.items():
            pg = Page(self, page_name)
            self.customPages.append(pg)
            for name, value in pars.items():
                pg.appendStr(name)[0].val = value
        self.modules = {'ent_core': ent_core, 'dtls_psk': FakeDtls}

    def op(self, name):
        if name in self.modules:
            return SimpleNamespace(module=self.modules[name])
        return self.tables[name]

    def fetch(self, k, d=None):
        return self.storage.get(k, d)

    def store(self, k, v):
        self.storage[k] = v

    def page_pars(self, page):
        return [p.name for p in next(pg for pg in self.customPages if pg.name == page).pars]


def area_resources(n=2, gradient=False):
    cfg = {'id': AREA_ID, 'metadata': {'name': 'Stage'}, 'status': 'inactive', 'channels': []}
    ent, dev = [], []
    for i in range(n):
        owner = 'dev-0' if gradient else 'dev-%d' % i
        cfg['channels'].append({'channel_id': i, 'position': {'x': i, 'y': 0, 'z': 0},
                                'members': [{'service': {'rid': 'ent-%d' % i, 'rtype': 'entertainment'}, 'index': i}]})
        ent.append({'id': 'ent-%d' % i, 'owner': {'rid': owner}, 'renderer': True})
        if not gradient or i == 0:
            dev.append({'id': owner, 'metadata': {'name': 'Lamp %d' % i},
                        'services': [{'rid': 'light-%d' % i, 'rtype': 'light'}, {'rid': 'ent-%d' % i, 'rtype': 'entertainment'}]})
    return [cfg], ent, dev


class PacketTests(unittest.TestCase):
    def test_header_and_channel_layout(self):
        p = ent_core.packet(AREA_ID, [(3, 0.5, 0.25, 1.0)])
        self.assertEqual(p[:9], b'HueStream')
        self.assertEqual(p[9:16], bytes([2, 0, 0, 0, 0, ent_core.COLOR_XY, 0]))
        self.assertEqual(p[16:52], AREA_ID.encode())
        self.assertEqual(len(p), 52 + 7)
        self.assertEqual(p[52], 3)
        self.assertEqual(int.from_bytes(p[53:55], 'big'), 32768)
        self.assertEqual(int.from_bytes(p[57:59], 'big'), 65535)

    def test_values_are_clamped_and_channels_capped(self):
        p = ent_core.packet(AREA_ID, [(i, 2.0, -1, float('nan')) for i in range(25)])
        self.assertEqual(len(p), 52 + 7 * 20)
        self.assertEqual(p[53:59], b'\xff\xff\x00\x00\x00\x00')

    def test_bad_area_id_rejected(self):
        with self.assertRaises(ValueError):
            ent_core.packet('short', [])


class ColorTests(unittest.TestCase):
    def test_white_is_d65(self):
        x, y, b = ent_core.rgba_to_xyb((1, 1, 1, 1))
        self.assertAlmostEqual(x, 0.3127, places=3)
        self.assertAlmostEqual(y, 0.3290, places=3)
        self.assertEqual(b, 1)

    def test_value_mode_dims_dark_pixels(self):
        self.assertAlmostEqual(ent_core.rgba_to_xyb((0.5, 0, 0, 1))[2], 0.5)
        self.assertEqual(ent_core.rgba_to_xyb((0, 0, 0, 1))[2], 0)
        self.assertAlmostEqual(ent_core.rgba_to_xyb((1, 0, 0, 0.5))[2], 0.5)

    def test_alpha_mode_ignores_rgb_level(self):
        self.assertEqual(ent_core.rgba_to_xyb((0.1, 0, 0, 0.8), brightness='alpha')[2], 0.8)

    def test_red_is_red(self):
        x, y, _ = ent_core.rgba_to_xyb((1, 0, 0, 1))
        self.assertAlmostEqual(x, 0.64, places=2)
        self.assertAlmostEqual(y, 0.33, places=2)


class AreaTests(unittest.TestCase):
    def test_channels_named_after_devices(self):
        areas = ent_core.parse_areas(*area_resources())
        self.assertEqual([c['name'] for c in areas[0]['channels']], ['Lamp 0', 'Lamp 1'])

    def test_gradient_segments_are_numbered(self):
        areas = ent_core.parse_areas(*area_resources(3, gradient=True))
        self.assertEqual([c['name'] for c in areas[0]['channels']], ['Lamp 0 #0', 'Lamp 0 #1', 'Lamp 0 #2'])


class ControllerBase(unittest.TestCase):
    """Shared setup and helpers (no tests)."""
    def setUp(self):
        FakeDtls.instances = []
        FakeDtls.fail = ''
        self.c = Component()
        self.ctrl = ent_runtime.Controller(self.c)
        self.ctrl.config = ('192.0.2.5', True)
        self.fetch_time = 100.5

    fetch_time = 100.5

    def tearDown(self):
        self.ctrl.close()

    def respond(self, kind, value, code=200, extra=None):
        f = Future()
        f.set_result((code, value))
        self.ctrl.pending = (f, kind, (self.ctrl.config, self.ctrl.generation), extra)
        self.ctrl.receive(100)

    def paired(self):
        self.c.par.Apikey.val = 'app-key'
        self.c.par.Clientkey.val = '00112233445566778899aabbccddeeff'
        self.c.par.Keyhost.val = '192.0.2.5'

    def load_areas(self, n=2, resources=None, zigbee=None, on=None):
        cfg, ent, dev = resources or area_resources(n)
        if zigbee is None:
            zigbee = [{'owner': {'rid': d['id']}, 'status': 'connected'} for d in dev]
        lights = [{'id': s['rid'], 'owner': {'rid': d['id']}, 'on': {'on': (on or {}).get(s['rid'], True)}}
                  for d in dev for s in d.get('services', []) if s['rtype'] == 'light']
        steps = [('entertainment_configuration', cfg), ('entertainment', ent), ('device', dev),
                 ('zigbee_connectivity', zigbee), ('light', lights)]
        for i, (kind, data) in enumerate(steps):
            self.ctrl.fetch_queue = [k for k, _ in steps[i + 1:]]
            self.respond(kind, {'errors': [], 'data': data})
        self.ctrl.last_area_poll = time.monotonic()  # tick() uses the real clock

    def state(self):
        return self.c.par.Status.val

    def use(self):
        """{light name: toggle} as shown under Lights in Area."""
        return {getattr(self.c.par, n).label: getattr(self.c.par, n).val
                for n in self.c.page_pars('Area') if n.startswith('Use') and n[3:].isdigit()}

    def set_use(self, name, value):
        for n in self.c.page_pars('Area'):
            if n.startswith('Use') and n[3:].isdigit() and getattr(self.c.par, n).label == name:
                getattr(self.c.par, n).val = value
                return
        raise KeyError(name)

    def light_rows(self):
        """{light name: text} of the read-only list on the Lights page."""
        return {getattr(self.c.par, n).label: getattr(self.c.par, n).val
                for n in self.c.page_pars('Lights') if n.startswith('Light') and n[5:].isdigit()}


class ControllerTests(ControllerBase):

    def test_pairing_requests_and_stores_client_key(self):
        self.respond('auth', [{'success': {'username': 'u', 'clientkey': 'ABCD'}}])
        self.assertEqual(self.c.par.Clientkey.val, 'ABCD')
        self.assertEqual(self.c.par.Keyhost.val, '192.0.2.5')

    def test_pairing_without_client_key_is_an_error(self):
        self.respond('auth', [{'success': {'username': 'u'}}])
        self.assertEqual(self.c.par.Apikey.val, '')
        self.assertIn('client key', self.c.tables['errors'].rows[-1][1])

    def test_pairing_request_asks_for_client_key(self):
        self.ctrl.config = None
        self.ctrl.tick()
        f, kind, _, _ = self.ctrl.pending
        self.assertEqual(kind, 'auth')

    def mapping(self):
        return [r[:3] for r in self.c.tables['mapping'].rows[1:]]

    def test_single_area_is_selected_and_mapped(self):
        self.load_areas()
        self.assertTrue(self.ctrl.ready)
        self.assertEqual(self.c.par.Area.val, AREA_ID)
        self.assertEqual(self.c.tables['mapping'].rows[0], ['pixel', 'channel_id', 'name', 'light_key'])
        self.assertEqual(self.mapping(), [[0, 0, 'Lamp 0'], [1, 1, 'Lamp 1']])

    def test_mapping_always_covers_every_light_and_appends_new_ones(self):
        self.load_areas(2)
        self.c.tables['mapping'].deleteRow(1)        # a deleted light comes back (at the end)
        self.load_areas(2)
        self.assertEqual(self.mapping(), [[0, 1, 'Lamp 1'], [1, 0, 'Lamp 0']])
        self.load_areas(3)                           # new light appended, all renumbered
        self.assertEqual(self.mapping(), [[0, 1, 'Lamp 1'], [1, 0, 'Lamp 0'], [2, 2, 'Lamp 2']])
        self.load_areas(2)                           # light removed from the area
        self.assertEqual(self.mapping(), [[0, 1, 'Lamp 1'], [1, 0, 'Lamp 0']])

    def test_user_edit_is_renumbered_immediately(self):
        self.load_areas(3)
        t = self.c.tables['mapping']
        t[1, 0] = 1.5                                # move Lamp 0 between Lamp 1 and Lamp 2
        self.ctrl.check_mapping_edits(time.monotonic() + 1)
        self.assertEqual(self.mapping(), [[0, 1, 'Lamp 1'], [1, 0, 'Lamp 0'], [2, 2, 'Lamp 2']])
        t[1, 0] = 2                                  # duplicate pixel 2: table order breaks the tie
        self.ctrl.check_mapping_edits(time.monotonic() + 2)
        self.assertEqual([r[0] for r in self.mapping()], [0, 1, 2])

    def test_bridge_renumbering_channels_keeps_pixels_on_the_same_lights(self):
        self.load_areas(3)
        cfg, ent, dev = area_resources(3)
        for ch, new_id in zip(cfg[0]['channels'], (2, 0, 1)):   # Bridge reassigns channel ids
            ch['channel_id'] = new_id
        self.load_areas(resources=(cfg, ent, dev))
        self.assertEqual(self.mapping(), [[0, 2, 'Lamp 0'], [1, 0, 'Lamp 1'], [2, 1, 'Lamp 2']])

    def test_frame_uses_mapping_and_top(self):
        self.load_areas()
        p = self.ctrl.frame(self.ctrl.selected_area())
        self.assertEqual(len(p), 52 + 14)
        self.assertEqual(p[52], 0)
        self.assertAlmostEqual(int.from_bytes(p[53:55], 'big') / 65535, 0.64, places=2)

    def test_missing_input_sends_black(self):
        self.load_areas()
        self.c.inputs = []
        p = self.ctrl.frame(self.ctrl.selected_area())
        self.assertEqual(p[57:59], b'\x00\x00')

    def test_stream_start_runs_sender_and_stop_releases_area(self):
        self.paired()
        self.load_areas()
        self.c.par.Stream.val = True
        self.ctrl.tick()
        f, kind, _, extra = self.ctrl.pending
        self.assertEqual(kind, 'start')
        self.respond('start', {'errors': [], 'data': []}, extra=extra)
        self.assertIsNotNone(self.ctrl.sender)
        deadline = time.time() + 2
        while not (FakeDtls.instances and FakeDtls.instances[0].sent) and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(FakeDtls.instances[0].sent[0].startswith(b'HueStream'))
        self.c.par.Stream.val = False
        self.ctrl.pending = None
        self.ctrl.tick()
        self.assertIsNone(self.ctrl.sender)
        f, kind, _, _ = self.ctrl.pending
        self.assertEqual(kind, 'stop')

    def test_stream_disabled_while_starting_releases_area(self):
        self.paired()
        self.load_areas()
        self.c.par.Stream.val = False
        self.respond('start', {'errors': [], 'data': []}, extra=AREA_ID)
        self.assertIsNone(self.ctrl.sender)
        self.assertEqual(self.ctrl.stop_queue, [AREA_ID])

    def test_lost_stream_retries(self):
        self.paired()
        self.load_areas()
        self.c.par.Stream.val = True
        FakeDtls.fail = 'Handshake timed out'
        self.respond('start', {'errors': [], 'data': []}, extra=AREA_ID)
        self.ctrl.sender.join(1)
        self.ctrl.watch_sender(100)
        self.assertIsNone(self.ctrl.sender)
        self.assertIn('Stream lost', self.state())
        self.assertEqual(self.ctrl.retry_at, 103)

    def test_start_failure_backs_off(self):
        self.paired()
        self.load_areas()
        self.c.par.Stream.val = True
        self.respond('start', {'errors': [{'description': 'area in use'}]}, extra=AREA_ID)
        self.assertIsNone(self.ctrl.sender)
        self.assertEqual(self.ctrl.retry_at, 105)

    def test_secrets_are_redacted_in_errors(self):
        self.paired()
        self.ctrl.error('bad key 00112233445566778899aabbccddeeff for app-key')
        self.assertNotIn('app-key', self.c.tables['errors'].rows[-1][1])


@unittest.skipUnless(shutil.which('openssl'), 'needs the openssl command-line tool')
class DtlsEndToEndTests(unittest.TestCase):
    """Real DTLS 1.2 PSK handshake + HueStream frames against `openssl s_server`."""
    PSK = '0123456789abcdef0123456789abcdef'

    def setUp(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind(('127.0.0.1', 0))
            self.port = s.getsockname()[1]
        self.server = subprocess.Popen(
            ['openssl', 's_server', '-dtls1_2', '-listen', '-nocert', '-psk', self.PSK,
             '-psk_identity', 'app-key', '-cipher', 'PSK-AES128-GCM-SHA256', '-accept', '127.0.0.1:%d' % self.port],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        time.sleep(0.7)

    def tearDown(self):
        self.server.kill()
        self.server.communicate()

    def test_handshake_and_stream(self):
        client = dtls_psk.DtlsPskClient('127.0.0.1', 'app-key', bytes.fromhex(self.PSK), port=self.port)
        client.connect()
        frame = ent_core.packet(AREA_ID, [(0, .3, .3, 1)])
        for _ in range(3):
            client.send(frame + b'\n')
        time.sleep(0.3)
        client.close()
        time.sleep(0.3)
        self.server.kill()
        out = self.server.communicate()[0]
        self.assertIn(b'CIPHER is PSK-AES128-GCM-SHA256', out)
        self.assertEqual(out.count(b'HueStream'), 3)
        self.assertIn(AREA_ID.encode(), out)

    def test_wrong_key_fails_cleanly(self):
        client = dtls_psk.DtlsPskClient('127.0.0.1', 'app-key', b'\xff' * 16, port=self.port, timeout=0.3, retries=2)
        with self.assertRaises(dtls_psk.DtlsError):
            client.connect()


class CryptoTests(unittest.TestCase):
    def test_aes_fips197(self):
        k = bytes.fromhex('000102030405060708090a0b0c0d0e0f')
        p = bytes.fromhex('00112233445566778899aabbccddeeff')
        self.assertEqual(dtls_psk.AES128(k).encrypt_block(p).hex(), '69c4e0d86a7b0430d8cdb78070b4c55a')

    def test_gcm_nist_vector(self):
        # NIST GCM test case 4 (AES-128, 60-byte plaintext, 20-byte AAD), truncated to 96-bit IV form.
        key = bytes.fromhex('feffe9928665731c6d6a8f9467308308')
        iv = bytes.fromhex('cafebabefacedbaddecaf888')
        pt = bytes.fromhex('d9313225f88406e5a55909c5aff5269a86a7a9531534f7da2e4c303d8a318a721c3c0c95956809532fcf0e2449a6b525b16aedf5aa0de657ba637b39')
        aad = bytes.fromhex('feedfacedeadbeeffeedfacedeadbeefabaddad2')
        out = dtls_psk.AESGCM(key).encrypt(iv, pt, aad)
        self.assertEqual(out[-16:].hex(), '5bc94fbc3221a5db94fae95ae7121a47')
        self.assertEqual(out[:8].hex(), '42831ec2217774244b7221b784d0d49c'[:16])

    def test_prf_known_answer(self):
        # TLS 1.2 PRF (SHA-256) test vector widely used by implementations (IETF mailing list).
        secret = bytes.fromhex('9bbe436ba940f017b17652849a71db35')
        seed = bytes.fromhex('a0ba9f936cda311827a6f796ffd5198c')
        out = dtls_psk.prf(secret, b'test label', seed, 16)
        self.assertEqual(out.hex(), 'e3f229ba727be17b8d122620557cd453')


if __name__ == '__main__':
    unittest.main()


class AreaManagementTests(ControllerBase):
    """Create / delete Entertainment areas from TouchDesigner."""

    def resources_with_bridge_service(self, n=3):
        cfg, ent, dev = area_resources(n)
        ent.append({'id': 'bridge-ent', 'owner': {'rid': 'bridge'}, 'renderer': False})
        return cfg, ent, dev

    def test_lights_in_area_lists_renderers_only(self):
        self.load_areas(resources=self.resources_with_bridge_service())
        self.assertEqual(list(self.use()), ['Lamp 0', 'Lamp 1', 'Lamp 2'])
        self.assertEqual(self.c.page_pars('Area')[-4:], ['Useinfo', 'Use1', 'Use2', 'Use3'])
        self.assertEqual(getattr(self.c.par, 'Use1').style, 'Toggle')

    def test_area_members_on_others_off(self):
        cfg, ent, dev = area_resources(2)
        ent.append({'id': 'ent-x', 'owner': {'rid': 'dev-x'}, 'renderer': True})
        dev.append({'id': 'dev-x', 'metadata': {'name': 'Aaa spare'}})
        self.load_areas(resources=(cfg, ent, dev))
        self.assertEqual(self.use(), {'Aaa spare': False, 'Lamp 0': True, 'Lamp 1': True})

    def test_without_an_area_every_light_starts_on(self):
        cfg, ent, dev = area_resources(3)
        self.load_areas(resources=([], ent, dev))
        self.assertEqual(self.use(), {'Lamp 0': True, 'Lamp 1': True, 'Lamp 2': True})

    def test_new_light_starts_on_and_edits_survive_polls(self):
        self.load_areas(3)
        self.set_use('Lamp 0', False)
        cfg, ent, dev = area_resources(3)
        ent.append({'id': 'ent-new', 'owner': {'rid': 'dev-new'}, 'renderer': True})
        dev.append({'id': 'dev-new', 'metadata': {'name': 'New bulb'}})
        self.load_areas(resources=(cfg, ent, dev))   # periodic poll: keep the edit, the new bulb starts on
        self.assertEqual(self.use(), {'Lamp 0': False, 'Lamp 1': True, 'Lamp 2': True, 'New bulb': True})
        self.ctrl.pulse('Refreshareas')               # back to the area's actual members
        self.load_areas(resources=(cfg, ent, dev))
        self.assertEqual(self.use(), {'Lamp 0': True, 'Lamp 1': True, 'Lamp 2': True, 'New bulb': False})

    def test_create_uses_toggles_in_name_order_left_to_right(self):
        cfg, ent, dev = area_resources(3)
        dev[0]['metadata']['name'] = 'Zed'            # ent-0 sorts last by name
        self.load_areas(resources=(cfg, ent, dev))
        self.set_use('Lamp 1', False)
        self.ctrl.pulse('Createarea')
        kind, method, path, body, name = self.ctrl.management
        self.assertEqual((kind, method, name), ('create_area', 'POST', 'TD'))
        locs = body['locations']['service_locations']
        self.assertEqual([l['service']['rid'] for l in locs], ['ent-2', 'ent-0'])
        self.assertEqual([l['positions'][0]['x'] for l in locs], [-1.0, 1.0])
        self.assertEqual(body['type'], 'entertainment_configuration')

    def test_create_rejects_more_than_ten_lights(self):
        self.load_areas(11)
        self.ctrl.pulse('Createarea')
        self.assertIsNone(self.ctrl.management)
        self.assertIn('at most 10', self.c.tables['errors'].rows[-1][1])

    def test_create_without_lights_explains_what_to_do(self):
        self.load_areas(2)
        for name in ('Lamp 0', 'Lamp 1'):
            self.set_use(name, False)
        self.ctrl.pulse('Createarea')
        self.assertIsNone(self.ctrl.management)
        self.assertIn('Lights in Area', self.c.tables['errors'].rows[-1][1])

    def test_created_area_is_selected_after_refresh(self):
        self.load_areas(2)
        self.ctrl.pulse('Createarea')
        self.ctrl.management_inflight = True
        self.respond('create_area', {'errors': [], 'data': [{'rid': 'new-area', 'rtype': 'entertainment_configuration'}]},
                     extra='TD')
        self.assertIsNone(self.ctrl.management)
        self.assertTrue(self.ctrl.areas_dirty)
        cfg, ent, dev = area_resources(2)
        cfg.append(dict(cfg[0], id='new-area', metadata={'name': 'TD'}))
        self.load_areas(resources=(cfg, ent, dev))
        self.assertEqual(self.c.par.Area.val, 'new-area')

    def test_create_is_sent_once_by_tick(self):
        self.paired()
        self.load_areas(2)
        self.ctrl.pulse('Createarea')
        self.ctrl.tick()
        self.assertEqual(self.ctrl.pending[1], 'create_area')
        self.ctrl.pending = None
        self.ctrl.tick()
        self.assertNotEqual((self.ctrl.pending or (0, ''))[1], 'create_area')

    def test_delete_needs_confirmation_and_stops_stream(self):
        self.paired()
        self.load_areas(2)
        self.ctrl.pulse('Deletearea')
        self.assertIsNone(self.ctrl.management)
        self.ctrl.stream_area = AREA_ID
        self.ctrl.pulse('Deletearea', confirmed=True)
        self.assertEqual(self.ctrl.management[:3], ('delete_area', 'DELETE',
                         '/clip/v2/resource/entertainment_configuration/' + AREA_ID))
        self.assertEqual(self.ctrl.stop_queue, [AREA_ID])

    def test_failed_create_reports_and_unblocks(self):
        self.load_areas(2)
        self.ctrl.pulse('Createarea')
        self.ctrl.management_inflight = True
        self.respond('create_area', {'errors': [{'description': 'invalid configuration_type'}]}, code=400, extra='TD')
        self.assertIsNone(self.ctrl.management)
        self.assertIn('Could not create', self.state())

    def test_mapping_follows_left_to_right_position(self):
        cfg, ent, dev = area_resources(3)
        for ch, x in zip(cfg[0]['channels'], (0.5, -1.0, 0.0)):
            ch['position']['x'] = x
        self.load_areas(resources=(cfg, ent, dev))
        self.assertEqual([r[1] for r in self.c.tables['mapping'].rows[1:]], [1, 2, 0])

    def test_update_area_stops_stream_puts_new_lights_and_renumbers(self):
        self.paired()
        self.load_areas(3)
        self.ctrl.stream_area = AREA_ID
        self.set_use('Lamp 1', False)                 # drop
        self.ctrl.pulse('Updatearea')
        kind, method, path, body, extra = self.ctrl.management
        self.assertEqual((kind, method, extra), ('update_area', 'PUT', AREA_ID))
        self.assertTrue(path.endswith('/entertainment_configuration/' + AREA_ID))
        self.assertEqual([l['service']['rid'] for l in body['locations']['service_locations']], ['ent-0', 'ent-2'])
        self.assertEqual(self.ctrl.stop_queue, [AREA_ID])   # stop is sent before the update
        self.ctrl.management_inflight = True
        self.respond('update_area', {'errors': [], 'data': [{'rid': AREA_ID}]}, extra=AREA_ID)
        self.assertEqual(self.ctrl.retry_at, 0)
        cfg, ent, dev = area_resources(3)
        cfg[0]['channels'] = [cfg[0]['channels'][0], cfg[0]['channels'][2]]
        cfg[0]['channels'][0]['position']['x'], cfg[0]['channels'][1]['position']['x'] = -1, 1
        cfg[0]['channels'][0]['channel_id'], cfg[0]['channels'][1]['channel_id'] = 0, 1
        self.load_areas(resources=(cfg, ent, dev))
        self.assertEqual([r[:3] for r in self.c.tables['mapping'].rows[1:]], [[0, 0, 'Lamp 0'], [1, 1, 'Lamp 2']])
        self.assertEqual(self.use(), {'Lamp 0': True, 'Lamp 1': False, 'Lamp 2': True})

    def test_update_needs_a_selected_area(self):
        self.load_areas(2)
        self.c.par.Area.val = 'none'
        self.ctrl.pulse('Updatearea')
        self.assertIsNone(self.ctrl.management)

    def test_area_body_single_light_is_centered(self):
        body = ent_core.area_body('A', ['s'])
        self.assertEqual(body['locations']['service_locations'][0]['positions'][0]['x'], 0.0)


class SmoothingTests(ControllerBase):
    def bri(self, packet, i=0):
        off = 52 + 7 * i
        return int.from_bytes(packet[off + 5:off + 7], 'big') / 65535

    def x(self, packet, i=0):
        off = 52 + 7 * i
        return int.from_bytes(packet[off + 1:off + 3], 'big') / 65535

    def test_zero_smoothing_matches_direct_conversion(self):
        self.load_areas()
        area = self.ctrl.selected_area()
        p1 = self.ctrl.frame(area, now=10.0)
        x, y, b = ent_core.rgba_to_xyb((1, 0, 0, 1))
        self.assertAlmostEqual(self.x(p1), x, places=4)
        self.c.pixels.colors[0] = (0, 0, 0, 1)
        self.assertEqual(self.bri(self.ctrl.frame(area, now=10.02)), 0)

    def test_fade_to_black_keeps_hue_and_reaches_95_percent(self):
        self.load_areas()
        self.c.par.Smoothing.val = 1.0
        area = self.ctrl.selected_area()
        self.ctrl.frame(area, now=10.0)                    # settle on red at full brightness
        red_x = self.x(self.ctrl.frame(area, now=10.0))
        self.c.pixels.colors[0] = (0, 0, 0, 1)             # cut to black
        t, last = 10.0, 1.0
        for _ in range(50):                                 # 1 s at 50 fps
            t += 0.02
            p = self.ctrl.frame(area, now=t)
            self.assertLessEqual(self.bri(p), last + 1e-9)
            self.assertAlmostEqual(self.x(p), red_x, places=3)  # still red while dimming
            last = self.bri(p)
        self.assertAlmostEqual(last, 0.05, delta=0.01)

    def test_colour_change_passes_through_mix_not_jump(self):
        self.load_areas()
        self.c.par.Smoothing.val = 0.5
        area = self.ctrl.selected_area()
        self.ctrl.frame(area, now=1.0)
        self.c.pixels.colors[0] = (0, 0, 1, 1)
        x_mid = self.x(self.ctrl.frame(area, now=1.1))
        self.assertLess(ent_core.rgba_to_xyb((0, 0, 1, 1))[0], x_mid)
        self.assertLess(x_mid, ent_core.rgba_to_xyb((1, 0, 0, 1))[0])

    def test_area_change_resets_history(self):
        self.load_areas()
        self.c.par.Smoothing.val = 2.0
        area = self.ctrl.selected_area()
        self.ctrl.frame(area, now=1.0)
        self.c.pixels.colors[0] = (0, 0, 0, 1)
        other = dict(area, id='2b8d99cc-967b-44f2-9202-43f976c0fa6e')
        self.assertEqual(self.bri(self.ctrl.frame(other, now=1.02)), 0)  # no fade from the old area

    def test_smooth_step_math(self):
        self.assertEqual(ent_core.smooth_step(None, (1, 2), 0.1, 1), (1, 2))
        self.assertEqual(ent_core.smooth_step((0,), (1,), 0.1, 0), (1,))
        self.assertAlmostEqual(ent_core.smooth_step((0,), (1,), 1.0, 1.0)[0], 1 - 2.718281828 ** -3, places=6)


class SenderThreadTests(unittest.TestCase):
    def test_no_attribute_shadows_thread_internals(self):
        # Python 3.11's Thread.join() calls self._stop(); an attribute named _stop broke
        # it in TouchDesigner while newer Pythons (which run these tests) did not notice.
        import threading
        sender = ent_runtime.Sender(FakeDtls, '127.0.0.1', 'k', b'\0' * 16, 50)
        own = set(vars(sender)) - set(vars(threading.Thread()))
        # Internals across Python versions (3.11 in TouchDesigner, newer in CI).
        internals = {'_stop', '_started', '_is_stopped', '_tstate_lock', '_wait_for_tstate_lock',
                     '_target', '_args', '_kwargs', '_name', '_daemonic', '_ident', '_native_id',
                     '_handle', '_delete', '_bootstrap', '_bootstrap_inner', '_set_ident',
                     '_set_native_id', '_set_tstate_lock', '_initialized', '_stderr', '_invoke_excepthook'}
        clashes = sorted(name for name in own if name in internals or hasattr(threading.Thread, name))
        self.assertEqual(clashes, [])

    def test_stop_and_join(self):
        FakeDtls.fail = ''
        sender = ent_runtime.Sender(FakeDtls, '127.0.0.1', 'k', b'\0' * 16, 50)
        sender.payload = b'x'
        sender.start()
        time.sleep(0.05)
        sender.stop()
        sender.join(1)
        self.assertFalse(sender.is_alive())
        self.assertEqual(sender.state, 'stopped')


class NormalizeMappingTests(unittest.TestCase):
    CH = [{'key': k, 'channel_id': i, 'name': k.upper(), 'x': x, 'y': 0}
          for i, (k, x) in enumerate((('a', 0.0), ('b', -1.0), ('c', 1.0)))]

    def pix(self, rows, **kw):
        return [(r[0], r[3]) for r in ent_core.normalize_mapping(rows, self.CH, **kw)]

    def test_empty_table_orders_left_to_right(self):
        self.assertEqual(self.pix([]), [(0, 'b'), (1, 'a'), (2, 'c')])

    def test_gaps_and_duplicates_are_renumbered(self):
        rows = [('5', 'c'), ('5', 'a'), ('9', 'b')]
        self.assertEqual(self.pix(rows), [(0, 'c'), (1, 'a'), (2, 'b')])

    def test_fractional_value_moves_a_light(self):
        self.assertEqual(self.pix([('0', 'a'), ('1', 'b'), ('0.5', 'c')]), [(0, 'a'), (1, 'c'), (2, 'b')])

    def test_removed_dropped_new_appended_unreadable_last(self):
        rows = [('0', 'gone'), ('x', 'a'), ('1', 'c')]
        self.assertEqual(self.pix(rows), [(0, 'c'), (1, 'a'), (2, 'b')])

    def test_duplicate_key_rows_collapse(self):
        self.assertEqual(self.pix([('0', 'a'), ('1', 'a')]), [(0, 'a'), (1, 'b'), (2, 'c')])

    def test_rebuild_ignores_old_order(self):
        self.assertEqual(self.pix([('0', 'c'), ('1', 'a'), ('2', 'b')], rebuild=True), [(0, 'b'), (1, 'a'), (2, 'c')])

    def test_pixels_always_0_to_n_minus_1(self):
        import random
        rnd = random.Random(1)
        for _ in range(200):
            rows = [(str(rnd.choice([rnd.randint(-5, 30), 'x', '2.5', ''])), rnd.choice('abcz')) for _ in range(rnd.randint(0, 6))]
            out = ent_core.normalize_mapping(rows, self.CH)
            self.assertEqual([r[0] for r in out], [0, 1, 2])
            self.assertEqual(sorted(r[3] for r in out), ['a', 'b', 'c'])


class LightManagementTests(ControllerBase):
    """Features moved over from HueBridge.tox: search, delete device, Zigbee status."""

    def test_lights_page_lists_status_and_pixel_under_search(self):
        zig = [{'owner': {'rid': 'dev-0'}, 'status': 'connected'},
               {'owner': {'rid': 'dev-1'}, 'status': 'connectivity_issue'}]
        self.load_areas(2, zigbee=zig)
        self.assertEqual(self.c.page_pars('Lights'),
                         ['Searchlights', 'Lightinfo', 'Light1', 'Light2', 'Device', 'Deletedevice'])
        self.assertEqual(self.light_rows(), {'Lamp 0': 'on | connected | in area, pixel 0',
                                             'Lamp 1': 'on | connectivity_issue | in area, pixel 1'})
        self.assertIn('Lamp 1', self.c.tables['errors'].rows[-1][1])

    def test_light_list_follows_pixel_edits(self):
        self.load_areas(2)
        self.c.tables['mapping'][1, 0] = 5             # move Lamp 0 to the end
        self.ctrl.check_mapping_edits(time.monotonic() + 1)
        self.assertEqual(self.light_rows()['Lamp 0'], 'on | connected | in area, pixel 1')

    def test_status_updates_without_losing_toggle_edits(self):
        self.load_areas(2)
        self.set_use('Lamp 0', False)
        zig = [{'owner': {'rid': 'dev-0'}, 'status': 'disconnected'}, {'owner': {'rid': 'dev-1'}, 'status': 'connected'}]
        self.load_areas(2, zigbee=zig)
        self.assertFalse(self.use()['Lamp 0'])
        self.assertTrue(self.light_rows()['Lamp 0'].startswith('on | disconnected'))

    def test_device_selection_survives_light_list_rebuild(self):
        self.load_areas(2)
        self.c.par.Device.val = 'dev-1'
        cfg, ent, dev = area_resources(3)              # a light is added: pages are rebuilt
        self.load_areas(resources=(cfg, ent, dev))
        self.assertEqual(self.c.par.Device.val, 'dev-1')
        self.assertEqual(self.c.page_pars('Lights')[-2:], ['Device', 'Deletedevice'])

    def test_offline_light_reported_once(self):
        zig = [{'owner': {'rid': 'dev-0'}, 'status': 'disconnected'}, {'owner': {'rid': 'dev-1'}, 'status': 'connected'}]
        self.load_areas(2, zigbee=zig)
        n = self.c.tables['errors'].numRows
        self.ctrl.last_error = ''
        self.load_areas(2, zigbee=zig)
        self.assertEqual(self.c.tables['errors'].numRows, n)

    def test_device_menu_lists_lights(self):
        self.load_areas(2)
        self.assertEqual(self.c.par.Device.menuNames, ['none', 'dev-0', 'dev-1'])
        self.assertEqual(self.c.par.Device.menuLabels[1:], ['Lamp 0', 'Lamp 1'])

    def test_delete_device_needs_confirmation_and_stops_stream(self):
        self.paired()
        self.load_areas(2)
        self.c.par.Device.val = 'dev-1'
        self.ctrl.pulse('Deletedevice')
        self.assertIsNone(self.ctrl.management)
        self.ctrl.sender = SimpleNamespace(stop=lambda: None)
        self.ctrl.stream_area = AREA_ID
        self.ctrl.pulse('Deletedevice', confirmed=True)
        self.assertEqual(self.ctrl.management[:3], ('delete_device', 'DELETE', '/clip/v2/resource/device/dev-1'))
        self.assertIsNone(self.ctrl.sender)
        self.assertEqual(self.ctrl.stop_queue, [AREA_ID])
        self.assertEqual(self.ctrl.device_areas('dev-1'), ['Stage'])

    def test_deleted_device_leaves_light_lists_and_mapping(self):
        self.load_areas(3)
        self.ctrl.management = ('delete_device',) + (None,) * 4
        self.ctrl.management_inflight = True
        self.respond('delete_device', {'errors': [], 'data': [{'rid': 'dev-1'}]}, extra='Lamp 1')
        self.assertTrue(self.ctrl.areas_dirty)
        cfg, ent, dev = area_resources(3)
        del cfg[0]['channels'][1]; del ent[1]; del dev[1]
        self.load_areas(resources=(cfg, ent, dev))
        self.assertEqual(list(self.use()), ['Lamp 0', 'Lamp 2'])
        self.assertEqual(list(self.light_rows()), ['Lamp 0', 'Lamp 2'])
        self.assertEqual([r[:3] for r in self.c.tables['mapping'].rows[1:]], [[0, 0, 'Lamp 0'], [1, 2, 'Lamp 2']])

    def test_search_flow(self):
        self.paired()
        self.load_areas(2)
        self.ctrl.pulse('Searchlights')
        self.assertEqual(self.ctrl.management[:3], ('search_resources', 'GET', '/clip/v2/resource/zigbee_device_discovery'))
        self.ctrl.management_inflight = True
        self.respond('search_resources', {'errors': [], 'data': [{'id': 'disc', 'status': 'ready'}]})
        self.assertEqual(self.ctrl.management, ('search_start', 'PUT', '/clip/v2/resource/zigbee_device_discovery/disc',
                                                {'action': {'action_type': 'search'}}, None))
        self.ctrl.management_inflight = True
        self.respond('search_start', {'errors': [], 'data': []})
        self.assertIsNone(self.ctrl.management)
        self.ctrl.search_next = 0
        self.ctrl.search_deadline = time.monotonic() + 100  # tick() uses the real clock
        self.ctrl.last_area_poll = time.monotonic()
        self.ctrl.areas_dirty = False
        self.ctrl.tick()
        self.assertEqual(self.ctrl.pending[1], 'search_poll')
        self.ctrl.pending = None
        self.respond('search_poll', {'errors': [], 'data': [{'id': 'disc', 'status': 'active'}]})
        self.assertEqual(self.ctrl.search_id, 'disc')
        self.respond('search_poll', {'errors': [], 'data': [{'id': 'disc', 'status': 'ready'}]})
        self.assertIsNone(self.ctrl.search_id)
        self.assertIn('Search complete', self.state())

    def test_search_rejected_while_another_operation_runs(self):
        self.load_areas(2)
        self.ctrl.search_id = 'disc'
        self.ctrl.pulse('Searchlights')
        self.assertIsNone(self.ctrl.management)

    def test_periodic_refresh_keeps_streaming_message_and_runs_while_streaming(self):
        self.paired()
        self.load_areas(2)
        self.ctrl.status('stream', 'Streaming 2 channel(s) at 50 fps')
        self.load_areas(2)
        self.assertIn('Streaming', self.state())
        self.ctrl.sender = SimpleNamespace(payload=None, stop=lambda: None, join=lambda timeout=None: None, state='streaming', fps=50)
        self.ctrl.stream_area = AREA_ID
        self.ctrl.stream_announced = True
        self.c.par.Stream.val = True
        self.ctrl.last_area_poll = time.monotonic() - 11
        self.ctrl.areas_dirty = False
        self.ctrl.tick()
        self.assertEqual(self.ctrl.pending[1], 'entertainment_configuration')

    def test_discovery_fills_menu_without_a_bridges_table(self):
        self.c.par.Bridgeip.val = ''
        f = Future()
        f.set_result(([{'id': 'abc', 'ip': '192.0.2.9', 'source': 'mDNS'}], ''))
        self.ctrl.discovery_future = f
        self.ctrl.receive_discovery()
        self.assertEqual(self.c.par.Bridgeip.val, '192.0.2.9')
        self.assertNotIn('bridges', self.c.tables)


# ---------------------------------------------------------------- docked DATs and callbacks
class TextDat:
    def __init__(self, name):
        self.name, self.text, self.dock, self.valid = name, '', None, True
        self.par = SimpleNamespace(language='text')
        self._mod, self._src = None, None

    @property
    def module(self):
        if self._src != self.text:
            import types
            self._mod = types.ModuleType(self.name)
            exec(compile(self.text, self.name, 'exec'), self._mod.__dict__)
            self._src = self.text
        return self._mod


class Network:
    """The component's parent: holds the docked DATs."""
    def __init__(self):
        self.ops = {}

    def op(self, name):
        return self.ops.get(name)

    def create(self, typename, name):
        o = Table([]) if typename == 'tableDAT' else TextDat(name)
        o.name, o.dock, o.valid = name, None, True
        self.ops[name] = o
        return o

    def destroy(self, name):
        self.ops.pop(name).valid = False


class DatPar(Par):
    """OP-style parameter: eval() returns the operator its path points to."""
    def eval(self):
        if not self.val:
            return None
        o = self.comp.network.op(self.val.split('/')[-1])
        return o if o is not None and o.valid else None


class DockedComponent(Component):
    def __init__(self, name='HueEntertainment', network=None):
        super().__init__()
        self.name, self.nodeX, self.nodeY = name, 0, 0
        self.network = network or Network()
        pg = Page(self, 'Setup')
        self.customPages.append(pg)
        p = DatPar('Callbacksdat', '', pg, self)
        pg.pars.append(p)
        self.par.Callbacksdat = p

    def parent(self):
        return self.network

    def relativePath(self, o):
        return '../' + o.name

    @property
    def docked(self):
        return [o for o in self.network.ops.values() if o.dock is self]


RECORDER = '''
calls = []
def _rec(name):
    def f(comp, *args):
        calls.append((name,) + args)
    return f
for _n in ('onBridgesDiscovered onPaired onConnected onAreasChanged onLightsChanged onLightStatusChanged '
           'onSearchComplete onAreaCreated onAreaUpdated onAreaDeleted onLightDeleted onStreamStart '
           'onStreamStop onMappingChanged onError').split():
    globals()[_n] = _rec(_n)
'''


class CallbacksDatTests(unittest.TestCase):
    def test_created_below_component_docked_python(self):
        c = DockedComponent()
        c.nodeX, c.nodeY = 300, 200
        dat = ent_runtime.ensure_callbacks(c)
        self.assertEqual(list(c.network.ops), ['HueEntertainment_callbacks'])
        self.assertIs(dat.dock, c)
        self.assertEqual(dat.par.language, 'python')
        self.assertEqual(dat.nodeX, 300)
        self.assertLess(dat.nodeY, 200)
        self.assertEqual(dat.text, ent_runtime.CALLBACKS_TEMPLATE)
        self.assertEqual(c.par.Callbacksdat.val, '../HueEntertainment_callbacks')
        self.assertIs(ent_runtime.ensure_callbacks(c), dat)
        self.assertEqual(len(c.network.ops), 1)

    def test_deleted_callbacks_stay_deleted(self):
        c = DockedComponent()
        ent_runtime.ensure_callbacks(c)
        c.network.destroy('HueEntertainment_callbacks')
        self.assertIsNone(ent_runtime.ensure_callbacks(c))
        c.par.Callbacksdat.val = ''
        self.assertIsNone(ent_runtime.ensure_callbacks(c))
        self.assertEqual(c.network.ops, {})

    def test_copied_component_adopts_its_own_docked_copy(self):
        net = Network()
        a = DockedComponent('HueEntertainment', net)
        ent_runtime.ensure_callbacks(a)
        b = DockedComponent('HueEntertainment1', net)
        b.par.Callbacksdat.val = a.par.Callbacksdat.val   # copied parameter still points at the original
        b.storage['callbacks_created'] = True
        net.create('textDAT', 'HueEntertainment_callbacks1').dock = b
        self.assertEqual(ent_runtime.ensure_callbacks(b).name, 'HueEntertainment_callbacks1')
        self.assertEqual(b.par.Callbacksdat.val, '../HueEntertainment_callbacks1')

    def test_template_defines_every_event_the_runtime_fires(self):
        import re
        fired = set(re.findall(r"'(on[A-Z]\w+)'", Path(ent_runtime.__file__).read_text().split('CALLBACKS_TEMPLATE = ')[0] + Path(ent_runtime.__file__).read_text().split('class Sender')[1]))
        dat = TextDat('callbacks')
        dat.text = ent_runtime.CALLBACKS_TEMPLATE
        defined = {n for n in dir(dat.module) if n.startswith('on')}
        self.assertEqual(fired, defined)
        self.assertNotIn('    ', ent_runtime.CALLBACKS_TEMPLATE.split('"""', 2)[2])  # tabs, like TouchDesigner


class CallbackTests(ControllerBase):
    def setUp(self):
        FakeDtls.instances = []
        FakeDtls.fail = ''
        self.c = DockedComponent()
        self.ctrl = ent_runtime.Controller(self.c)
        self.ctrl.config = ('192.0.2.5', True)
        self.ctrl.check_callbacks(0)
        self.dat = self.c.network.op('HueEntertainment_callbacks')
        self.dat.text = RECORDER

    def calls(self, name=None):
        return [c for c in self.dat.module.calls if name is None or c[0] == name]

    def test_connect_reports_areas_and_lights_once(self):
        self.paired()
        self.load_areas(2)
        self.load_areas(2)
        self.assertEqual(self.calls('onConnected'), [('onConnected', '192.0.2.5')])
        areas = self.calls('onAreasChanged')
        self.assertEqual(len(areas), 1)
        self.assertEqual(areas[0][1], [{'id': AREA_ID, 'name': 'Stage', 'lights': ['Lamp 0', 'Lamp 1']}])
        lights = self.calls('onLightsChanged')
        self.assertEqual(len(lights), 1)
        self.assertEqual([l['name'] for l in lights[0][1]], ['Lamp 0', 'Lamp 1'])
        self.assertEqual(lights[0][1][0]['status'], 'connected')

    def test_light_status_change(self):
        self.paired()
        self.load_areas(2)
        cfg, ent, dev = area_resources(2)
        self.load_areas(2, zigbee=[{'owner': {'rid': 'dev-0'}, 'status': 'connectivity_issue'},
                                   {'owner': {'rid': 'dev-1'}, 'status': 'connected'}])
        (call,) = self.calls('onLightStatusChanged')
        self.assertEqual((call[1]['name'], call[2], call[3]), ('Lamp 0', 'connectivity_issue', 'connected'))

    def test_new_light_changes_lights_areas_and_mapping(self):
        self.paired()
        self.load_areas(2)
        self.load_areas(3)
        self.assertEqual(len(self.calls('onLightsChanged')), 2)
        self.assertEqual(len(self.calls('onAreasChanged')), 2)
        (call,) = self.calls('onMappingChanged')
        self.assertEqual([m['name'] for m in call[1]], ['Lamp 0', 'Lamp 1', 'Lamp 2'])
        self.assertEqual([m['pixel'] for m in call[1]], [0, 1, 2])

    def test_pixel_edit_reports_mapping(self):
        self.paired()
        self.load_areas(2)
        m = self.c.tables['mapping']
        m[1, 0] = 5
        self.ctrl.check_mapping_edits(10 ** 9)
        (call,) = self.calls('onMappingChanged')
        self.assertEqual([x['name'] for x in call[1]], ['Lamp 1', 'Lamp 0'])

    def test_stream_start_and_stop(self):
        self.paired()
        self.load_areas()
        self.c.par.Stream.val = True
        self.respond('start', {'errors': [], 'data': []}, extra=AREA_ID)
        deadline = time.time() + 2
        while self.ctrl.sender.state != 'streaming' and time.time() < deadline:
            time.sleep(0.01)
        self.ctrl.watch_sender(100)
        (start,) = self.calls('onStreamStart')
        self.assertEqual(start[1]['name'], 'Stage')
        self.c.par.Stream.val = False
        self.ctrl.pending = None
        self.ctrl.tick()
        self.assertEqual(self.calls('onStreamStop'), [('onStreamStop', start[1], 'disabled')])

    def test_lost_stream_after_start_reports_reason(self):
        self.paired()
        self.load_areas()
        self.c.par.Stream.val = True
        self.ctrl.sender = SimpleNamespace(state='streaming', fps=50, error='', stop=lambda: None)
        self.ctrl.stream_area = AREA_ID
        self.ctrl.watch_sender(100)
        self.ctrl.sender.state, self.ctrl.sender.error = 'stopped', 'Handshake timed out'
        self.ctrl.watch_sender(101)
        self.assertEqual(self.calls('onStreamStop')[0][2], 'lost: Handshake timed out')

    def test_area_and_light_operations(self):
        self.paired()
        self.load_areas(2)
        self.respond('create_area', {'errors': [], 'data': [{'rid': AREA_ID}]}, extra='Stage')
        self.assertEqual(self.calls('onAreaCreated'), [])   # reported once the refresh has the new area
        self.load_areas(2)
        self.assertEqual(self.calls('onAreaCreated')[0][1]['id'], AREA_ID)
        self.respond('delete_area', {'errors': [], 'data': []}, extra='Stage')
        self.respond('delete_device', {'errors': [], 'data': []}, extra='Lamp 1')
        self.assertEqual(self.calls('onAreaDeleted'), [('onAreaDeleted', 'Stage')])
        self.assertEqual(self.calls('onLightDeleted'), [('onLightDeleted', 'Lamp 1')])

    def test_search_complete_lists_new_lights(self):
        self.paired()
        self.load_areas(2)
        self.ctrl.pulse('Searchlights')
        self.ctrl.management = None
        self.ctrl.search_id = 'disc'
        self.respond('search_poll', {'errors': [], 'data': [{'id': 'disc', 'status': 'ready'}]})
        self.load_areas(3)
        (call,) = self.calls('onSearchComplete')
        self.assertEqual([l['name'] for l in call[1]], ['Lamp 2'])

    def test_paired_discovered_and_error(self):
        self.respond('auth', [{'success': {'username': 'u', 'clientkey': 'aa'}}])
        self.assertEqual(self.calls('onPaired'), [('onPaired', '192.0.2.5')])
        f = Future()
        f.set_result(([{'id': 'abc', 'ip': '192.0.2.9', 'source': 'mDNS'}], ''))
        self.ctrl.discovery_future = f
        self.ctrl.receive_discovery()
        self.assertEqual(self.calls('onBridgesDiscovered'), [('onBridgesDiscovered', [{'id': 'abc', 'ip': '192.0.2.9'}])])
        self.ctrl.error('boom')
        self.assertEqual(self.calls('onError'), [('onError', 'boom')])

    def test_failing_callback_is_reported_not_raised(self):
        self.dat.text = 'def onConnected(comp, ip):\n\traise RuntimeError("oops")\ndef onError(comp, m):\n\traise RuntimeError("again")\n'
        self.paired()
        self.load_areas(2)
        self.assertIn('Callback onConnected failed: oops', [r[1] for r in self.c.tables['errors'].rows])

    def test_pulse_creates_a_fresh_template(self):
        self.c.network.destroy('HueEntertainment_callbacks')
        self.ctrl.check_callbacks(10)
        self.assertNotIn('HueEntertainment_callbacks', self.c.network.ops)
        self.ctrl.pulse('Newcallbacks')
        dat = self.c.network.op('HueEntertainment_callbacks')
        self.assertEqual(dat.text, ent_runtime.CALLBACKS_TEMPLATE)
        self.assertIs(dat.dock, self.c)
        self.assertEqual(self.c.par.Callbacksdat.val, '../HueEntertainment_callbacks')


class LightPowerTests(ControllerBase):
    def capture(self):
        sent = []
        self.ctrl.submit = lambda kind, method, path, body=None, extra=None: sent.append((kind, method, path, body, extra))
        return sent

    def run_ticks(self, n=3):
        for _ in range(n):
            self.ctrl.power_next = 0
            self.ctrl.tick()

    def test_row_shows_bridge_on_off_state(self):
        self.paired()
        self.load_areas(2, on={'light-1': False})
        self.assertTrue(self.light_rows()['Lamp 1'].startswith('off | connected'))

    def test_on_and_off_are_sent_whatever_the_shown_state(self):
        self.paired()
        self.load_areas(2)              # both shown as on
        sent = self.capture()
        self.ctrl.set_power('Lamp 1', True)     # on again: still sent
        self.ctrl.set_power(1, False)
        self.run_ticks()
        self.assertEqual(sent, [
            ('power', 'PUT', '/clip/v2/resource/light/light-1', {'on': {'on': True}}, ('Lamp 1', True)),
            ('power', 'PUT', '/clip/v2/resource/light/light-0', {'on': {'on': False}}, ('Lamp 0', False))])

    def test_latest_pulse_for_a_light_wins(self):
        self.paired()
        self.load_areas(2)
        self.ctrl.set_power('Lamp 0', False)
        self.ctrl.set_power('lamp 0', True)
        self.assertEqual(self.ctrl.power_queue, [('Lamp 0', 'light-0', True)])

    def test_success_rereads_lights_and_failure_is_reported(self):
        self.paired()
        self.load_areas(2)
        self.ctrl.areas_dirty = False
        self.respond('power', {'errors': [], 'data': []}, extra=('Lamp 0', False))
        self.assertTrue(self.ctrl.areas_dirty)
        self.assertIn('Switched "Lamp 0" off', self.state())
        self.respond('power', {'errors': [{'description': 'device unreachable'}], 'data': []}, extra=('Lamp 0', True))
        self.assertTrue(self.ctrl.ready)
        self.assertIn('Could not switch "Lamp 0"', self.c.tables['errors'].rows[-1][1])

    def test_streaming_light_is_warned(self):
        self.paired()
        self.load_areas(2)
        self.ctrl.stream_area = AREA_ID
        self.ctrl.set_power(None, False)
        self.assertIn('streaming lights ignore it: Lamp 0, Lamp 1', self.state())

    def test_unknown_light_is_reported(self):
        self.paired()
        self.load_areas(2)
        self.assertEqual(self.ctrl.set_power(['Lamp 0', 'Nope'], False), ['Lamp 0'])
        self.assertIn('Unknown light: Nope', self.c.tables['errors'].rows[-1][1])


class ColorFormatTests(ControllerBase):
    def test_rgb_format_header_and_values(self):
        self.paired()
        self.load_areas(2)
        self.c.par.Colorformat = par('rgb')
        packet = self.ctrl.frame(self.ctrl.selected_area(), now=1)
        self.assertEqual(packet[14], ent_core.COLOR_RGB)
        values = lambda i: [int.from_bytes(packet[52 + 7 * i + 1 + 2 * k:52 + 7 * i + 3 + 2 * k], 'big') for k in range(3)]
        self.assertEqual(values(0), [65535, 0, 0])       # red pixel
        self.assertEqual(values(1), [0, 0, 65535])       # blue pixel

    def test_xy_is_the_default(self):
        self.paired()
        self.load_areas(2)
        self.assertEqual(self.ctrl.frame(self.ctrl.selected_area(), now=1)[14], ent_core.COLOR_XY)

    def test_linear_to_rgb(self):
        self.assertEqual(ent_core.linear_to_rgb(0, 0, 0, 1), (0.0, 0.0, 0.0))
        r, g, b = ent_core.linear_to_rgb(0.2140, 0.2140, 0.2140, 0.5)   # mid grey, half brightness
        self.assertAlmostEqual(r, 0.5)
        r, g, b = ent_core.linear_to_rgb(1.0, 0.2140, 0.0, 1.0)         # sRGB (1, 0.5, 0)
        self.assertAlmostEqual(g, 0.5, places=3)


class CloseTests(ControllerBase):
    def test_close_does_not_block_and_releases_area_in_background(self):
        import threading
        self.paired()
        self.load_areas(2)
        self.ctrl.stream_area = AREA_ID
        calls = []
        started = threading.Event()

        class SlowClient:
            def __init__(self, timeout=None):
                pass

            def request(self, *args):
                started.wait(2)
                calls.append(args[2:4])

        self.ctrl.ent = SimpleNamespace(**{**{k: getattr(ent_core, k) for k in dir(ent_core) if not k.startswith('_')},
                                           'Client': SlowClient})
        t0 = time.monotonic()
        self.ctrl.close()
        self.assertLess(time.monotonic() - t0, 0.2)
        started.set()
        for th in threading.enumerate():
            if th.name == 'HueRelease':
                th.join(3)
        self.assertEqual(calls, [('PUT', '/clip/v2/resource/entertainment_configuration/' + AREA_ID)])

    def tearDown(self):
        pass


import ent_ext


class ExtensionTests(ControllerBase):
    def setUp(self):
        super().setUp()
        self.c.modules['lifecycle'] = SimpleNamespace(get_controller=lambda: self.ctrl)
        self.ext = ent_ext.HueEntertainmentExt(self.c)
        self.paired()
        self.load_areas(2, on={'light-1': False})

    def test_state(self):
        self.assertTrue(self.ext.Ready)
        self.assertFalse(self.ext.Streaming)
        self.assertEqual(self.ext.Areas, [{'id': AREA_ID, 'name': 'Stage', 'lights': ['Lamp 0', 'Lamp 1']}])
        self.assertEqual(self.ext.SelectedArea['name'], 'Stage')
        self.assertEqual([(l['name'], l['on']) for l in self.ext.Lights], [('Lamp 0', True), ('Lamp 1', False)])
        self.assertEqual([m['name'] for m in self.ext.Mapping], ['Lamp 0', 'Lamp 1'])

    def test_light_on_off(self):
        self.assertEqual(self.ext.LightOff(), ['Lamp 0', 'Lamp 1'])
        self.assertEqual(self.ext.LightOn('Lamp 1'), ['Lamp 1'])
        self.assertEqual(self.ctrl.power_queue, [('Lamp 0', 'light-0', False), ('Lamp 1', 'light-1', True)])

    def test_stream_and_color_format(self):
        self.c.par.Colorformat = par('xy')
        self.ext.StartStream('Stage')
        self.assertTrue(self.c.par.Stream.val)
        self.ext.SetColorFormat('rgb')
        self.assertEqual(self.c.par.Colorformat.val, 'rgb')
        self.ext.StopStream()
        self.assertFalse(self.c.par.Stream.val)

    def test_create_area_from_named_lights(self):
        self.ext.CreateArea('Desk', ['Lamp 1'])
        self.assertEqual(self.use(), {'Lamp 0': False, 'Lamp 1': True})
        self.assertEqual(self.ctrl.management[0], 'create_area')
        self.assertEqual(self.c.par.Newareaname.val, 'Desk')

    def test_delete_light_and_area_without_dialog(self):
        self.ext.DeleteLight('Lamp 1')
        self.assertEqual(self.ctrl.management[:3], ('delete_device', 'DELETE', '/clip/v2/resource/device/dev-1'))
        self.ctrl.management = None
        self.ext.DeleteArea('Stage')
        self.assertEqual(self.ctrl.management[0], 'delete_area')

    def test_unknown_names_raise(self):
        with self.assertRaises(ValueError):
            self.ext.SelectArea('Nope')
        with self.assertRaises(ValueError):
            self.ext.CreateArea('X', ['Nope'])
