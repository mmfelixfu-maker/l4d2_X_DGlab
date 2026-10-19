"""Materialize the locally inspected 2.2.4.3 profile and a read-only CE table.

Does not infer compatibility from a version string: the expected client DLL
hash below was measured during this session, not copied from a public offset list.
"""
import json
from pathlib import Path
import xml.etree.ElementTree as ET

CLIENT_HASH = "c0a8e1e88f7312db11bcfd4b0ec574d224a34b2d9a615d6088b28335eeb4af1f"
NAMES = {
    "health": ("m_iHealth", "i32", "当前实血"),
    "team": ("m_iTeamNum", "i32", "队伍"),
    "life_state": ("m_lifeState", "u8", "生命状态（0=存活）"),
    "max_health": ("m_iMaxHealth", "i32", "最大实血"),
    "incap": ("m_isIncapacitated", "u8", "倒地"),
    "ledge": ("m_isHangingFromLedge", "u8", "挂边"),
    "black_white": ("m_bIsOnThirdStrike", "u8", "黑白"),
    "smoker": ("m_tongueOwner", "u32", "Smoker 句柄"),
    "hunter": ("m_pounceAttacker", "u32", "Hunter 句柄"),
    "jockey": ("m_jockeyAttacker", "u32", "Jockey 句柄"),
    "charger_carry": ("m_carryAttacker", "u32", "Charger 携带句柄"),
    "charger_pummel": ("m_pummelAttacker", "u32", "Charger 砸地句柄"),
    "temp_buffer": ("m_healthBuffer", "f32", "临时血量缓冲"),
    "temp_time": ("m_healthBufferTime", "f32", "临时血量更新时间"),
}


def main():
    root = Path(__file__).resolve().parent
    variables = json.loads((root / "discovered-netvars.json").read_text())["DT_TerrorPlayer"]
    field = lambda name, kind: {"offset": hex(variables[name]), "type": kind}
    player = lambda add=0, kind="i32": {"module": "client.dll", "rva": "0x726bd8",
                                        "chain": [0], "add": hex(add), "type": kind}
    profile = {
        "schema": 1, "name": "L4D2 2.2.4.3 本机验证配置", "process": "left4dead2.exe",
        "pointer_size": 4, "modules": {"client.dll": CLIENT_HASH},
        "in_game_value": 2,
        "roots": {
            "local_player": player(),
            "tick": player(variables["m_nTickBase"]),
            "in_game": player(variables["m_iTeamNum"]),
            "epoch": {"module": "client.dll", "rva": "0x726bd8", "type": "u32"},
            "game_time": player(variables["m_flSimulationTime"], "f32"),
        },
        "entities": {"address": {"module": "client.dll", "rva": "0x73a564"},
                     "index_bits": 12, "stride": 16, "serial_offset": 4},
        "fields": {key: field(name, kind) for key, (name, kind, _) in NAMES.items()},
        "validation": {
            "verified": ["本地 CTerrorPlayer 实体", "屏幕与内存血量 100→93", "玩家 tick 递增",
                         "RecvTable 字段偏移", "实体句柄索引与序列号布局"],
            "pending": ["重启游戏与换图", "远程服务器", "倒地与挂边", "五类特感控制", "死亡与复活", "临时血量衰减"],
            "tick_source": "本地预测 m_nTickBase；并非完整服务器事件流",
            "readiness": "本地玩家指针、队伍、tick 新鲜度；没有独立的 engine signon 状态",
            "epoch": "本地玩家指针；配合 tick/模拟时间回退检测会话切换",
        },
    }
    (root / "profiles").mkdir(exist_ok=True)
    (root / "profiles" / "game.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    table = ET.Element("CheatTable", CheatEngineTableVersion="26")
    entries = ET.SubElement(table, "CheatEntries")
    for index, (key, (name, kind, label)) in enumerate(NAMES.items()):
        entry = ET.SubElement(entries, "CheatEntry")
        ET.SubElement(entry, "ID").text = str(index)
        ET.SubElement(entry, "Description").text = f'"{label} / {name}"'
        ET.SubElement(entry, "VariableType").text = {"i32": "4 Bytes", "u32": "4 Bytes", "u8": "Byte", "f32": "Float"}[kind]
        if kind == "u32": ET.SubElement(entry, "ShowAsHex").text = "1"
        ET.SubElement(entry, "Address").text = '"client.dll"+726BD8'
        offsets = ET.SubElement(entry, "Offsets")
        ET.SubElement(offsets, "Offset").text = f"{variables[name]:X}"
    ET.indent(table)
    ET.ElementTree(table).write(root / "L4D2-2.2.4.3.CT", encoding="utf-8", xml_declaration=True)


if __name__ == "__main__": main()
