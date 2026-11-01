"""Apply vision-model groups to glyphmgr classes + propagate labels to rare classes.

Usage: python scripts/apply_vision_groups.py <groups.json> <labels.json>
groups.json: {"sheet_01": {"groups": [["G0","G17"],...], "bad": [...]}, ...}
labels.json: {"G0": {"key": "auto:0", "n": 55}, ...} (made by the sheet generator)
"""
import json, sys, os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from glyphmgr.core import Library

def main():
    groups_fp, labels_fp = sys.argv[1], sys.argv[2]
    groups = json.load(open(groups_fp, encoding="utf-8"))
    labels = json.load(open(labels_fp, encoding="utf-8"))
    lib = Library(os.path.join(ROOT, "out"))
    merged = 0
    failures = 0
    for sheet, data in groups.items():
        if not isinstance(data, dict):
            continue
        for grp in data.get("groups") or []:
            keys = []
            for g in grp:
                info = labels.get(g)
                if info and info.get("key") and info["key"] not in keys:
                    keys.append(info["key"])
            if len(keys) < 2:
                continue
            dst = keys[0]
            for src in keys[1:]:
                r = lib.merge_classes(src, dst)
                if r.get("ok"):
                    merged += 1
                else:
                    failures += 1
        for g in data.get("bad") or []:
            info = labels.get(g)
            if info:
                lib.set_label(info["key"], "⚠疑似两字/碎片")
    lib.refresh_classes()
    print("classes after merge:", len(lib.classes or []), "| merges:", merged, "| failures:", failures)

if __name__ == "__main__":
    main()