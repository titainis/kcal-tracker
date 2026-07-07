"""Private local calorie tracker. Stdlib only: http.server + sqlite3.

Run:  python app.py
PC:    http://localhost:8090
Phone: http://<LAN IP shown on startup>:8090  (same Wi-Fi; Add to Home Screen)
"""
import json, os, re, socket, sqlite3, urllib.error, urllib.request
from datetime import date
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "tracker.db")
PORT = 8090

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries(
  id INTEGER PRIMARY KEY, date TEXT NOT NULL, meal TEXT NOT NULL, name TEXT NOT NULL,
  grams REAL NOT NULL, kcal REAL NOT NULL, protein REAL NOT NULL,
  carbs REAL NOT NULL, fat REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_entries_date ON entries(date);
CREATE TABLE IF NOT EXISTS foods(  -- values per 100 g
  id INTEGER PRIMARY KEY, name TEXT UNIQUE COLLATE NOCASE NOT NULL,
  kcal REAL NOT NULL, protein REAL NOT NULL, carbs REAL NOT NULL, fat REAL NOT NULL,
  barcode TEXT);
CREATE TABLE IF NOT EXISTS weights(date TEXT PRIMARY KEY, kg REAL NOT NULL);
CREATE TABLE IF NOT EXISTS exercises(
  id INTEGER PRIMARY KEY, date TEXT NOT NULL, name TEXT NOT NULL,
  minutes REAL NOT NULL, kcal REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_exercises_date ON exercises(date);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# ponytail: tiny starter set so day one isn't a blank search box; the real DB
# grows from foods the user saves. Bulk USDA import can come later if wanted.
SEED_FOODS = [  # name, kcal, protein, carbs, fat — per 100 g
    ("Chicken breast, cooked", 165, 31, 0, 3.6),
    ("Ground beef 15%, cooked", 250, 26, 0, 15),
    ("Salmon, cooked", 208, 20, 0, 13),
    ("Egg, whole", 143, 12.6, 0.7, 9.5),
    ("White rice, cooked", 130, 2.7, 28, 0.3),
    ("Pasta, cooked", 158, 5.8, 31, 0.9),
    ("Potato, boiled", 87, 1.9, 20, 0.1),
    ("Bread, white", 265, 9, 49, 3.2),
    ("Oats, dry", 379, 13, 68, 6.5),
    ("Milk 2%", 50, 3.3, 4.8, 2),
    ("Greek yogurt, plain", 59, 10, 3.6, 0.4),
    ("Cheddar cheese", 403, 25, 1.3, 33),
    ("Butter", 717, 0.9, 0.1, 81),
    ("Olive oil", 884, 0, 0, 100),
    ("Banana", 89, 1.1, 23, 0.3),
    ("Apple", 52, 0.3, 14, 0.2),
    ("Avocado", 160, 2, 8.5, 14.7),
    ("Almonds", 579, 21, 22, 50),
    ("Peanut butter", 588, 25, 20, 50),
    ("Broccoli, cooked", 35, 2.4, 7, 0.4),
]

DEFAULT_SETTINGS = {"kcal": "2000", "protein": "120", "carbs": "230", "fat": "65"}


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init():
    with db() as c:
        c.executescript(SCHEMA)
        try:  # migrate DBs created before barcode support
            c.execute("ALTER TABLE foods ADD COLUMN barcode TEXT")
        except sqlite3.OperationalError:
            pass
        if not c.execute("SELECT 1 FROM foods LIMIT 1").fetchone():
            c.executemany("INSERT INTO foods(name,kcal,protein,carbs,fat) VALUES(?,?,?,?,?)", SEED_FOODS)
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))


def rows(cur):
    return [dict(r) for r in cur]


# --- Meal photo AI: local Ollama first, Claude API fallback ------------------

PHOTO_PROMPT = (
    "Identify each distinct food item in this meal photo. For every item estimate the "
    "portion weight in grams and its nutrition PER 100 GRAMS: kcal, protein, carbs, fat. "
    "Use realistic typical values; if unsure, give your best estimate."
)
FOOD_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "grams": {"type": "number"},
                       "kcal": {"type": "number"}, "protein": {"type": "number"},
                       "carbs": {"type": "number"}, "fat": {"type": "number"}},
        "required": ["name", "grams", "kcal", "protein", "carbs", "fat"],
        "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False,
}
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5vl:7b")


