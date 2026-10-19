from dataclasses import replace
import asyncio
import ctypes
import json
import math
import os
from pathlib import Path
import struct
import sys
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from main import Bridge
from model import CONTROLS, Edge, Output, Snapshot, Tracker, checked_config, defaults
from native import Address, Module, Process, ReadError, pattern_matches, process_ids
from reader import Reader, TickChanged
from sampling import Sampler


def snap(tick=1, **kwargs):
    return Snapshot(session="map1", player=0x100000, tick=tick, game_time=float(tick), health=100, **kwargs)


class StateTests(unittest.TestCase):
    def test_incap_detected_with_frozen_prediction_tick(self):
        self.assertEqual(self.observe(snap(), replace(snap(), health=300, incap=True)), [Edge("incap", 100)])

    def test_server_time_advances_when_prediction_tick_frozen(self):
        self.assertEqual(self.observe(snap(), replace(snap(), game_time=2, health=93)), [Edge("hurt", 7)])

    def setUp(self): self.tracker = Tracker()

    def observe(self, first, second):
        self.tracker.observe(first)
        return self.tracker.observe(second)[0]

    def test_first_and_duplicate_do_not_trigger(self):
        initial = replace(snap(), health=93)
        self.assertEqual(self.tracker.observe(initial), ([], True))
        self.assertEqual(self.tracker.observe(initial), ([], False))

    def test_health_100_to_93(self):
        self.assertEqual(self.observe(snap(), replace(snap(2), health=93)), [Edge("hurt", 7)])

    def test_temp_decay_is_not_damage(self):
        first = replace(snap(), health=1, temp_buffer=50, temp_time=0)
        second = replace(first, tick=2, game_time=2)
        self.assertEqual(self.observe(first, second), [])

    def test_temp_damage_is_damage(self):
        first = replace(snap(), health=1, temp_buffer=50, temp_time=1)
        second = replace(first, tick=2, game_time=2, temp_buffer=40, temp_time=2)
        edges = self.observe(first, second)
        self.assertEqual(edges[0].name, "hurt")
        self.assertAlmostEqual(edges[0].amount, 9.73)

    def test_incap_pool_not_heal(self):
        self.assertEqual(self.observe(snap(), replace(snap(2), health=300, incap=True)), [Edge("incap", 100)])

    def test_ledge_not_duplicate_incap(self):
        self.assertEqual(self.observe(snap(), replace(snap(2), health=300, incap=True, ledge=True)), [Edge("ledge")])

    def test_death_not_massive_hurt(self):
        self.assertEqual(self.observe(snap(), replace(snap(2), health=0, life_state=2)), [Edge("death", 100)])

    def test_death_with_clock_regression_triggers_once_and_stops_incap(self):
        for tick, game_time in ((0, 1.0), (101, 0.0)):
            tracker = Tracker()
            out = Output({"mode": "challenge", "incap_preset": "incap", "death_preset": "death"})
            standing = snap(99)
            down = replace(snap(100), health=1, incap=True)
            tracker.observe(standing)
            edges, fresh = tracker.observe(down)
            out.render(down, edges, 0, fresh=fresh)
            dying = replace(down, tick=tick, game_time=game_time, life_state=1, incap=False)
            edges, fresh = tracker.observe(dying)
            self.assertEqual(edges, [Edge("death")])
            self.assertFalse(fresh)
            messages = out.render(dying, edges, 1, fresh=fresh)
            triggers = [m for m in messages if m["op"] == "trigger"]
            self.assertEqual([m["preset"] for m in triggers], ["death"])
            self.assertIsNone(out.sustain_due)
            self.assertEqual(out.last_strength, {"a": 60, "b": 60})
            dead = replace(dying, life_state=2)
            self.assertEqual(tracker.observe(dead)[0], [])

    def test_respawn_not_heal(self):
        self.assertEqual(self.observe(replace(snap(), health=0, life_state=2), snap(2)), [Edge("respawn")])

    def test_revive_not_damage(self):
        first = replace(snap(), health=200, incap=True)
        self.assertEqual(self.observe(first, replace(snap(2), health=30)), [Edge("revive")])

    def test_all_controls_enter_once(self):
        for control in CONTROLS:
            tracker = Tracker()
            tracker.observe(snap())
            current = replace(snap(2), controls=(control,))
            self.assertEqual(tracker.observe(current)[0], [Edge(control)])
            self.assertEqual(tracker.observe(replace(current, tick=3, game_time=3))[0], [])

    def test_charger_phase_switch_not_release(self):
        self.assertEqual(self.observe(replace(snap(), controls=("charger_carry",)),
                                      replace(snap(2), controls=("charger_pummel",))), [Edge("charger_pummel")])

    def test_release(self):
        self.assertEqual(self.observe(replace(snap(), controls=("smoker",)), snap(2)), [Edge("released")])

    def test_rebase_map_clock_entity_and_spectator(self):
        for candidate in (replace(snap(2), session="map2", health=1),
                          replace(snap(2), player=0x200000, health=1),
                          replace(snap(0), health=1), replace(snap(2), team=1)):
            tracker = Tracker(); tracker.observe(snap())
            self.assertEqual(tracker.observe(candidate)[0], [])

    def test_missed_ticks_counted(self):
        self.observe(snap(), snap(5))
        self.assertEqual(self.tracker.missed_ticks, 3)

    def test_heal(self):
        self.assertEqual(self.observe(replace(snap(), health=50), replace(snap(2), health=80)), [Edge("heal", 30)])


