"""Light output scheduler: diff-only commands, Bridge-wide budget, one in flight."""
import unittest
from concurrent.futures import Future
from types import SimpleNamespace

import test_auth
from test_auth import Parameter, Table
import hue_core


class Pixels:
    """Minimal stand-in for TOP.numpyArray(): shape (1, width, 4), pixels[0, i]."""
    def __init__(self, colors):
        self.colors = colors
        self.shape = (1, len(colors), 4)

    def __getitem__(self, pos):
        return self.colors[pos[1]]


def par(value):
    p = Parameter()
    p.val = value
    return p


class ControlTests(unittest.TestCase):
    tearDown = test_auth.AuthTests.tearDown

    def setUp(self):
        test_auth.AuthTests.setUp(self)
        c = self.c
        for name, value in dict(Send=True, On=True, Transition=0.0, Colorspace='srgb', Maxrate=10,
                                Interval=0.0, Refresh=2, Bridgeip='192.168.1.2', Apikey='key',
                                Keyhost='192.168.1.2').items():
            setattr(c.par, name, par(value))
        self.pixels = Pixels([(1, 0, 0, 1), (0, 0, 1, 1)])
        c.tables['rgba'] = SimpleNamespace(height=1, numpyArray=lambda delayed=True: self.pixels)
        c.inputs = [object()]
        light = {'on': {'on': True}, 'dimming': {'brightness': 50}, 'color': {'xy': {'x': .3, 'y': .3}}}
        self.ctrl.lights = {'a': dict(light, id='a'), 'b': dict(light, id='b')}
        self.ctrl.ready = True
        self.ctrl.config = ('192.168.1.2', True)
        c.tables['mapping'] = Table(['pixel', 'light_id', 'name'])
        c.tables['mapping'].appendRow([0, 'a', 'A'])
        c.tables['mapping'].appendRow([1, 'b', 'B'])
        self.sent = []
        self.ctrl.submit_control = self.fake_submit

    def fake_submit(self, lid, body, new_state=None):
        self.sent.append((lid, body))
        f = Future()
        self.ctrl.control_pending[lid] = (f, (self.ctrl.config, self.ctrl.generation), body, new_state)
        return f

    def finish(self, code=200, result=None, now=0):
        """Complete the single in-flight command."""
        lid, (f, *_rest) = next(iter(self.ctrl.control_pending.items()))
        f.set_result((code, result if result is not None else {'errors': [], 'data': [{'rid': lid}]}))
        self.ctrl.receive_controls(now)
        return lid

    def test_first_command_includes_power_color_and_brightness(self):
        self.ctrl.control_step(1.0)
        lid, body = self.sent[0]
        self.assertEqual(body['on'], {'on': True})
        self.assertIn('color', body)
        self.assertEqual(body['dimming']['brightness'], 100)

    def test_only_one_command_in_flight(self):
        self.ctrl.control_step(1.0)
        self.ctrl.control_step(5.0)
        self.assertEqual(len(self.sent), 1)

    def test_bridge_wide_rate_limit_spaces_commands(self):
        self.ctrl.control_step(1.0)
        self.finish(now=1.01)
        self.ctrl.control_step(1.05)  # 10/s budget: next token at 1.1
        self.assertEqual(len(self.sent), 1)
        self.ctrl.control_step(1.1)
        self.assertEqual(len(self.sent), 2)

    def test_changed_color_sends_only_color(self):
        for t in (1.0, 2.0):
            self.ctrl.control_step(t)
            self.finish(now=t)
        self.pixels.colors[0] = (0, 1, 0, 1)
        self.ctrl.control_step(3.0)
        lid, body = self.sent[-1]
        self.assertEqual(lid, 'a')
        self.assertEqual(set(body), {'color', 'dynamics'})

    def test_unchanged_lights_wait_for_refresh(self):
        for t in (1.0, 2.0):
            self.ctrl.control_step(t)
            self.finish(now=t)
        self.ctrl.control_step(2.5)
        self.assertEqual(len(self.sent), 2)
        self.ctrl.control_step(3.1)  # 'a' last sent at 1.0, Refresh 2 s
        lid, body = self.sent[-1]
        self.assertEqual(lid, 'a')
        self.assertIn('on', body)

    def test_largest_change_goes_first(self):
        for t in (1.0, 2.0):
            self.ctrl.control_step(t)
            self.finish(now=t)
        self.pixels.colors[0] = (1, 0, 0, .98)   # small brightness change
        self.pixels.colors[1] = (0, 1, 0, 1)     # blue -> green
        self.ctrl.control_step(2.2)
        self.assertEqual(self.sent[-1][0], 'b')

    def test_black_keeps_previous_color(self):
        for t in (1.0, 2.0):
            self.ctrl.control_step(t)
            self.finish(now=t)
        self.pixels.colors[0] = (0, 0, 0, .5)
        self.ctrl.control_step(3.0)
        lid, body = self.sent[-1]
        self.assertEqual(lid, 'a')
        self.assertNotIn('color', body)
        self.assertEqual(body['dimming']['brightness'], 50)

    def test_lights_off_sends_power_only(self):
        self.c.par.On.val = False
        self.ctrl.control_step(1.0)
        self.assertEqual(set(self.sent[0][1]), {'on', 'dynamics'})
        self.assertEqual(self.sent[0][1]['on'], {'on': False})

    def test_rate_limit_pauses_output_without_benching_the_light(self):
        self.ctrl.control_step(1.0)
        lid = self.finish(429, {'errors': [{'description': 'HTTP 429'}]}, now=1.0)
        self.assertTrue(self.ctrl.light_ready_for_send(lid, 1.0))
        self.assertNotIn(lid, self.ctrl.sent)
        self.ctrl.control_step(1.5)
        self.assertEqual(len(self.sent), 1)
        self.ctrl.control_step(2.1)
        self.assertEqual(len(self.sent), 2)

    def test_failed_command_forgets_state_and_retries_later(self):
        self.ctrl.control_step(1.0)
        lid = self.finish(200, {'errors': [{'description': 'communication_error'}], 'data': []}, now=1.0)
        self.assertNotIn(lid, self.ctrl.sent)
        self.assertFalse(self.ctrl.light_ready_for_send(lid, 5.9))

    def test_output_runs_while_a_light_list_poll_is_in_flight(self):
        self.ctrl.pending = (Future(), 'lights', (self.ctrl.config, self.ctrl.generation), None, None)
        self.ctrl.last_poll = 0
        self.ctrl.tick()
        self.assertEqual(len(self.sent), 1)

    def test_disabling_output_forgets_state_so_reenable_resends_everything(self):
        self.ctrl.control_step(1.0)
        self.finish(now=1.0)
        self.c.par.Send.val = False
        self.ctrl.control_step(1.5)
        self.assertEqual(self.ctrl.sent, {})


class StateCommandTests(unittest.TestCase):
    def test_small_changes_are_skipped(self):
        prev = {'on': True, 'bri': 50.0, 'xy': (.3, .3)}
        body, _, _ = hue_core.state_command(prev, {'on': True, 'bri': 50.2, 'xy': (.3005, .3)}, 0)
        self.assertIsNone(body)

    def test_refresh_sends_full_state(self):
        prev = {'on': True, 'bri': 50.0, 'xy': (.3, .3)}
        body, _, _ = hue_core.state_command(prev, dict(prev), 100, refresh=True)
        self.assertEqual(set(body), {'on', 'dimming', 'color', 'dynamics'})


if __name__ == '__main__':
    unittest.main()