def ollama_analyze(image_b64):
    req = urllib.request.Request(
        OLLAMA_URL + "/api/chat",
        json.dumps({"model": OLLAMA_MODEL, "stream": False, "format": FOOD_SCHEMA,
                    "messages": [{"role": "user", "content": PHOTO_PROMPT,
                                  "images": [image_b64]}]}).encode(),
        {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(json.loads(r.read())["message"]["content"])["items"]


def claude_analyze(image_b64):
    import anthropic  # optional dep; only needed for the cloud fallback
    resp = anthropic.Anthropic().messages.create(
        model="claude-opus-4-8",
        max_tokens=2048,
        output_config={"format": {"type": "json_schema", "schema": FOOD_SCHEMA}},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": image_b64}},
            {"type": "text", "text": PHOTO_PROMPT},
        ]}])
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)["items"]


def analyze_photo(image_b64):
    try:
        return ollama_analyze(image_b64)
    except Exception as e:
        if os.environ.get("ANTHROPIC_API_KEY"):
            return claude_analyze(image_b64)
        raise RuntimeError(
            "No AI backend available. For fully local: install Ollama (ollama.com) and run "
            f"'ollama pull {OLLAMA_MODEL}'. Or set ANTHROPIC_API_KEY to use the Claude API. "
            f"(Ollama error: {e})")


def barcode_lookup(code, c):
    hit = c.execute("SELECT * FROM foods WHERE barcode=?", (code,)).fetchone()
    if hit:
        return dict(hit)
    # ponytail: one product query to Open Food Facts on first scan, cached locally forever
    url = f"https://world.openfoodfacts.org/api/v2/product/{quote(code)}.json?fields=product_name,brands,nutriments"
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.loads(r.read())
    if data.get("status") != 1:
        return None
    p, n = data["product"], data["product"].get("nutriments", {})
    brand, product = p.get("brands", "").split(",")[0].strip(), (p.get("product_name") or "").strip()
    name = product if brand.lower() in product.lower() else f"{brand} {product}".strip()
    name = name or code
    food = {"name": name.strip(),
            "kcal": n.get("energy-kcal_100g") or 0, "protein": n.get("proteins_100g") or 0,
            "carbs": n.get("carbohydrates_100g") or 0, "fat": n.get("fat_100g") or 0}
    c.execute("""INSERT INTO foods(name,kcal,protein,carbs,fat,barcode) VALUES(?,?,?,?,?,?)
                 ON CONFLICT(name) DO UPDATE SET barcode=excluded.barcode""",
              (*food.values(), code))
    return food


def entry_fields(b):
    name = str(b["name"]).strip()
    grams = float(b["grams"])
    per = {k: float(b["per100"].get(k) or 0) for k in ("kcal", "protein", "carbs", "fat")}
    if not name or grams <= 0:
        raise ValueError("name and grams required")
    return name, grams, per, tuple(round(per[k] * grams / 100, 1) for k in ("kcal", "protein", "carbs", "fat"))


