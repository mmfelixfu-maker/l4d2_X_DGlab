"""Version-bound memory profiles and coherent, validated player snapshots."""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import time

from model import CONTROLS, Snapshot
from native import Address, Process, ReadError, TYPES, decode, integer, process_ids


class ProfileError(ValueError):
    pass


class NotReady(ReadError):
    pass


class TickChanged(ReadError):
    pass


class Reader:
    def __init__(self, path, temp_decay=0.27):
        self.process = None
        self.last_marker = None
        self.last_snapshot = None
        self.last_full_read = 0.0
        try:
            self.profile = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except FileNotFoundError as exc:
            raise ProfileError("尚未配置当前游戏版本的地址文件；先运行 diagnose.py 检查") from exc
        if self.profile.get("schema") != 1:
            raise ProfileError("地址文件 schema 必须为 1")
        self.temp_decay = temp_decay
        self.fields = self.profile.get("fields", {})
        for key in ("health", "life_state", "team"):
            if key not in self.fields: raise ProfileError(f"缺少 {key} 字段")
        self.field_bounds = {}
        for key, spec in self.fields.items():
            try:
                offset, kind = integer(spec["offset"]), spec["type"]
                if kind not in TYPES or not 0 <= offset <= 0x10000:
                    raise ValueError("字段偏移或类型无效")
                self.field_bounds[key] = (offset, offset + TYPES[kind][1], kind)
            except (ValueError, TypeError, KeyError) as exc:
                raise ProfileError(f"{key} 偏移尚未配置或格式错误") from exc
        for key in ("health", "team"):
            if self.fields[key]["type"] != "i32": raise ProfileError(f"{key} 必须为 i32")
        if self.fields["life_state"]["type"] != "u8": raise ProfileError("life_state 必须为 u8")
        for key in ("incap", "ledge", "black_white"):
            if key in self.fields and self.fields[key]["type"] != "u8":
                raise ProfileError(f"{key} 必须为 u8")
        for key in CONTROLS:
            if key in self.fields and self.fields[key]["type"] != "u32":
                raise ProfileError(f"{key} 必须为 u32 实体句柄")
        roots = self.profile.get("roots", {})
        for key in ("local_player", "tick", "in_game", "epoch"):
            if key not in roots: raise ProfileError(f"缺少 {key} 定位配置")
        if any(key in self.fields for key in CONTROLS) and "entities" not in self.profile:
            raise ProfileError("控制状态需要实体列表和句柄布局配置")
        if any(key in self.fields for key in ("temp_buffer", "temp_time")):
            if not all(key in self.fields for key in ("temp_buffer", "temp_time")) or "game_time" not in roots:
                raise ProfileError("临时血量需要 buffer、time 和游戏时间三个字段")
        self.start = min(item[0] for item in self.field_bounds.values())
        self.end = max(item[1] for item in self.field_bounds.values())

    def attach(self):
        names = self.profile.get("process", "left4dead2.exe")
        pids = process_ids(names)
        if not pids: raise NotReady("等待游戏进程")
        if len(pids) != 1: raise NotReady("发现多个同名游戏进程，请仅保留一个")
        process = Process(pids[0])
        try:
            if process.pointer_size != integer(self.profile.get("pointer_size")):
                raise ProfileError("游戏进程位数与地址配置不一致")
            fingerprints = self.profile.get("modules", {})
            required_modules = {spec["module"].casefold() for spec in self.profile["roots"].values()}
            if "entities" in self.profile:
                required_modules.add(self.profile["entities"]["address"]["module"].casefold())
            for name in required_modules:
                expected = fingerprints.get(name, "")
                if not isinstance(expected, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", expected):
                    raise ProfileError(f"{name} 尚未填写 SHA-256 版本指纹")
                module = process.modules.get(name)
                if module is None: raise NotReady(f"等待游戏加载 {name}")
                if module.fingerprint().lower() != expected.lower():
                    raise ProfileError(f"{name} 版本指纹不符，停止读取以免误判")
            self.roots = {key: Address(process, spec) for key, spec in self.profile["roots"].items()}
            self.entities = None
            if "entities" in self.profile:
                entity = self.profile["entities"]
                self.entities = Address(process, entity["address"])
                self.index_bits = integer(entity["index_bits"])
                self.stride = integer(entity["stride"])
                self.serial_offset = integer(entity["serial_offset"])
                if not 8 <= self.index_bits <= 16 or not 8 <= self.stride <= 128:
                    raise ProfileError("实体句柄布局无效")
                if not process.pointer_size <= self.serial_offset <= self.stride - 4:
                    raise ProfileError("实体序列号偏移无效")
            self.process = process
            self.identity = f"{pids[0]}:{time.monotonic_ns()}"
        except Exception:
            process.close()
            raise

    def _field(self, data, name, default=0):
        if name not in self.field_bounds: return default
        start, end, kind = self.field_bounds[name]
        return decode(data[start - self.start:end - self.start], kind)

    def _active_handle(self, handle):
        if handle in (0, 0xFFFFFFFF): return False
        if self.entities is None: raise ProfileError("未配置实体列表")
        index = handle & ((1 << self.index_bits) - 1)
        serial = handle >> self.index_bits
        if index == 0: return False
        entry = self.entities.resolve() + index * self.stride
        raw = self.process.read(entry, self.stride)
        pointer = int.from_bytes(raw[:self.process.pointer_size], "little")
        current_serial = int.from_bytes(raw[self.serial_offset:self.serial_offset + 4], "little")
        if pointer < 0x10000 or current_serial != serial: return False
        team_spec = self.fields["team"]
        team = decode(self.process.read(pointer + integer(team_spec["offset"]), 4), "i32")
        life = self.process.read(pointer + integer(self.fields["life_state"]["offset"]), 1)[0]
        return team == 3 and life == 0

    def _marker(self):
        cache = {}
        player = self.roots["local_player"].resolve(cache)
        tick = self.roots["tick"].value(cache)
        in_game = self.roots["in_game"].value(cache)
        epoch = self.roots["epoch"].value(cache)
        simulation_time = self.roots["game_time"].value(cache) if "game_time" in self.roots else None
        return player, tick, in_game, epoch, simulation_time

    def sample(self, only_changed=False):
        if self.process is None: self.attach()
        if not self.process.alive(): raise NotReady("游戏已退出")
        marker = self._marker()
        player, tick, in_game, epoch, simulation_time = marker
        if in_game != self.profile.get("in_game_value", 6): raise NotReady("等待进入对局")
        if not epoch: raise NotReady("当前地图尚未就绪")
        if player < 0x10000: raise NotReady("本地玩家尚未就绪")
        unchanged = marker == self.last_marker
        if only_changed and unchanged and time.monotonic() - self.last_full_read < 0.1:
            return None
        raw = self.process.read(player + self.start, self.end - self.start)
        game_time = simulation_time if simulation_time is not None else float(tick)
        controls = tuple(name for name in CONTROLS if name in self.fields and self._active_handle(self._field(raw, name)))
        # This detects cross-tick reads; it is not a lock on the game's memory.
        if self._marker() != marker:
            raise TickChanged("采样期间游戏更新，丢弃本次快照")
        health, team, life = (self._field(raw, name) for name in ("health", "team", "life_state"))
        maximum = self._field(raw, "max_health", 100)
        buffer, buffer_time = self._field(raw, "temp_buffer"), self._field(raw, "temp_time")
        if not 0 <= health <= 10000 or not 1 <= maximum <= 10000 or team not in (0, 1, 2, 3, 4) or life not in (0, 1, 2, 3, 4):
            raise ReadError("玩家字段校验失败，地址可能失效")
        if not isinstance(tick, int) or tick < 0 or not math.isfinite(game_time) or game_time < 0:
            raise ReadError("游戏时钟校验失败")
        if not all(math.isfinite(value) for value in (buffer, buffer_time)) or not 0 <= buffer <= 10000:
            raise ReadError("临时血量校验失败")
        flags = {}
        for name in ("incap", "ledge", "black_white"):
            value = self._field(raw, name)
            if value not in (0, 1): raise ReadError(f"{name} 标志校验失败")
            flags[name] = bool(value)
        self.last_marker = marker
        self.last_full_read = time.monotonic()
        snapshot = Snapshot(session=f"{self.identity}:{epoch}", player=player, tick=tick, game_time=game_time,
                        health=health, max_health=maximum, team=team, life_state=life,
                        controls=controls, temp_buffer=buffer, temp_time=buffer_time,
                        temp_decay=self.temp_decay, supported=tuple(self.fields), **flags)
        if only_changed and snapshot == self.last_snapshot:
            return None
        self.last_snapshot = snapshot
        return snapshot

    def close(self):
        self.last_marker = None
        self.last_snapshot = None
        self.last_full_read = 0.0
        if self.process is not None:
            self.process.close()
            self.process = None
