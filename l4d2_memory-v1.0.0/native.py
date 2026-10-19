"""Windows process access. Deliberately exposes no write or injection API."""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import struct


class ReadError(RuntimeError):
    pass


@dataclass(frozen=True)
class Module:
    name: str
    base: int
    size: int
    path: str

    def fingerprint(self):
        with open(self.path, "rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest()


class PROCESSENTRY32W(C.Structure):
    _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD), ("th32ProcessID", W.DWORD),
                ("th32DefaultHeapID", C.c_size_t), ("th32ModuleID", W.DWORD),
                ("cntThreads", W.DWORD), ("th32ParentProcessID", W.DWORD),
                ("pcPriClassBase", W.LONG), ("dwFlags", W.DWORD), ("szExeFile", W.WCHAR * 260)]


class MODULEENTRY32W(C.Structure):
    _fields_ = [("dwSize", W.DWORD), ("th32ModuleID", W.DWORD), ("th32ProcessID", W.DWORD),
                ("GlblcntUsage", W.DWORD), ("ProccntUsage", W.DWORD),
                ("modBaseAddr", C.POINTER(C.c_ubyte)), ("modBaseSize", W.DWORD),
                ("hModule", W.HMODULE), ("szModule", W.WCHAR * 256), ("szExePath", W.WCHAR * 260)]


def kernel():
    if os.name != "nt":
        raise ReadError("实时内存读取仅支持 Windows")
    dll = C.WinDLL("kernel32", use_last_error=True)
    declarations = {
        "CreateToolhelp32Snapshot": ([W.DWORD, W.DWORD], W.HANDLE),
        "Process32FirstW": ([W.HANDLE, C.POINTER(PROCESSENTRY32W)], W.BOOL),
        "Process32NextW": ([W.HANDLE, C.POINTER(PROCESSENTRY32W)], W.BOOL),
        "Module32FirstW": ([W.HANDLE, C.POINTER(MODULEENTRY32W)], W.BOOL),
        "Module32NextW": ([W.HANDLE, C.POINTER(MODULEENTRY32W)], W.BOOL),
        "OpenProcess": ([W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
        "CloseHandle": ([W.HANDLE], W.BOOL),
        "ReadProcessMemory": ([W.HANDLE, C.c_void_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)], W.BOOL),
        "GetExitCodeProcess": ([W.HANDLE, C.POINTER(W.DWORD)], W.BOOL),
        "IsWow64Process2": ([W.HANDLE, C.POINTER(W.WORD), C.POINTER(W.WORD)], W.BOOL),
    }
    for name, (args, result) in declarations.items():
        if hasattr(dll, name):
            fn = getattr(dll, name)
            fn.argtypes, fn.restype = args, result
    return dll


def process_ids(name):
    dll = kernel()
    handle = dll.CreateToolhelp32Snapshot(2, 0)
    if handle == C.c_void_p(-1).value:
        raise ReadError("无法枚举进程")
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = C.sizeof(entry)
        found = []
        available = dll.Process32FirstW(handle, C.byref(entry))
        while available:
            if entry.szExeFile.casefold() == name.casefold():
                found.append(entry.th32ProcessID)
            available = dll.Process32NextW(handle, C.byref(entry))
        return found
    finally:
        dll.CloseHandle(handle)


class Process:
    def __init__(self, pid):
        self.dll = kernel()
        self.pid = pid
        self.handle = self.dll.OpenProcess(0x0010 | 0x0400, False, pid)
        if not self.handle:
            raise ReadError(f"无法以只读方式打开进程，Windows 错误 {C.get_last_error()}")
        try:
            self.pointer_size = self._pointer_size()
            if C.sizeof(C.c_void_p) < self.pointer_size:
                raise ReadError("当前 Python 位数不足以读取目标进程")
            self.modules = self._modules()
        except Exception:
            self.close()
            raise

    def _pointer_size(self):
        if hasattr(self.dll, "IsWow64Process2"):
            machine, native = W.WORD(), W.WORD()
            if not self.dll.IsWow64Process2(self.handle, C.byref(machine), C.byref(native)):
                raise ReadError("无法识别进程位数")
            architecture = machine.value or native.value
            if architecture == 0x14C: return 4
            if architecture in (0x8664, 0xAA64): return 8
            raise ReadError(f"不支持的进程架构 {architecture:#x}")
        raise ReadError("需要支持 IsWow64Process2 的 Windows 10 或更新版本")

    def _modules(self):
        handle = self.dll.CreateToolhelp32Snapshot(0x8 | 0x10, self.pid)
        if handle == C.c_void_p(-1).value:
            raise ReadError("无法枚举游戏模块")
        try:
            entry = MODULEENTRY32W()
            entry.dwSize = C.sizeof(entry)
            result = {}
            available = self.dll.Module32FirstW(handle, C.byref(entry))
            while available:
                mod = Module(entry.szModule, C.cast(entry.modBaseAddr, C.c_void_p).value or 0,
                             entry.modBaseSize, entry.szExePath)
                result[mod.name.casefold()] = mod
                available = self.dll.Module32NextW(handle, C.byref(entry))
            return result
        finally:
            self.dll.CloseHandle(handle)

    def alive(self):
        code = W.DWORD()
        return bool(self.handle and self.dll.GetExitCodeProcess(self.handle, C.byref(code)) and code.value == 259)

    def read(self, address, size):
        if not self.handle or address < 0x10000 or not 0 < size <= 16 * 1024 * 1024:
            raise ReadError("读取地址或长度无效")
        buffer = C.create_string_buffer(size)
        count = C.c_size_t()
        if not self.dll.ReadProcessMemory(self.handle, address, buffer, size, C.byref(count)) or count.value != size:
            raise ReadError(f"进程数据读取失败，Windows 错误 {C.get_last_error()}")
        return buffer.raw

    def pointer(self, address):
        return int.from_bytes(self.read(address, self.pointer_size), "little")

    def close(self):
        if self.handle:
            self.dll.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self): return self
    def __exit__(self, *args): self.close()


