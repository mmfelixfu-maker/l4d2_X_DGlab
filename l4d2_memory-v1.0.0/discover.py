"""Read client RecvTables from an existing process; never executes game code."""
from __future__ import annotations

import json
from pathlib import Path
import struct

from native import Process, ReadError, process_ids


def image(process, module):
    result = bytearray(module.size)
    for start in range(0, module.size, 0x10000):
        size = min(0x10000, module.size - start)
        try:
            result[start:start + size] = process.read(module.base + start, size)
        except ReadError:
            for page in range(start, start + size, 0x1000):
                length = min(0x1000, module.size - page)
                try: result[page:page + length] = process.read(module.base + page, length)
                except ReadError: pass
    return bytes(result)


def positions(data, needle):
    start = 0
    while True:
        found = data.find(needle, start)
        if found < 0: return
        yield found
        start = found + 1


def recv_tables(process, module, data):
    if process.pointer_size != 4:
        raise ValueError("当前自动属性发现只适配 x86 RecvTable 布局")

    def u32(address):
        rva = address - module.base
        if not 0 <= rva <= len(data) - 4: raise ValueError("指针不在 client.dll 中")
        return struct.unpack_from("<I", data, rva)[0]

    def string(address):
        rva = address - module.base
        if not 0 <= rva < len(data): raise ValueError("字符串不在 client.dll 中")
        stop = data.find(b"\0", rva, rva + 256)
        if stop < 0: raise ValueError("未终止的属性名")
        return data[rva:stop].decode("ascii")

    def walk(table, inherited=0, stack=()):
        if len(stack) > 32 or table in stack: return {}
        props, count = u32(table), u32(table + 4)
        name = string(u32(table + 12))
        if not name.startswith("DT_") or not 0 < count <= 4096:
            raise ValueError("不是有效属性表")
        output = {}
        for index in range(count):
            prop = props + index * 60
            prop_name = string(u32(prop))
            kind, offset = u32(prop + 4), u32(prop + 44)
            if kind > 7: raise ValueError("属性布局无效")
            if offset == 0xFFFFFFFF: continue  # excluded network property
            if offset > 0x10000: continue
            if kind == 6:
                child = u32(prop + 40)
                if child:
                    try:
                        output.update(walk(child, inherited + offset, (*stack, table)))
                    except (ValueError, UnicodeDecodeError, struct.error):
                        pass  # runtime-created array tables may live on the heap
            elif prop_name and prop_name[0].isalpha():
                output.setdefault(prop_name, inherited + offset)
        return output

    found = {}
    for table_name in (b"DT_TerrorPlayer\0", b"DT_BasePlayer\0"):
        for rva in positions(data, table_name):
            pointer = struct.pack("<I", module.base + rva)
            for reference in positions(data, pointer):
                table = module.base + reference - 12
                try:
                    properties = walk(table)
                    if "m_iHealth" in properties and "m_nTickBase" in properties:
                        found[table_name[:-1].decode()] = properties
                except (ValueError, UnicodeDecodeError, struct.error):
                    continue
    return found


def player_candidates(process, module, data, fields):
    """Candidate discovery only; a plausible number is never auto-approved."""
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    count = struct.unpack_from("<H", data, pe + 6)[0]
    optional = struct.unpack_from("<H", data, pe + 20)[0]
    references = {}
    for index in range(count):
        section = pe + 24 + optional + index * 40
        size, rva = struct.unpack_from("<II", data, section + 8)
        flags = struct.unpack_from("<I", data, section + 36)[0]
        if not flags & 0x80000000: continue
        for offset in range(rva, min(rva + size, len(data)) - 3, 4):
            pointer = struct.unpack_from("<I", data, offset)[0]
            if 0x10000 <= pointer < 0x7FFF0000 and pointer % 4 == 0 and not module.base <= pointer < module.base + module.size:
                references.setdefault(pointer, []).append(offset)
    results = []
    for pointer, refs in references.items():
        try:
            vtable = process.pointer(pointer)
            if not module.base <= vtable < module.base + module.size: continue
            raw = process.read(pointer, max(fields.values()) + 4)
            def i32(name): return struct.unpack_from("<i", raw, fields[name])[0]
            hp, team, tick = i32("m_iHealth"), i32("m_iTeamNum"), i32("m_nTickBase")
            life = raw[fields["m_lifeState"]]
            if not (team == 2 and 0 <= hp <= 10000 and 0 < tick < 100000000 and life <= 4): continue
            # Confirm the IClientNetworkable subobject's GetClientClass getter.
            network_vtable = process.pointer(pointer + 8)
            getter = process.pointer(network_vtable + 1 * 4)
            code = process.read(getter, 6)
            if code[0] != 0xB8 or code[5] != 0xC3: continue
            client_class = struct.unpack_from("<I", code, 1)[0]
            name_ptr = process.pointer(client_class + 8)
            name = process.read(name_ptr, 64).split(b"\0", 1)[0].decode("ascii", errors="replace")
            if name != "CTerrorPlayer": continue
            results.append({"entity": hex(pointer), "health": hp, "team": team, "tick": tick,
                            "life_state": life, "class": name, "pointer_rvas": [hex(value) for value in refs]})
        except (ReadError, struct.error):
            pass
    return results


def main():
    pids = process_ids("left4dead2.exe")
    if len(pids) != 1: raise SystemExit("需要唯一的游戏进程")
    with Process(pids[0]) as process:
        module = process.modules.get("client.dll")
        if module is None: raise SystemExit("client.dll 尚未加载")
        data = image(process, module)
        tables = recv_tables(process, module, data)
        destination = Path(__file__).parent / "discovered-netvars.json"
        destination.write_text(json.dumps(tables, indent=2), encoding="utf-8")
        selected = {key: {name: hex(offset) for name, offset in value.items()
                         if name in ("m_iHealth", "m_iTeamNum", "m_lifeState", "m_nTickBase", "m_iMaxHealth",
                                     "m_isIncapacitated", "m_isHangingFromLedge", "m_bIsOnThirdStrike",
                                     "m_tongueOwner", "m_pounceAttacker", "m_jockeyAttacker", "m_pummelAttacker",
                                     "m_carryAttacker", "m_healthBuffer", "m_healthBufferTime", "m_flSimulationTime")}
                    for key, value in tables.items()}
        print(json.dumps(selected, indent=2))
        if "DT_TerrorPlayer" in tables:
            candidates = player_candidates(process, module, data, tables["DT_TerrorPlayer"])
            print(json.dumps({"player_candidates": candidates}, indent=2))
            (Path(__file__).parent / "player-candidates.json").write_text(json.dumps(candidates, indent=2), encoding="utf-8")


if __name__ == "__main__": main()
