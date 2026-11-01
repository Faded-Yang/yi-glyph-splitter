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
    skipped = 0

    def live_keys(grp):
        live = {c.get("key") for c in (lib.classes or [])}
        keys = []
        for g in grp:
            info = labels.get(g)
            if info and info.get("key") in live and info["key"] not in keys:
                keys.append(info["key"])
        return keys

    for sheet, data in groups.items():
        if not isinstance(data, dict):
            continue
        for grp in data.get("groups") or []:
            keys = live_keys(grp)
            if len(keys) < 2:
                skipped += 1
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
            if info and info.get("key") in {c.get("key") for c in (lib.classes or [])}:
                lib.set_label(info["key"], "⚠疑似两字/碎片")
    lib.refresh_classes()
    print("classes after merge:", len(lib.classes or []), "| merges:", merged, "| failures:", failures, "| skipped:", skipped)

if __name__ == "__main__":
    main()