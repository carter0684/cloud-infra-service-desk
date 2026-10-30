"""Render deployable artifacts into ./dist.

dist/regional.yaml    cfn/regional.yaml with canary/index.js injected
dist/sites-seed.json  initial config/sites.json (uploaded only if the object does not exist yet;
                      after that the portal's admins own the list)
"""
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"


def main() -> None:
    cfg = json.loads((ROOT / "sites.json").read_text(encoding="utf-8"))
    script = (ROOT / "canary" / "index.js").read_text(encoding="utf-8")

    out_lines = []
    for line in (ROOT / "cfn" / "regional.yaml").read_text(encoding="utf-8").splitlines():
        if line.strip() == "__CANARY_SCRIPT__":
            indent = line[: len(line) - len(line.lstrip())]
            out_lines.extend((indent + s) if s.strip() else "" for s in script.splitlines())
        else:
            out_lines.append(line)

    DIST.mkdir(exist_ok=True)
    (DIST / "regional.yaml").write_text("\n".join(out_lines) + "\n", encoding="utf-8")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    seed = {
        "intervalMinutes": cfg["intervalMinutes"],
        "regions": [{"key": r["key"], "label": r["label"]} for r in cfg["regions"]],
        "sites": [
            {"id": s["id"], "name": s["name"], "url": s["url"], "addedBy": "initial-seed", "addedAt": now}
            for s in cfg["sites"]
        ],
        "updatedBy": "initial-seed",
        "updatedAt": now,
    }
    (DIST / "sites-seed.json").write_text(json.dumps(seed, indent=2), encoding="utf-8")
    print(f"Built {DIST / 'regional.yaml'} and {DIST / 'sites-seed.json'}")


if __name__ == "__main__":
    main()
