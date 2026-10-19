"""Read-only diagnostics; does not connect to DGHub or control a device."""
import argparse
import json
from pathlib import Path
import time

from native import Process, process_ids
from reader import Reader, TickChanged


def inspect():
    pids = process_ids("left4dead2.exe")
    if not pids:
        return {"status": "waiting", "detail": "游戏进程尚未启动"}
    output = []
    for pid in pids:
        with Process(pid) as process:
            modules = []
            for name in ("left4dead2.exe", "client.dll", "engine.dll", "server.dll"):
                module = process.modules.get(name)
                if module:
                    modules.append({"name": name, "base": hex(module.base), "size": module.size,
                                    "path": module.path, "sha256": module.fingerprint()})
            output.append({"pid": pid, "pointer_size": process.pointer_size, "modules": modules})
    return {"processes": output}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="保存模块指纹 JSON")
    parser.add_argument("--profile", help="验证地址文件并显示玩家快照")
    parser.add_argument("--seconds", type=float, default=10)
    args = parser.parse_args()
    if args.profile:
        reader = Reader(args.profile)
        try:
            until = time.monotonic() + args.seconds
            while time.monotonic() < until:
                try:
                    print(json.dumps(reader.sample().public(), ensure_ascii=False))
                except TickChanged:
                    pass
                time.sleep(0.1)
        finally:
            reader.close()
    else:
        result = json.dumps(inspect(), ensure_ascii=False, indent=2)
        print(result)
        if args.output: Path(args.output).write_text(result, encoding="utf-8")


if __name__ == "__main__":
    main()
