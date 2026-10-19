"""DGHub SDK v1 external plugin. The host runs this without a new console."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import time
from urllib.parse import quote

from model import EVENTS, Output, Tracker, checked_config, defaults
from native import ReadError
from reader import ProfileError, Reader, TickChanged
from sampling import Sampler

ROOT = Path(__file__).resolve().parent


class Bridge:
    def __init__(self, ws, manifest):
        self.ws, self.manifest = ws, manifest
        self.config = defaults()
        self.tracker = Tracker()
        self.output = Output(self.config)
        self.reader = None
        self.ready = asyncio.Event()
        self.stopped = asyncio.Event()
        self.revision = 0
        self.snapshot = None
        self.detail = "等待 DGHub 初始配置"
        self.check_state = "pending"
        self.last_sample = 0.0
        self.last_tick = None
        self.valid_samples = 0
        self.discarded = 0
        self.events_seen = []
        self.sample_queue = asyncio.Queue(maxsize=64)
        self.sampler = None
        self.output_changed = asyncio.Event()
        self.dropped_snapshots = 0
        self.device_connected = None
        self.last_event_at = None

    async def send(self, message):
        await self.ws.send(json.dumps(message, ensure_ascii=False, allow_nan=False))

    async def configure(self, values, replace=False):
        candidate = checked_config(values if replace else {**self.config, **values})
        # Once configured, immediately clear the old channels before adopting
        # a different mode/channel/profile or resuming actual hardware output.
        if self.ready.is_set() and not self.config["observe_only"]:
            await self.send({"op": "set_strength", "channel": "both", "pct": 0})
        self.config = candidate
        self.output.update(candidate)
        self.tracker.reset("配置更新")
        self.snapshot = None
        self.revision += 1
        self.ready.set()
        self.output_changed.set()
        if self.sampler:
            self.sampler.configure(candidate, self.revision)

    async def receive(self):
        async for raw in self.ws:
            try:
                message = json.loads(raw)
                if not isinstance(message, dict): continue
                op = message.get("op")
                if op == "stop":
                    self.stopped.set()
                    return
                if op == "ping":
                    await self.send({"op": "pong", "t": message.get("t")})
                elif op == "config":
                    await self.configure(message.get("data", {}), replace=True)
                elif op == "config_changed":
                    await self.configure({message.get("key"): message.get("value")})
                elif op == "device_info":
                    if isinstance(message.get("connected"), bool):
                        self.device_connected = message["connected"]
                # Device state belongs to the host; percentages remain 0–100.
            except (ValueError, TypeError) as exc:
                await self.send({"op": "log", "level": "warning", "message": f"配置或消息未采用：{exc}"})
        self.stopped.set()

    async def emit(self, messages):
        if self.config["observe_only"]:
            return
        for message in messages:
            await self.send(message)

    async def invalidate(self, detail, state):
        if self.snapshot is not None:
            await self.emit(self.output.clear())
        self.snapshot = None
        self.tracker.reset(detail)
        self.last_tick = None
        self.detail, self.check_state = detail, state
        self.output_changed.set()

    async def sample_loop(self):
        await self.ready.wait()
        if self.stopped.is_set(): return
        loop = asyncio.get_running_loop()
        def deliver(kind, revision, payload):
            if self.stopped.is_set(): return
            if self.sample_queue.full():
                # Don't interpret a gap in a saturated queue as one huge hit.
                self.dropped_snapshots += self.sample_queue.qsize()
                while not self.sample_queue.empty(): self.sample_queue.get_nowait()
                self.sample_queue.put_nowait(("overflow", revision, None))
            self.sample_queue.put_nowait((kind, revision, payload))
        def notify(kind, revision, payload):
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(deliver, kind, revision, payload)
        self.sampler = Sampler(notify, ROOT, self.config, self.revision)
        self.sampler.start()
        try:
            while not self.stopped.is_set():
                kind, revision, payload = await self.sample_queue.get()
                if kind == "stop": return
                if revision != self.revision: continue
                self.discarded = self.sampler.discarded
                if kind == "fatal": raise RuntimeError(f"采集线程退出：{payload}")
                if kind == "error":
                    await self.invalidate(*payload)
                    continue
                if kind == "overflow":
                    await self.invalidate("采集队列过载，重新建立基线", "warn")
                    continue
                snapshot = payload
                if snapshot.team != 2:
                    await self.invalidate("当前不是生还者，已暂停输出", "pending")
                    continue
                self.last_tick = (snapshot.session, snapshot.player, snapshot.tick)
                self.last_sample = time.monotonic()
                self.valid_samples += 1
                edges, fresh = self.tracker.observe(snapshot)
                self.snapshot = snapshot
                if edges:
                    self.events_seen = [edge.name for edge in edges if edge.name in EVENTS]
                    self.last_event_at = self.last_sample
                await self.emit(self.output.render(snapshot, edges, self.last_sample, fresh=fresh))
                self.detail = "观察模式：只显示状态" if self.config["observe_only"] else "正在采集本地玩家状态"
                self.check_state = "ok"
                self.output_changed.set()
        finally:
            self.sampler.stop()
            await asyncio.to_thread(self.sampler.join)

    async def expiry_loop(self):
        while not self.stopped.is_set():
            self.output_changed.clear()
            deadline = self.output.next_deadline(time.monotonic()) if self.snapshot else None
            if deadline is None:
                await self.output_changed.wait()
                continue
            try:
                await asyncio.wait_for(self.output_changed.wait(), max(0.001, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                if self.snapshot:
                    await self.emit(self.output.render(self.snapshot, [], time.monotonic()))

    async def status_loop(self):
        last_key, last_sent = None, -float("inf")
        while not self.stopped.is_set():
            now = time.monotonic()
            player = self.snapshot
            key = (self.detail, self.check_state, self.config["observe_only"], self.revision,
                   self.device_connected, self.last_event_at,
                   (player.session, player.player, player.health, player.max_health, player.team,
                    player.life_state, player.incap, player.ledge, player.black_white, player.controls,
                    round(player.effective_health, 1)) if player else None)
            fields = {
                "display_status": self.display_status(), "observe_only": self.config["observe_only"],
                "valid_samples": self.valid_samples, "missed_ticks": self.tracker.missed_ticks,
                "discarded_snapshots": self.discarded, "last_events": self.events_seen,
                "player": self.snapshot.public() if self.snapshot else None,
                "dropped_snapshots": self.dropped_snapshots,
                "marker_polls": self.sampler.polls if self.sampler else 0,
                "full_snapshots": self.sampler.full_snapshots if self.sampler else 0,
                "output_ready": self.device_connected,
                "output_warning": "DGHub 当前输出目标未连接，请在主程序确认输出设备" if self.device_connected is False else "",
                "last_event_at": self.last_event_at,
                "startup_check": {"title": "L4D2 状态采集检查", "steps": [
                    {"key": "bridge", "title": "DGHub 桥接", "state": "ok", "detail": "已连接 SDK v1"},
                    {"key": "source", "title": "游戏地址与玩家数据", "state": self.check_state,
                     "detail": self.detail, "hint": "地址必须匹配当前 client.dll 和 engine.dll 版本"},
                ]},
            }
            # Tick/time/counters alone don't redraw the host UI. A quiet state
            # gets a telemetry heartbeat at most once every fifteen seconds.
            if key != last_key or now - last_sent >= 15:
                await self.send({"op": "status", "fields": fields})
                last_key, last_sent = key, now
            try:
                await asyncio.wait_for(self.stopped.wait(), 1 / self.config["status_hz"])
            except asyncio.TimeoutError:
                pass

    def display_status(self):
        if self.snapshot is None:
            return self.detail
        player = self.snapshot
        if not player.alive: label = "死亡"
        elif player.ledge: label = "挂边"
        elif player.incap: label = "倒地"
        else: label = "存活"
        if player.controls:
            label += " / " + "、".join(player.controls)
        label += f" · 实血 {player.health} HP"
        temp = player.temp_at(player.game_time)
        if temp >= 0.1: label += f" + 临时 {temp:.1f} HP"
        if self.config["observe_only"]: label += " · 仅观察"
        elif self.device_connected is False: label += " · 输出设备未连接"
        return label

    async def run(self):
        tasks = [asyncio.create_task(self.receive()), asyncio.create_task(self.sample_loop()),
                 asyncio.create_task(self.status_loop()), asyncio.create_task(self.expiry_loop())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done: task.result()
        finally:
            self.stopped.set()
            self.ready.set()  # stop can arrive before the initial config
            self.output_changed.set()
            if self.sampler: self.sampler.stop()
            if self.sample_queue.full(): self.sample_queue.get_nowait()
            self.sample_queue.put_nowait(("stop", self.revision, None))
            # Do not cancel an in-flight to_thread read and then close its
            # handle. Signal the sampler and allow the read to finish first.
            for task in tasks:
                if task is not tasks[1]: task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with suppress(Exception):
                if not self.config["observe_only"]:
                    await self.send({"op": "set_strength", "channel": "both", "pct": 0})
            if self.reader: self.reader.close()


async def main():
    import websockets
    host, port, token = (os.environ.get(key, "") for key in ("DGHUB_HOST", "DGHUB_PORT", "DGHUB_TOKEN"))
    if not host or not port or not token:
        raise SystemExit("请通过 DGHub 启动；离线检查使用 diagnose.py，模拟使用 demo.py")
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    # Never log the URL: it contains the session token.
    uri = f"ws://{host}:{port}/ws/plugin?token={quote(token, safe='')}"
    async with websockets.connect(uri, open_timeout=10, close_timeout=3, max_size=1_048_576) as ws:
        await ws.send(json.dumps({"op": "hello", "token": token, "manifest": manifest}, ensure_ascii=False))
        ack = json.loads(await asyncio.wait_for(ws.recv(), 10))
        if ack.get("op") != "hello_ack" or ack.get("accepted") is not True:
            raise RuntimeError("DGHub 未接受插件握手")
        await Bridge(ws, manifest).run()


if __name__ == "__main__":
    asyncio.run(main())