class OutputTests(unittest.TestCase):
    def test_removed_events_have_no_settings_or_feedback(self):
        removed = ("black_white", "revive", "heal", "respawn")
        cfg = checked_config({f"{name}_preset": "obsolete" for name in removed})
        self.assertFalse(any(key.startswith(tuple(name + "_" for name in removed)) for key in cfg))
        out = Output(cfg)
        out.render(snap(), [], 0)
        for name in removed:
            messages = out.render(snap(2), [Edge(name)], 1)
            self.assertFalse(any(m["op"] in ("trigger", "event") for m in messages))

    def test_single_hit_incap_counts_remaining_standing_pool(self):
        out = Output({"mode": "challenge", "incap_preset": "down"})
        out.render(snap(), [], 0)
        messages = out.render(replace(snap(2), health=300, incap=True), [Edge("incap", 100)], 1)
        self.assertEqual(out.last_strength["a"], 60)
        self.assertTrue(any(m.get("preset") == "down" for m in messages))

    def test_incap_waveform_renews_and_stops_on_revive(self):
        out = Output({"incap_preset": "down", "incap_sustain": True})
        down = replace(snap(), health=300, incap=True)
        initial = out.render(down, [], 0, fresh=True)
        self.assertTrue(any(m.get("preset") == "down" for m in initial))
        self.assertIsNotNone(out.next_deadline(0))
        renewed = out.render(down, [], 0.4)
        self.assertTrue(any(m.get("preset") == "down" for m in renewed))
        released = out.render(snap(2), [Edge("revive")], 0.5)
        self.assertFalse(any(m.get("preset") == "down" for m in released))
        self.assertIsNone(out.sustain_due)

    def test_incap_waveform_stops_on_death(self):
        out = Output({"incap_preset": "down"})
        out.render(replace(snap(), health=300, incap=True), [], 0)
        messages = out.render(replace(snap(2), health=0, life_state=2), [Edge("death")], 1)
        self.assertFalse(any(m.get("preset") == "down" for m in messages))
        self.assertIsNone(out.sustain_due)

    def test_overdue_expiry_is_not_lost(self):
        out = Output({"hurt_strength": 40, "hurt_duration": 0.05})
        out.render(snap(), [Edge("hurt", 1)], 0)
        self.assertEqual(out.next_deadline(1), 0.05)
        out.render(snap(), [], 1)
        self.assertIsNone(out.next_deadline(1))

    def test_fresh_downed_challenge_has_strength(self):
        out = Output({"mode": "challenge"})
        out.render(replace(snap(), health=300, incap=True), [], 0, fresh=True)
        self.assertEqual(out.last_strength["a"], 60)
    def test_dynamic_current_missing_hp(self):
        out = Output({"hp_scale": 2, "strength_cap": 60})
        out.render(replace(snap(), health=93), [], 0)
        self.assertEqual(out.last_strength, {"a": 14, "b": 14})
        out.render(snap(2), [Edge("heal", 7)], 1)
        self.assertEqual(out.last_strength, {"a": 0, "b": 0})

    def test_cap_and_disabled_hurt_does_not_disable_base(self):
        out = Output({"hp_scale": 10, "strength_cap": 40, "hurt_enabled": False})
        messages = out.render(replace(snap(), health=93), [Edge("hurt", 7)], 0)
        self.assertEqual(out.last_strength["a"], 40)
        self.assertTrue(all(message["op"] == "set_strength" for message in messages))

    def test_waveform_and_strength_independent(self):
        out = Output({"hurt_preset": "hit", "hurt_channel": "b"})
        messages = out.render(snap(), [Edge("hurt", 5)], 0)
        trigger = next(message for message in messages if message["op"] == "trigger")
        self.assertEqual(trigger["action"], "waveform")
        self.assertEqual(trigger["channel"], "b")
        self.assertNotIn("delta_pct", trigger)

    def test_overlay_per_channel_expires(self):
        out = Output({"hurt_strength": 40, "hurt_channel": "b", "hurt_duration": 1})
        current = replace(snap(), health=93)
        out.render(current, [Edge("hurt", 7)], 0)
        self.assertEqual(out.last_strength, {"a": 7, "b": 40})
        out.render(current, [], 2)
        self.assertEqual(out.last_strength, {"a": 7, "b": 7})

    def test_dynamic_death_holds_until_duration(self):
        out = Output({"death_duration": 3})
        out.render(replace(snap(), health=50), [], 0)
        dead = replace(snap(2), health=0, life_state=2)
        out.render(dead, [Edge("death")], 1)
        self.assertEqual(out.last_strength["a"], 50)
        out.render(dead, [], 3.9)
        self.assertEqual(out.last_strength["a"], 50)
        out.render(dead, [], 4.1)
        self.assertEqual(out.last_strength["a"], 0)

    def test_revive_cancels_death_hold(self):
        out = Output({"death_duration": 3})
        out.render(replace(snap(), health=50), [], 0)
        out.render(replace(snap(2), health=0, life_state=2), [Edge("death")], 1)
        out.render(snap(3), [Edge("respawn")], 2)
        self.assertEqual(out.last_strength["a"], 0)

    def test_fixed_and_challenge(self):
        out = Output({"mode": "fixed", "fixed_strength": 20})
        out.render(snap(), [], 0)
        out.render(replace(snap(2), health=0, life_state=2), [Edge("death")], 1)
        self.assertEqual(out.last_strength["a"], 20)
        out = Output({"mode": "challenge"})
        out.render(snap(), [Edge("hurt", 7)], 0)
        out.render(snap(2), [Edge("heal", 7)], 1)
        self.assertEqual(out.last_strength["a"], 7)

    def test_unknown_data_clears_both_channels(self):
        out = Output({"mode": "fixed", "fixed_strength": 20})
        out.render(snap(), [], 0)
        self.assertEqual(out.render(None, [], 1), [{"op": "set_strength", "channel": "both", "pct": 0}])

    def test_does_not_keep_old_hurt_overlay_after_death(self):
        out = Output({"hurt_strength": 40, "hurt_duration": 100, "death_duration": 1})
        out.render(snap(), [Edge("hurt", 1)], 0)
        dead = replace(snap(2), health=0, life_state=2)
        out.render(dead, [Edge("death")], 1)
        out.render(dead, [], 3)
        self.assertEqual(out.last_strength["a"], 0)

    def test_config_rejects_invalid_not_silent_clamp(self):
        for bad in ({"hp_scale": float("nan")}, {"strength_cap": 101}, {"channel": "bad"},
                    {"observe_only": "false"}, {"death_duration": float("inf")}):
            with self.assertRaises(ValueError): checked_config(bad)


