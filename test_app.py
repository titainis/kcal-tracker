"""Smoke test: python test_app.py — spins up the server on a temp DB and checks the core loop."""
import json, os, tempfile, threading, urllib.request

import app

app.DB = os.path.join(tempfile.mkdtemp(), "test.db")
app.init()
srv = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"

def call(path, data=None, method=None):
    req = urllib.request.Request(base + path, json.dumps(data).encode() if data else None, method=method)
    return json.loads(urllib.request.urlopen(req).read())

call("/api/entries", {"date": "2026-01-01", "meal": "Lunch", "name": "Rice", "grams": 200,
                      "per100": {"kcal": 130, "protein": 2.7, "carbs": 28, "fat": 0.3}, "save": True})
day = call("/api/day?date=2026-01-01")
assert day["entries"][0]["kcal"] == 260.0, day
assert any(f["name"] == "Rice" for f in call("/api/foods?q=ric")), "saved food not searchable"
call("/api/weights", {"date": "2026-01-01", "kg": 80})
assert call("/api/day?date=2026-01-01")["weight"] == 80
eid = day["entries"][0]["id"]  # edit: PUT recomputes totals from grams × per-100g
call(f"/api/entries/{eid}", {"date": "2026-01-01", "meal": "Dinner", "name": "Rice", "grams": 150,
                             "per100": {"kcal": 130, "protein": 2.7, "carbs": 28, "fat": 0.3}}, method="PUT")
edited = call("/api/day?date=2026-01-01")["entries"][0]
assert edited["kcal"] == 195.0 and edited["meal"] == "Dinner", edited
call(f"/api/entries/{eid}", method="DELETE")
assert call("/api/day?date=2026-01-01")["entries"] == []

call("/api/exercises", {"date": "2026-01-01", "name": "Running", "minutes": 30, "kcal": 350})
ex = call("/api/day?date=2026-01-01")["exercises"]
assert ex[0]["kcal"] == 350, ex
call(f"/api/exercises/{ex[0]['id']}", method="DELETE")
assert call("/api/day?date=2026-01-01")["exercises"] == []

with app.db() as c:  # barcode: cached product resolves without any network call
    c.execute("INSERT INTO foods(name,kcal,protein,carbs,fat,barcode) VALUES('TestBar',400,10,50,15,'4000000000000')")
assert call("/api/barcode?code=4000000000000")["name"] == "TestBar"
print("ok")
