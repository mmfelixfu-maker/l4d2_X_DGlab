"""Build a self-contained DGHub import ZIP; optionally copy a new plugin folder."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import shutil
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--install", help="不存在的 DGHub 插件目录；不覆盖已有目录")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    names = ["main.py", "model.py", "native.py", "reader.py", "sampling.py", "diagnose.py", "discover.py",
             "prepare_profile.py", "build.py", "package.py", "manifest.json", "README.md", "CHANGELOG.md", ".gitignore",
             "profiles/game.json", "L4D2-2.2.4.3.CT", "settings-preview.html",
             "discovered-netvars.json", "tests/test_plugin.py", "tests/benchmark_sampling.py"]
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest["entry"] not in names: raise ValueError("入口未打包")
    for name in names:
        if name.endswith(".py"): ast.parse((root / name).read_text(encoding="utf-8"))
    output = root.parent / "dist"
    output.mkdir(exist_ok=True)
    archive = output / "l4d2_memory.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name in names: bundle.write(root / name, name)
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip() is not None: raise ValueError("ZIP 校验失败")
        if "manifest.json" not in bundle.namelist(): raise ValueError("ZIP 根目录缺少 manifest")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / "l4d2_memory.zip.sha256").write_text(f"{digest}  l4d2_memory.zip\n", encoding="ascii")
    release = output / f"l4d2_memory-v{manifest['version']}.zip"
    shutil.copy2(archive, release)
    release.with_suffix(".zip.sha256").write_text(f"{digest}  {release.name}\n", encoding="ascii")
    if args.install:
        destination = Path(args.install).resolve()
        if destination.exists(): raise FileExistsError("安装目录已经存在，不覆盖用户文件")
        destination.mkdir()
        for name in names:
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / name, target)
        print(json.dumps({"installed": str(destination)}, ensure_ascii=False))
    print(json.dumps({"archive": str(archive), "bytes": archive.stat().st_size, "sha256": digest}, ensure_ascii=False))


if __name__ == "__main__": main()