class NativeTests(unittest.TestCase):
    def test_signature_wildcards_and_uniqueness(self):
        self.assertEqual(list(pattern_matches(b"\xA1\x11\x22\x33\x44\xC3", "A1 ?? ?? ?? ?? C3")), [0])
        with self.assertRaises(ValueError): list(pattern_matches(b"1234", "?? ?? ?? ??"))

    @unittest.skipUnless(os.name == "nt", "Windows process API")
    def test_actual_read_own_process_64bit_handle(self):
        original = ctypes.c_int32(987654)
        with Process(os.getpid()) as process:
            self.assertEqual(struct.unpack("<i", process.read(ctypes.addressof(original), 4))[0], 987654)
            self.assertEqual(process.pointer_size, ctypes.sizeof(ctypes.c_void_p))
            self.assertTrue(process.alive())
            self.assertTrue(process.modules)
            with self.assertRaises(ReadError): process.read(0, 4)
        self.assertIsNone(process.handle)

    def test_profile_fields_match_discovery(self):
        profile = json.loads((ROOT / "profiles/game.json").read_text(encoding="utf-8"))
        self.assertEqual(profile["fields"]["health"]["offset"], "0xec")
        self.assertEqual(profile["fields"]["team"]["offset"], "0xe4")
        Reader(ROOT / "profiles/game.json")

    def test_serial_number_prevents_stale_control_handle(self):
        reader = Reader(ROOT / "profiles/game.json")
        reader.index_bits, reader.stride, reader.serial_offset = 12, 16, 4
        class FakeAddress:
            def resolve(self): return 0x100000
        class FakeProcess:
            pointer_size = 4
            def read(self, addr, size):
                if size == 16: return struct.pack("<4I", 0x200000, 5, 0, 0)
                if size == 4: return struct.pack("<i", 3)
                return b"\0"
        reader.entities, reader.process = FakeAddress(), FakeProcess()
        self.assertTrue(reader._active_handle((5 << 12) | 1))
        self.assertFalse(reader._active_handle((4 << 12) | 1))
        self.assertFalse(reader._active_handle(0xFFFFFFFF))

    def test_repeated_tick_skips_full_player_read(self):
        reader = Reader(ROOT / "profiles/game.json")
        class ProcessStub:
            def __init__(self): self.reads = 0
            def alive(self): return True
            def read(self, *_): self.reads += 1; raise AssertionError("unchanged tick must not read body")
        reader.process = ProcessStub()
        marker = (0x100000, 123, 2, 0x100000, 4.1)
        reader.last_marker = marker
        reader.last_full_read = time.monotonic()
        reader._marker = lambda: marker
        self.assertIsNone(reader.sample(only_changed=True))
        self.assertEqual(reader.process.reads, 0)

    def test_frozen_markers_still_recheck_incap_field(self):
        reader = Reader(ROOT / "profiles/game.json")
        marker = (0x100000, 123, 2, 0x100000, 4.1)
        reader.last_marker = marker
        reader.last_full_read = time.monotonic() - 0.2
        reader._marker = lambda: marker
        reader.identity = "test"
        data = bytearray(reader.end - reader.start)
        for name, value in (("health", 300), ("team", 2), ("max_health", 100), ("incap", 1)):
            start, _, kind = reader.field_bounds[name]
            struct.pack_into("<B" if kind == "u8" else "<i", data, start - reader.start, value)
        class Memory:
            def alive(self): return True
            def read(self, address, size): return bytes(data)
        reader.process = Memory()
        snapshot = reader.sample(only_changed=True)
        self.assertTrue(snapshot.incap)
        self.assertEqual(snapshot.health, 300)

    def test_shared_root_pointer_read_once(self):
        class Memory:
            pointer_size = 4
            def __init__(self): self.pointers = 0
            def pointer(self, address): self.pointers += 1; return 0x200000
            def read(self, address, size): return struct.pack("<i", 123)
        memory = Memory()
        def root(add=0, kind="i32"):
            result = Address.__new__(Address)
            result.process, result.base = memory, 0x100000
            result.spec = {"chain": [0], "add": add, "type": kind}
            return result
        cache = {}
        self.assertEqual(root().resolve(cache), 0x200000)
        self.assertEqual(root(4).value(cache), 123)
        self.assertEqual(root(8).value(cache), 123)
        self.assertEqual(memory.pointers, 1)