def upsert_food(c, name, per):
    c.execute("""INSERT INTO foods(name,kcal,protein,carbs,fat) VALUES(?,?,?,?,?)
                 ON CONFLICT(name) DO UPDATE SET kcal=excluded.kcal,
                 protein=excluded.protein, carbs=excluded.carbs, fat=excluded.fat""",
              (name, per["kcal"], per["protein"], per["carbs"], per["fat"]))


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            with open(os.path.join(ROOT, "index.html"), "rb") as f:
                return self.reply(200, f.read(), "text/html; charset=utf-8")
        with db() as c:
            if u.path == "/api/day":
                d = q.get("date", [date.today().isoformat()])[0]
                w = c.execute("SELECT kg FROM weights WHERE date=?", (d,)).fetchone()
                latest = c.execute("SELECT kg FROM weights ORDER BY date DESC LIMIT 1").fetchone()
                return self.reply(200, {
                    "entries": rows(c.execute("SELECT * FROM entries WHERE date=? ORDER BY id", (d,))),
                    "exercises": rows(c.execute("SELECT * FROM exercises WHERE date=? ORDER BY id", (d,))),
                    "settings": dict(c.execute("SELECT key,value FROM settings")),
                    "weight": w["kg"] if w else None,
                    "latest_kg": latest["kg"] if latest else None,
                })
            if u.path == "/api/foods":
                s = q.get("q", [""])[0]
                return self.reply(200, rows(c.execute(
                    "SELECT * FROM foods WHERE name LIKE ? ORDER BY name LIMIT 10", (f"%{s}%",))))
            if u.path == "/api/weights":
                return self.reply(200, rows(c.execute("SELECT * FROM weights ORDER BY date")))
            if u.path == "/api/barcode":
                code = q.get("code", [""])[0].strip()
                if not code.isdigit():
                    return self.reply(400, {"error": "numeric barcode required"})
                try:
                    food = barcode_lookup(code, c)
                except (urllib.error.URLError, OSError) as e:
                    return self.reply(502, {"error": f"Open Food Facts unreachable: {e}"})
                return self.reply(200, food) if food else self.reply(404, {"error": "product not found"})
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            b = self.json_body()
            with db() as c:
                if p == "/api/entries":
                    name, grams, per, totals = entry_fields(b)
                    c.execute(
                        "INSERT INTO entries(date,meal,name,grams,kcal,protein,carbs,fat) VALUES(?,?,?,?,?,?,?,?)",
                        (b["date"], b["meal"], name, grams, *totals))
                    if b.get("save"):
                        upsert_food(c, name, per)
                    return self.reply(200, {"ok": True})
                if p == "/api/exercises":
                    name, minutes, kcal = str(b["name"]).strip(), float(b["minutes"]), float(b["kcal"])
                    if not name or minutes <= 0 or kcal < 0:
                        return self.reply(400, {"error": "name, minutes and kcal required"})
                    c.execute("INSERT INTO exercises(date,name,minutes,kcal) VALUES(?,?,?,?)",
                              (b["date"], name, minutes, round(kcal)))
                    return self.reply(200, {"ok": True})
                if p == "/api/weights":
                    c.execute("INSERT INTO weights(date,kg) VALUES(?,?) ON CONFLICT(date) DO UPDATE SET kg=excluded.kg",
                              (b["date"], float(b["kg"])))
                    return self.reply(200, {"ok": True})
                if p == "/api/photo":
                    try:
                        items = analyze_photo(b["image"])
                    except RuntimeError as e:
                        return self.reply(502, {"error": str(e)})
                    return self.reply(200, {"items": items})
                if p == "/api/settings":
                    for k in DEFAULT_SETTINGS:
                        if k in b:
                            c.execute("UPDATE settings SET value=? WHERE key=?", (str(float(b[k])), k))
                    return self.reply(200, {"ok": True})
        except (KeyError, ValueError, TypeError) as e:
            return self.reply(400, {"error": str(e)})
        self.reply(404, {"error": "not found"})

    def do_PUT(self):
        m = re.fullmatch(r"/api/entries/(\d+)", urlparse(self.path).path)
        if not m:
            return self.reply(404, {"error": "not found"})
        try:
            b = self.json_body()
            name, grams, per, totals = entry_fields(b)
            with db() as c:
                c.execute("UPDATE entries SET meal=?,name=?,grams=?,kcal=?,protein=?,carbs=?,fat=? WHERE id=?",
                          (b["meal"], name, grams, *totals, m.group(1)))
                if b.get("save"):
                    upsert_food(c, name, per)
            self.reply(200, {"ok": True})
        except (KeyError, ValueError, TypeError) as e:
            self.reply(400, {"error": str(e)})

    def do_DELETE(self):
        m = re.fullmatch(r"/api/(entries|exercises)/(\d+)", urlparse(self.path).path)
        if m:
            with db() as c:
                c.execute(f"DELETE FROM {m.group(1)} WHERE id=?", (m.group(2),))
            return self.reply(200, {"ok": True})
        self.reply(404, {"error": "not found"})

    def log_message(self, *args):
        pass


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no traffic sent; just picks the LAN interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    init()
    print(f"On this PC:    http://localhost:{PORT}")
    print(f"On your phone: http://{lan_ip()}:{PORT}  (same Wi-Fi)")
    # ponytail: no auth — LAN-only by design. Add a PIN if the network is shared.
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
