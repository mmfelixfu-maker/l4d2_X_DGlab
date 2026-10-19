"""Local memory fixture benchmark: real ReadProcessMemory, simulated 30Hz ticks.

No game or device is touched. --source can select an archived plugin version.
"""
import argparse
import asyncio
import ctypes
import json
from pathlib import Path
import os
import struct
import sys
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--source", required=True)
parser.add_argument("--profile", required=True)
parser.add_argument("--seconds", type=float, default=4)
parser.add_argument("--output")
args = parser.parse_args()
sys.path.insert(0, str(Path(args.source).resolve()))
import main as bridge_module
from native import Address, Process, integer
from reader import Reader


async def benchmark():
    body = ctypes.create_string_buffer(0x2800)
    body_address = ctypes.addressof(body)
    pointer = ctypes.c_void_p(body_address)
    pointer_address = ctypes.addressof(pointer)
    origin = time.perf_counter()
    class CountingProcess(Process):
        def __init__(self):
            self.reads = self.bytes = 0
            super().__init__(os.getpid())
        def alive(self):
            tick = int((time.perf_counter() - origin) * 30) + 1
            struct.pack_into("<i", body, 0x14cc, tick)
            struct.pack_into("<f", body, 0x150, tick / 30)
            return super().alive()
        def read(self, address, size):
            self.reads += 1; self.bytes += size
            return super().read(address, size)
    process = CountingProcess()
    reader = Reader(args.profile)
    reader.process, reader.identity = process, "benchmark"
    struct.pack_into("<i", body, 0xec, 93)
    struct.pack_into("<i", body, 0xe4, 2)
    struct.pack_into("<i", body, 0x1fdc, 100)
    reader.entities = None
    reader.roots = {}
    for name, spec in reader.profile["roots"].items():
        address = Address.__new__(Address)
        address.process, address.base, address.spec = process, pointer_address, spec
        reader.roots[name] = address
    def factory(*_): return reader
    if hasattr(bridge_module, "Sampler"):
        from sampling import Sampler
        bridge_module.Sampler = lambda notify, root, config, revision: Sampler(notify, root, config, revision, factory)
    else:
        bridge_module.Reader = factory
    sent = []
    class Socket:
        async def send(self, value): sent.append(json.loads(value)["op"])
    bridge = bridge_module.Bridge(Socket(), {})
    await bridge.configure({"observe_only": True, "poll_hz": 120, "status_hz": 2}, replace=True)
    tasks = [asyncio.create_task(bridge.sample_loop()), asyncio.create_task(bridge.status_loop())]
    if hasattr(bridge, "expiry_loop"): tasks.append(asyncio.create_task(bridge.expiry_loop()))
    start, cpu_start = time.perf_counter(), time.process_time()
    try:
        await asyncio.sleep(args.seconds)
    finally:
        bridge.stopped.set(); bridge.ready.set()
        if hasattr(bridge, "sample_queue"):
            if bridge.sampler: bridge.sampler.stop()
            bridge.sample_queue.put_nowait(("stop", bridge.revision, None))
            bridge.output_changed.set()
        await asyncio.gather(*tasks)
        cpu, elapsed = time.process_time() - cpu_start, time.perf_counter() - start
        result = {"source": args.source, "fixture": "own-process memory / simulated 30 Hz", "seconds": round(elapsed, 3),
                  "cpu_seconds": round(cpu, 6), "cpu_percent_machine": round(100 * cpu / elapsed / os.cpu_count(), 4),
                  "memory_reads": process.reads, "memory_bytes": process.bytes,
                  "new_ticks": bridge.valid_samples, "status_messages": len(sent),
                  "missed_ticks": bridge.tracker.missed_ticks}
        reader.close()
    return result


result = asyncio.run(benchmark())
text = json.dumps(result, ensure_ascii=False, indent=2)
print(text)
if args.output: Path(args.output).write_text(text, encoding="utf-8")
