"""逐个端点测 tier：标准版能用哪些、哪些要专业版。"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

KEY = os.environ.get("ASHARE_API_KEY") or "ct-01085571653a0ad83a3a237bbee44d5c"
BASE = "https://api.ashareapi.com/v1"

CASES = [
    ("stocks", "/stocks", {"limit": 3}),
    ("trade-calendar", "/trade-calendar", {}),
    ("market-kline", "/market-kline", {"date": "2026-11-13"}),
    ("snapshot-batch", "/snapshot-batch", {"codes": "sh600519,sz000001"}),
    ("quote-batch", "/quote-batch", {"codes": "sh600519,sz000001"}),
    ("kline-batch", "/kline-batch", {"codes": "sh600519,sz000001", "count": 3}),
    ("kline-full", "/kline-full", {"code": "sh600519"}),
    ("finance", "/finance", {"code": "sh600519", "num": 1}),
    ("finance-batch", "/finance-batch", {"codes": "sh600519,sz000001", "num": 1}),
    ("profile", "/profile", {"code": "sh600519"}),
    ("profile-batch", "/profile-batch", {"codes": "sh600519,sz000001"}),
    ("dividend", "/dividend", {"code": "sh600519"}),
    ("suspension", "/suspension", {}),
    ("index-constituent", "/index-constituent", {"code": "sh000300"}),
    ("limit-up", "/limit-up", {}),
    ("market-mood", "/market-mood", {}),
    ("broken-board", "/broken-board", {}),
    ("limit-reason", "/limit-reason", {"code": "sh600519"}),
    ("events", "/events", {"code": "sh600519"}),
    ("notice", "/notice", {"code": "sh600519", "limit": 2}),
]


def call(path, params):
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body[:200]}
    except Exception as e:  # noqa: BLE001
        return None, {"error": f"{type(e).__name__}: {e}"}


rows = []
for name, path, params in CASES:
    code, res = call(path, params)
    ok = bool(res.get("ok"))
    need = ""
    if not ok:
        need = (res.get("min_tier") or "")
        msg = res.get("error") or res.get("raw") or ""
        if not need and "专业版" in str(msg):
            need = "pro"
        if not need and "机构版" in str(msg):
            need = "institution"
    rows.append((name, code, ok, need, res.get("tier") or ""))
    print(f"{name:20s} http={code} ok={ok} tier={res.get('tier')} need={need}")

print("\n--- 汇总 ---")
print("可用:", [r[0] for r in rows if r[2]])
print("需专业版:", [r[0] for r in rows if not r[2] and r[3] == "pro"])
print("其他不可用:", [(r[0], r[3]) for r in rows if not r[2] and r[3] != "pro"])
