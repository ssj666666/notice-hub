"""复核 GitHub 仓库内容：文件清单 + 敏感文件检查 + 与本地一致性。"""
import json
import os
import subprocess
import sys
from pathlib import Path

TEMP = Path(os.environ.get("TEMP", "."))


def load(name: str):
    raw = (TEMP / name).read_bytes()
    for enc in ("utf-8-sig", "utf-16", "utf-8"):
        try:
            return json.loads(raw.decode(enc))
        except Exception:
            continue
    raise RuntimeError(f"无法解析 {name}")


tree = load("tree.json")
repo = load("repo.json")

print("=== 仓库信息 ===")
for label, key in (("名称", "full_name"), ("可见性", "visibility"),
                   ("默认分支", "default_branch"), ("地址", "html_url"),
                   ("描述", "description")):
    print(f"  {label:8}: {repo.get(key)}")

blobs = sorted(t["path"] for t in tree["tree"] if t["type"] == "blob")
print()
print(f"=== GitHub 上的文件（{len(blobs)} 个）===")
for p in blobs:
    print("  ", p)

print()
print("=== 敏感文件泄漏检查 ===")
BAD = ["config.yaml", "data/", ".venv/", "_token", "_device", ".db",
       ".env", "secret", "credential", "hosts.yml"]
leaks = [(p, b) for p in blobs for b in BAD if b in p]
if leaks:
    for p, b in leaks:
        print(f"  [泄漏] {p}   匹配到 {b!r}")
    sys.exit(1)
print("  OK：远端没有任何敏感文件")

local = subprocess.run(["git", "ls-files"], capture_output=True,
                       text=True, encoding="utf-8").stdout.split()
remote = set(blobs)
print()
print("=== 与本地一致性 ===")
print(f"  本地 git 跟踪 : {len(local)} 个")
print(f"  远端          : {len(remote)} 个")
missing = sorted(set(local) - remote)
extra = sorted(remote - set(local))
print(f"  本地有但没推上去: {missing if missing else '（无）'}")
print(f"  远端多出来的    : {extra if extra else '（无）'}")
print()
if not missing and not extra:
    print("  结论：本地与远端完全一致")
else:
    print("  注意：两边有差异，见上")
