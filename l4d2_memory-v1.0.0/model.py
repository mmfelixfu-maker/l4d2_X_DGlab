"""Snapshots, edge events and a DGHub-independent output state machine."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math


EVENTS = {
    "hurt": ("💔 受伤", "有效生命值下降；临时血量自然衰减不计入。"),
    "incap": ("🩸 倒地", "进入倒地状态，仅在开始时触发。"),
    "ledge": ("🧗 挂边", "进入挂边状态，与普通倒地区分。"),
    "smoker": ("🪢 Smoker 控制", "有效舌头拥有者句柄出现。"),
    "hunter": ("🐾 Hunter 扑倒", "有效扑倒攻击者句柄出现。"),
    "jockey": ("🎭 Jockey 骑乘", "有效骑乘攻击者句柄出现。"),
    "charger_carry": ("🐂 Charger 携带", "进入 Charger 携带阶段。"),
    "charger_pummel": ("🔨 Charger 砸地", "进入 Charger 砸地阶段。"),
    "death": ("💀 死亡", "存活变为死亡。"),
    "released": ("🕊 脱离控制", "所有特感控制关系解除；控制阶段切换不算脱离。"),
}
CONTROLS = ("smoker", "hunter", "jockey", "charger_carry", "charger_pummel")


@dataclass(frozen=True)
class Snapshot:
    session: str
    player: int
    tick: int
    game_time: float
    health: int
    max_health: int = 100
    team: int = 2
    life_state: int = 0
    incap: bool = False
    ledge: bool = False
    black_white: bool = False
    controls: tuple[str, ...] = ()
    temp_buffer: float = 0.0
    temp_time: float = 0.0
    temp_decay: float = 0.27
    supported: tuple[str, ...] = ()

    @property
    def alive(self):
        return self.life_state == 0

    def temp_at(self, game_time):
        return max(0.0, self.temp_buffer - max(0.0, game_time - self.temp_time) * self.temp_decay)

    @property
    def effective_health(self):
        return max(0.0, self.health) + self.temp_at(self.game_time)

    def public(self):
        result = asdict(self)
        result.update(alive=self.alive, effective_health=round(self.effective_health, 2))
        return result


@dataclass(frozen=True)
class Edge:
    name: str
    amount: float = 0.0


class Tracker:
    def __init__(self):
        self.previous = None
        self.missed_ticks = 0
        self.last_reset = "首次采样"

    def reset(self, reason="数据不可用"):
        self.previous = None
        self.last_reset = reason

    def observe(self, current: Snapshot):
        previous = self.previous
        if current.team != 2:
            self.reset("当前不是生还者")
            return [], True
        if previous is None or (current.session, current.player) != (previous.session, previous.player):
            self.previous = current
            self.last_reset = "建立新会话基线"
            return [], True
        # Prediction/simulation clocks may rewind as the player dies. For the
        # same entity, process its life-state transition before a clock rebase.
        if previous.alive and not current.alive:
            self.previous = current
            loss = previous.health + previous.temp_at(current.game_time) if not (previous.incap or previous.ledge) else 0
            return [Edge("death", max(0, loss))], False
        if current.tick < previous.tick or current.game_time < previous.game_time:
            self.previous = current
            self.last_reset = "游戏时钟回退，重建基线"
            return [], True
        if current == previous:
            return [], False
        self.missed_ticks += max(0, current.tick - previous.tick - 1)
        self.previous = current
        edges = []
        if not previous.alive and current.alive:
            return [Edge("respawn")], False
        if not current.alive:
            return [], False
        if current.ledge and not previous.ledge:
            edges.append(Edge("ledge"))
        if current.incap and not current.ledge and not previous.incap:
            loss = previous.health + previous.temp_at(current.game_time) if not previous.ledge else 0
            edges.append(Edge("incap", max(0, loss)))
        if (previous.incap or previous.ledge) and not (current.incap or current.ledge):
            edges.append(Edge("revive"))
        for name in CONTROLS:
            if name in current.controls and name not in previous.controls:
                edges.append(Edge(name))
        if previous.controls and not current.controls:
            edges.append(Edge("released"))
        if current.black_white and not previous.black_white:
            edges.append(Edge("black_white"))
        # Incap, ledge and death have different health pools. Never compare them
        # to standing health or interpret their pool reset as damage/healing.
        if not (previous.incap or current.incap or previous.ledge or current.ledge):
            expected_previous = previous.health + previous.temp_at(current.game_time)
            difference = expected_previous - current.effective_health
            if difference >= 0.75:
                edges.append(Edge("hurt", difference))
            elif difference <= -0.75:
                edges.append(Edge("heal", -difference))
        return edges, False


def defaults():
    result = {
        "profile_path": "profiles/game.json", "observe_only": True,
        "poll_hz": 120, "stale_s": 1.5, "status_hz": 1,
        "mode": "dynamic", "hp_scale": 1.0, "strength_cap": 60,
        "fixed_strength": 20, "channel": "both", "temp_decay": 0.27,
        "incap_sustain": True,
    }
    for name in EVENTS:
        result.update({
            f"{name}_enabled": True, f"{name}_preset": "",
            f"{name}_duration": 3.0 if name == "death" else 1.0,
            f"{name}_channel": "inherit", f"{name}_strength": 0,
            f"{name}_cooldown": 0.15,
        })
    return result


def checked_config(values):
    """Only declared keys and finite, bounded numeric values reach the wire."""
    base = defaults()
    if not isinstance(values, dict):
        raise ValueError("配置必须是对象")
    for key, value in values.items():
        if key not in base:
            continue
        original = base[key]
        if isinstance(original, bool):
            if not isinstance(value, bool):
                raise ValueError(f"{key} 必须是开关")
        elif isinstance(original, (int, float)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{key} 必须是有限数值")
            low, high = 0, 100
            if key == "poll_hz": low, high = 10, 500
            elif key == "status_hz": low, high = 0.2, 10
            elif key == "stale_s": low, high = 0.2, 10
            elif key == "temp_decay": low, high = 0, 10
            elif key == "hp_scale": low, high = 0, 10
            elif key.endswith("_duration"): low, high = 0.05, 300
            elif key.endswith("_cooldown"): low, high = 0, 60
            if not low <= value <= high:
                raise ValueError(f"{key} 范围是 {low}～{high}")
        elif not isinstance(value, str) or len(value) > 1024:
            raise ValueError(f"{key} 必须是文本")
        base[key] = value
    if base["mode"] not in ("dynamic", "fixed", "challenge"):
        raise ValueError("未知强度模式")
    if base["channel"] not in ("a", "b", "both"):
        raise ValueError("未知通道")
    for name in EVENTS:
        if base[f"{name}_channel"] not in ("inherit", "a", "b", "both"):
            raise ValueError(f"{name} 通道无效")
    return base


class Output:
    """Own absolute layer + independent waveform triggers, per SDK section 6."""
    def __init__(self, config=None):
        self.config = checked_config(config or {})
        self.reset()

    def reset(self):
        self.challenge = 0.0
        self.death_until = 0.0
        self.death_hold = 0
        self.overlays = {}
        self.last_event = {}
        self.last_strength = None
        self.sustain_due = None

    def update(self, config):
        candidate = checked_config(config)
        self.config = candidate
        self.reset()

    def clear(self):
        self.reset()
        self.last_strength = {"a": 0, "b": 0}
        return [{"op": "set_strength", "channel": "both", "pct": 0}]

    def next_deadline(self, now):
        deadlines = [value[0] for value in self.overlays.values()]
        if self.death_until > 0: deadlines.append(self.death_until)
        if self.sustain_due is not None: deadlines.append(self.sustain_due)
        return min(deadlines) if deadlines else None

    def render(self, snapshot, edges, now, fresh=False):
        cfg = self.config
        if fresh:
            self.reset()
        if self.death_until and now >= self.death_until:
            self.death_until = 0
            self.death_hold = 0
        messages = []
        if snapshot is None or snapshot.team != 2:
            return self.clear()
        if any(edge.name in ("revive", "respawn") for edge in edges):
            self.death_until = 0
            self.death_hold = 0
            self.overlays.clear()
            if any(edge.name == "respawn" for edge in edges):
                self.challenge = 0
        # Challenge follows cumulative observed loss, independent of whether the
        # hurt waveform is enabled. Dynamic mode follows current missing HP.
        for edge in edges:
            if edge.name in ("hurt", "incap", "death"):
                self.challenge = min(cfg["strength_cap"], self.challenge + edge.amount * cfg["hp_scale"])
        if fresh and snapshot.alive and snapshot.incap and not snapshot.ledge and cfg["mode"] == "challenge":
            self.challenge = min(cfg["strength_cap"], snapshot.max_health * cfg["hp_scale"])
        sustained = bool(cfg["incap_sustain"] and cfg["incap_enabled"] and snapshot.alive
                         and snapshot.incap and not snapshot.ledge)
        if not sustained: self.sustain_due = None
        dynamic = min(cfg["strength_cap"], max(0, snapshot.max_health - snapshot.effective_health) * cfg["hp_scale"])
        normal = {"dynamic": dynamic, "fixed": cfg["fixed_strength"], "challenge": self.challenge}[cfg["mode"]]
        if snapshot.incap or snapshot.ledge:
            normal = cfg["strength_cap"] if cfg["mode"] == "dynamic" else normal
        for edge in edges:
            name = edge.name
            if name not in EVENTS:
                continue  # Internal lifecycle changes reset state without feedback.
            if not cfg[f"{name}_enabled"]:
                continue
            if now - self.last_event.get(name, -math.inf) < cfg[f"{name}_cooldown"]:
                continue
            self.last_event[name] = now
            duration = cfg[f"{name}_duration"]
            channel = cfg[f"{name}_channel"]
            if channel == "inherit": channel = cfg["channel"]
            label = EVENTS[name][0]
            if edge.amount: label += f" {edge.amount:.1f} HP"
            if name == "death":
                self.overlays.clear()
                self.death_until = now + duration
                self.death_hold = max((self.last_strength or {}).values(), default=round(normal))
            strength = cfg[f"{name}_strength"]
            if strength and not (name == "incap" and sustained):
                self.overlays[name] = (now + duration, channel, strength)
            preset = cfg[f"{name}_preset"].strip()
            if name == "incap" and sustained:
                messages.append({"op": "event", "label": label, "name": label, "duration": duration})
            elif preset:
                messages.append({"op": "trigger", "action": "waveform", "preset": preset,
                                 "duration_s": duration, "channel": channel, "label": label})
            else:
                messages.append({"op": "event", "label": label, "name": label, "duration": duration})
        if sustained and cfg["incap_preset"].strip() and (self.sustain_due is None or now >= self.sustain_due):
            # Short waveform leases bound the tail after revive/death. No queue
            # of long effects is accumulated; stop renewing when state ends.
            lease = min(0.5, cfg["incap_duration"])
            channel = cfg["incap_channel"]
            if channel == "inherit": channel = cfg["channel"]
            messages.append({"op": "trigger", "action": "waveform", "preset": cfg["incap_preset"].strip(),
                             "duration_s": lease, "channel": channel, "label": "倒地持续反馈"})
            self.sustain_due = now + max(0.04, lease * 0.75)
        if not snapshot.alive:
            if cfg["mode"] == "dynamic":
                normal = self.death_hold if now < self.death_until else 0
            elif cfg["mode"] == "challenge":
                normal = self.challenge
        target = {"a": 0, "b": 0}
        for channel in ("a", "b"):
            if cfg["channel"] in (channel, "both"):
                target[channel] = normal
        self.overlays = {key: value for key, value in self.overlays.items() if value[0] > now}
        for _, channel, strength in self.overlays.values():
            for out_channel in ("a", "b"):
                if channel in (out_channel, "both"):
                    target[out_channel] = max(target[out_channel], strength)
        if sustained and cfg["incap_strength"]:
            channel = cfg["incap_channel"]
            if channel == "inherit": channel = cfg["channel"]
            for out_channel in ("a", "b"):
                if channel in (out_channel, "both"):
                    target[out_channel] = max(target[out_channel], cfg["incap_strength"])
        target = {key: round(min(cfg["strength_cap"], max(0, value))) for key, value in target.items()}
        # Send the absolute layer before the waveform; do not combine a
        # rollback lease with per-tick absolute strength writes.
        changed = []
        for channel in ("a", "b"):
            if self.last_strength is None or target[channel] != self.last_strength[channel]:
                changed.append({"op": "set_strength", "channel": channel, "pct": target[channel]})
        self.last_strength = target
        return changed + messages