def integer(value):
    if isinstance(value, bool): raise ValueError("地址不可为开关")
    if isinstance(value, int): return value
    if isinstance(value, str): return int(value, 0)
    raise ValueError("地址或偏移尚未配置")


TYPES = {"u8": ("<B", 1), "i32": ("<i", 4), "u32": ("<I", 4), "f32": ("<f", 4)}


def decode(data, kind):
    return struct.unpack(TYPES[kind][0], data)[0]


def pattern_matches(data, pattern):
    tokens = pattern.split()
    if not 4 <= len(tokens) <= 256:
        raise ValueError("特征码长度必须是 4～256 字节")
    mask = [None if item in ("?", "??") else int(item, 16) for item in tokens]
    if all(item is None for item in mask): raise ValueError("特征码不能全是通配符")
    if any(item is not None and not 0 <= item <= 255 for item in mask):
        raise ValueError("特征码字节无效")
    anchor = next(i for i, item in enumerate(mask) if item is not None)
    start = 0
    needle = bytes([mask[anchor]])
    while True:
        pos = data.find(needle, start)
        if pos < 0: return
        candidate = pos - anchor
        if candidate >= 0 and candidate + len(mask) <= len(data):
            if all(item is None or data[candidate + i] == item for i, item in enumerate(mask)):
                yield candidate
        start = pos + 1


class Address:
    def __init__(self, process, spec):
        self.process, self.spec = process, spec
        module = process.modules.get(spec.get("module", "").casefold())
        if module is None: raise ReadError("配置指定的游戏模块尚未加载")
        if "signature" in spec:
            # Read PE sections independently: unreadable padding must not make
            # an otherwise valid signature scan fail or match invented zeros.
            header = process.read(module.base, 0x1000)
            pe = struct.unpack_from("<I", header, 0x3C)[0]
            if pe + 24 > len(header) or header[pe:pe + 4] != b"PE\0\0":
                raise ReadError("模块 PE 头无效")
            count, optional = struct.unpack_from("<H", header, pe + 6)[0], struct.unpack_from("<H", header, pe + 20)[0]
            if not 0 < count <= 96: raise ReadError("模块节数量无效")
            sections = process.read(module.base + pe + 24 + optional, count * 40)
            matches = []
            for index in range(count):
                size, rva = struct.unpack_from("<II", sections, index * 40 + 8)
                size = min(size, module.size - rva)
                if size <= 0: continue
                # Overlap consecutive readable chunks to catch boundary matches.
                carry = b""
                for offset in range(0, size, 0x10000):
                    try:
                        block = process.read(module.base + rva + offset, min(0x10000, size - offset))
                    except ReadError:
                        carry = b""
                        continue
                    data = carry + block
                    matches.extend(module.base + rva + offset - len(carry) + pos
                                   for pos in pattern_matches(data, spec["signature"]))
                    carry = data[-255:]
            matches = sorted(set(matches))
            if len(matches) != 1: raise ReadError(f"特征码应唯一匹配，实际为 {len(matches)} 处")
            operand = matches[0] + integer(spec.get("operand_offset", 0))
            mode = spec.get("operand", "address")
            if mode == "absolute32": self.base = int.from_bytes(process.read(operand, 4), "little")
            elif mode == "relative32": self.base = operand + 4 + decode(process.read(operand, 4), "i32")
            elif mode == "address": self.base = operand
            else: raise ValueError("未知特征码操作数格式")
        else:
            rva = integer(spec.get("rva"))
            if not 0 <= rva < module.size: raise ValueError("模块偏移超出映像范围")
            self.base = module.base + rva

    def resolve(self, cache=None):
        address = self.base
        for offset in self.spec.get("chain", []):
            source = address + integer(offset)
            if cache is not None and source in cache:
                address = cache[source]
            else:
                address = self.process.pointer(source)
                if cache is not None: cache[source] = address
            if address < 0x10000: raise ReadError("游戏指针尚未就绪")
        return address + integer(self.spec.get("add", 0))

    def value(self, cache=None):
        kind = self.spec.get("type", "i32")
        address = self.resolve(cache)
        if kind == "u32" and self.process.pointer_size == 4 and cache is not None and address in cache:
            return cache[address]
        if kind == "cstring":
            length = integer(self.spec.get("length", 128))
            if not 1 <= length <= 1024: raise ValueError("字符串长度无效")
            data = self.process.read(address, length)
            if b"\0" not in data: raise ReadError("游戏字符串未终止")
            return data.split(b"\0", 1)[0].decode("utf-8", errors="replace")
        if kind not in TYPES: raise ValueError("不支持的数据类型")
        return decode(self.process.read(address, TYPES[kind][1]), kind)