class SamplingTests(unittest.TestCase):
    def test_waiting_game_does_not_busy_poll_or_repeat_notifications(self):
        messages = []
        called = threading.Event()
        class MissingGame:
            process = None
            def __init__(self, *_): pass
            def sample(self, **_):
                called.set()
                raise ReadError("等待游戏进程")
            def close(self): pass
        sampler = Sampler(lambda *value: messages.append(value), ROOT, defaults(), 1, MissingGame)
        sampler.start()
        self.assertTrue(called.wait(1))
        time.sleep(0.05)
        sampler.stop(); sampler.join()
        self.assertEqual(sampler.polls, 1)
        self.assertEqual(len(messages), 1)

    def test_stopped_worker_releases_reader(self):
        closed, called = threading.Event(), threading.Event()
        class Game:
            process = None
            last_marker = None
            def __init__(self, *_): pass
            def sample(self, **_): called.set(); return None
            def close(self): closed.set()
        sampler = Sampler(lambda *_: None, ROOT, defaults(), 1, Game)
        sampler.start(); self.assertTrue(called.wait(1))
        sampler.stop(); sampler.join()
        self.assertTrue(closed.is_set())


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_device_info_is_not_ignored(self):
        class Socket:
            def __aiter__(self): return self
            async def __anext__(self):
                if hasattr(self, "done"): raise StopAsyncIteration
                self.done = True
                return '{"op":"device_info","connected":false}'
            async def send(self, _): pass
        bridge = Bridge(Socket(), {})
        await bridge.receive()
        self.assertIs(bridge.device_connected, False)

    async def test_headline_distinguishes_sampling_and_disconnected_output(self):
        bridge = Bridge(None, {})
        bridge.config["observe_only"] = False
        bridge.device_connected = False
        bridge.snapshot = replace(snap(), health=77)
        self.assertIn("77 HP", bridge.display_status())
        self.assertIn("输出设备未连接", bridge.display_status())
        bridge.device_connected = True
        self.assertNotIn("输出设备未连接", bridge.display_status())
    async def test_unchanged_status_is_not_sent_every_refresh(self):
        sent = []
        class Socket:
            async def send(self, value): sent.append(json.loads(value))
        bridge = Bridge(Socket(), {})
        bridge.config["status_hz"] = 10
        task = asyncio.create_task(bridge.status_loop())
        await asyncio.sleep(0.25)
        bridge.stopped.set()
        await task
        self.assertEqual(len(sent), 1)

    async def test_overlay_expires_without_new_game_tick(self):
        sent = []
        class Socket:
            async def send(self, value): sent.append(json.loads(value))
        bridge = Bridge(Socket(), {})
        bridge.config.update(observe_only=False, hurt_strength=40, hurt_duration=0.05)
        bridge.output.update(bridge.config)
        bridge.snapshot = snap()
        await bridge.emit(bridge.output.render(bridge.snapshot, [Edge("hurt", 1)], time.monotonic()))
        task = asyncio.create_task(bridge.expiry_loop())
        await asyncio.sleep(0.1)
        bridge.stopped.set(); bridge.output_changed.set()
        await task
        self.assertEqual(bridge.output.last_strength, {"a": 0, "b": 0})
    async def test_websocket_handshake_ping_config_stop_no_output_in_observe(self):
        import websockets
        import main as entry
        observed = []
        completed = asyncio.Event()
        async def server(ws):
            observed.append(json.loads(await ws.recv()))
            await ws.send(json.dumps({"op": "hello_ack", "accepted": True}))
            await ws.send(json.dumps({"op": "config", "data": {"observe_only": True, "profile_path": "not-present.json"}}))
            await ws.send(json.dumps({"op": "ping", "t": 123}))
            while True:
                value = json.loads(await ws.recv())
                observed.append(value)
                if value.get("op") == "pong": break
            await ws.send(json.dumps({"op": "stop"}))
            await ws.wait_closed()
            completed.set()
        async with websockets.serve(server, "127.0.0.1", 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            with patch.dict(os.environ, {"DGHUB_HOST": "127.0.0.1", "DGHUB_PORT": str(port), "DGHUB_TOKEN": "test-secret"}):
                await asyncio.wait_for(entry.main(), 5)
            await asyncio.wait_for(completed.wait(), 5)
        self.assertEqual(observed[0]["op"], "hello")
        self.assertEqual(observed[0]["manifest"]["sdk"], "1")
        self.assertTrue(any(value["op"] == "pong" and value["t"] == 123 for value in observed))
        self.assertFalse(any(value["op"] in ("trigger", "set_strength", "pulse") for value in observed))

    async def test_stop_before_config_exits(self):
        class FakeSocket:
            def __aiter__(self): return self
            async def __anext__(self):
                if hasattr(self, "sent_stop"): raise StopAsyncIteration
                self.sent_stop = True
                return '{"op":"stop"}'
            async def send(self, message): pass
        await asyncio.wait_for(Bridge(FakeSocket(), {}).run(), 1)

    async def test_invalid_config_does_not_replace_good_config(self):
        class FakeSocket:
            async def send(self, message): pass
        bridge = Bridge(FakeSocket(), {})
        await bridge.configure({"hp_scale": 2})
        with self.assertRaises(ValueError): await bridge.configure({"hp_scale": math.inf})
        self.assertEqual(bridge.config["hp_scale"], 2)


if __name__ == "__main__": unittest.main()
