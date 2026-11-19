#!/usr/bin/env python3
"""Sammelt Veranstaltungen -> data/events.json (Datenbank), docs/events.json + docs/feeds/*.ics (Ausgabe)."""
import datetime as dt, hashlib, html as _html, json, os, re, sys
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests, yaml
from bs4 import BeautifulSoup
from dateutil import parser as dtparse, rrule
from icalendar import Calendar, Event as IEvent
from pydantic import BaseModel, field_validator

ROOT = Path(__file__).parent
DATA, DOCS = ROOT / "data", ROOT / "docs"
TZ = ZoneInfo("Europe/Berlin")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
TAGS = ["konzert", "jam-session", "theater", "lesung", "party", "film", "workshop", "ausstellung", "sonstiges"]
UA = {"User-Agent": "Mozilla/5.0 (compatible; event-collector; personal use)"}
NOW = dt.datetime.now(TZ)
TODAY = NOW.date()
MAX_AHEAD = dt.timedelta(days=400)  # alles dahinter ist vermutlich ein falsch geratenes Jahr


def slug(s): return re.sub(r"[^a-z0-9]+", "-", s.lower())
def sha(s): return hashlib.sha1(s.encode()).hexdigest()
def norm(s): return re.sub(r"\W+", "", s.lower())


def clean(s):
    """HTML-Entities auflösen, (auch maskierte) HTML-Tags entfernen, Whitespace normalisieren."""
    s = re.sub(r"\\[nrt]", " ", _html.unescape(s or ""))  # auch wörtlich eingetippte "\n" (doppelt escaped)
    if "<" in s: s = BeautifulSoup(s, "html.parser").get_text(" ", strip=True)
    s = re.sub(r"\s+", " ", _html.unescape(s)).strip()
    return re.sub(r"\s+([.,;:!?])", r"\1", s)


def merge_tags(*sets):
    t = set().union(*sets) - {"sonstiges"}
    return sorted(t) or ["sonstiges"]


STOP = {"der", "die", "das", "den", "dem", "des", "und", "oder", "im", "in", "am", "an", "auf", "mit", "von", "vom", "für",
        "zu", "zum", "zur", "the", "of", "and", "ein", "eine"}
def toks(s): return {w for w in re.findall(r"\w+", s.lower()) if w not in STOP}
def trunc(t): return t.rstrip().endswith(("...", "…"))
def tcompat(a, b): return not a or not b or a == b


def sim(a, b):
    """Anteil gemeinsamer Wörter, bezogen auf den kürzeren Titel (0..1). Toleriert Untertitel und Abkürzungen."""
    ta, tb = toks(a), toks(b)
    return len(ta & tb) / min(len(ta), len(tb)) if ta and tb else 0.0


def same_event(a, b):
    """Gleicher Tag, verträgliche Uhrzeit (gleich oder eine unbekannt), gleicher oder ähnlicher Titel (> 50 % Wortüberlappung)."""
    return (a["date"] == b["date"] and tcompat(a.get("time"), b.get("time"))
            and (norm(a["title"]) == norm(b["title"]) or sim(a["title"], b["title"]) > 0.5))


def resolve(events, cands):
    """Ordnet neuen Events bestehende (stabile) IDs zu -> {Index: alte_id}. Verhindert Dubletten bei schwankenden Titeln."""
    match, claimed = {}, set()
    by_id = {c["id"]: c for c in cands}
    for i, e in enumerate(events):                      # 1) identische ID
        if e["id"] in by_id and e["id"] not in claimed: match[i] = e["id"]; claimed.add(e["id"])
    for i, e in enumerate(events):                      # 2) ähnlicher Titel, gleicher Tag/Zeitpunkt
        if i in match: continue
        best = max((c for c in cands if c["id"] not in claimed and same_event(e, c)),
                   key=lambda c: sim(e["title"], c["title"]), default=None)
        if best: match[i] = best["id"]; claimed.add(best["id"])
    ev_slots, c_slots = {}, {}                          # 3) genau ein Termin im selben Zeitslot auf beiden Seiten (Titel komplett umbenannt)
    for i, e in enumerate(events):
        if i not in match and e["time"]: ev_slots.setdefault((e["date"], e["time"]), []).append(i)
    for c in cands:
        if c["id"] not in claimed and c.get("time"): c_slots.setdefault((c["date"], c["time"]), []).append(c["id"])
    for k, idx in ev_slots.items():
        if len(idx) == 1 and len(c_slots.get(k, [])) == 1: match[idx[0]] = c_slots[k][0]
    return match


def consolidate(db):
    """Bestehende Dubletten zusammenführen (gleicher Ort/Tag, ähnlicher Titel). Behält den aktuell gefundenen Eintrag. Idempotent."""
    groups = {}
    for e in db.values(): groups.setdefault((e["venue_id"], e["recurring"], e["date"]), []).append(e)
    out = {}
    for items in groups.values():
        parent = list(range(len(items)))
        def find(i):
            while parent[i] != i: parent[i] = parent[parent[i]]; i = parent[i]
            return i
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                if same_event(items[i], items[j]): parent[find(j)] = find(i)
        clusters = {}
        for i, e in enumerate(items): clusters.setdefault(find(i), []).append(e)
        for c in clusters.values():
            keep = max(c, key=lambda e: (not e.get("gone"), not trunc(e["title"]), bool(e.get("classified")), len(e["title"])))
            if trunc(keep["title"]):
                full = [e["title"] for e in c if not trunc(e["title"])]
                if full: keep["title"] = max(full, key=len)
            keep["first_seen"] = min(e["first_seen"] for e in c)
            if not keep.get("description"): keep["description"] = max((e.get("description", "") for e in c), key=len)
            out[keep["id"]] = keep
    return out


class Raw(BaseModel):
    title: str
    date: dt.date
    time: str | None = None
    description: str | None = ""
    url: str | None = None
    tags: list[str] = []
    genre: str | None = None
    location_override: str | None = None

    @field_validator("time")
    @classmethod
    def _time(cls, v):
        return v if v and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v) else None

    @field_validator("title", "genre", "location_override", mode="before")
    @classmethod
    def _clean(cls, v): return clean(v) if isinstance(v, str) else v

    @field_validator("description", mode="before")
    @classmethod
    def _desc(cls, v): return clean(v) if isinstance(v, str) else ""


def parts(x, has_time):
    if x.tzinfo: x = x.astimezone(TZ)
    return x.date(), (x.strftime("%H:%M") if has_time else None)


# ---------- Extraktoren ----------
def from_ics(content):
    out = []
    for c in Calendar.from_ical(content).walk("VEVENT"):
        s = c.decoded("DTSTART")
        d, t = parts(s, True) if isinstance(s, dt.datetime) else (s, None)
        out.append(Raw(title=str(c.get("SUMMARY", "")), date=d, time=t, description=str(c.get("DESCRIPTION", "")),
                       url=str(c["URL"]) if c.get("URL") else None))
    return out


def from_jsonld(html, base):
    out = []
    def walk(n):
        if isinstance(n, list):
            for x in n: walk(x)
        elif isinstance(n, dict):
            t = n.get("@type")
            if any(isinstance(x, str) and x.endswith("Event") for x in (t if isinstance(t, list) else [t])) and n.get("startDate"):
                s = str(n["startDate"])
                d, tm = parts(dtparse.parse(s), "T" in s or ":" in s)
                out.append(Raw(title=str(n.get("name", "")), date=d, time=tm, description=str(n.get("description") or ""),
                               url=urljoin(base, n["url"]) if isinstance(n.get("url"), str) else None))
            for x in n.values(): walk(x)
    for tag in BeautifulSoup(html, "html.parser").find_all("script", type="application/ld+json"):
        try: walk(json.loads(tag.string or ""))
        except Exception: pass
    return out


def page_text(html, v):
    """Seite auf Text reduzieren. Optionen pro Venue: text_after / text_before (Marker), max_chars."""
    base = v["source"]
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "nav", "footer", "noscript", "svg"]): t.decompose()
    for a in soup.find_all("a", href=True):
        title = clean(a.get("title") or "")
        a.append(f" [{urljoin(base, a['href'])}" + (f" | Titel: {title}" if title else "") + "]")
    text = re.sub(r"\n+", "\n", soup.get_text("\n", strip=True))
    for key, keep_tail in (("text_after", True), ("text_before", False)):
        marker = v.get(key)
        if not marker: continue
        head, sep, tail = text.partition(marker)
        if sep: text = tail if keep_tail else head
        else: print(f"  WARNUNG {v['id']}: {key} {marker!r} nicht gefunden")
    limit = int(v.get("max_chars", 40000))
    if len(text) > limit:
        print(f"  WARNUNG {v['id']}: Text von {len(text)} auf {limit} Zeichen gekürzt, kommende Termine fehlen evtl. (text_after/text_before setzen)")
        text = text[:limit]
    return text


def dedupe_raws(raws):
    """Dubletten innerhalb eines Laufs entfernen (gleiche Veranstaltung mehrfach auf der Seite oder abgekürzt/vollständig)."""
    out = []
    for r in raws:
        i = next((i for i, o in enumerate(out) if o.date == r.date and tcompat(o.time, r.time)
                  and (norm(o.title) == norm(r.title) or sim(o.title, r.title) > 0.5)), None)
        if i is None: out.append(r); continue
        o = out[i]
        rank = lambda x: (not trunc(x.title), len(x.title), len(x.description or ""))
        keep, other = (r, o) if rank(r) > rank(o) else (o, r)
        keep.time = keep.time or other.time
        keep.tags = sorted(set(keep.tags) | set(other.tags))
        out[i] = keep
    return out


def create_msg(**kw):
    """API-Aufruf mit temperature=0 (stabilere Titel); fällt zurück, falls das Modell den Parameter nicht annimmt."""
    import anthropic
    client = anthropic.Anthropic()
    try: return client.messages.create(temperature=0, **kw)
    except anthropic.BadRequestError: return client.messages.create(**kw)


def from_llm(text, v):
    import anthropic
    ev = {"type": "object", "required": ["title", "date", "tags"], "properties": {
        "title": {"type": "string"}, "date": {"type": "string", "description": "YYYY-MM-DD"},
        "time": {"type": ["string", "null"], "description": "Beginn HH:MM, null wenn nicht genannt"},
        "description": {"type": "string"}, "url": {"type": ["string", "null"]},
        "tags": {"type": "array", "items": {"type": "string", "enum": TAGS}},
        "genre": {"type": ["string", "null"]}, "location_override": {"type": ["string", "null"]}}}
    tool = {"name": "save_events", "description": "Speichert alle gefundenen kommenden Veranstaltungen.",
            "input_schema": {"type": "object", "required": ["events"], "properties": {"events": {"type": "array", "items": ev}}}}
    prompt = (f"Heute ist {TODAY.isoformat()} ({TODAY:%A}). Unten steht der Text der Programmseite von '{v['name']}' ({v['city']}). "
              "Extrahiere ALLE kommenden Veranstaltungen (ab heute) und rufe save_events auf.\n"
              "Regeln: Fehlt das Jahr, nimm das nächste passende Datum ab heute. Uhrzeit nur, wenn genannt (Beginn, HH:MM), sonst null. "
              "Wiederkehrende Reihen nicht selbst vervielfältigen, nur konkret datierte Termine. Nichts erfinden. "
              "title: Haupttitel genau wie auf der Seite (Überschrift), ohne Untertitel (Untertitel gehören in die description) und nie abgekürzt; endet ein Titel auf '…' oder '...', nimm den vollständigen Titel aus 'Titel: …', falls vorhanden. "
              "description: max. 300 Zeichen, Sprache wie im Original. url: Link zur Veranstaltung, falls im Text vorhanden "
              "(steht in eckigen Klammern hinter dem Linktext; steht dort 'Titel: …', ist das der saubere Veranstaltungstitel, nimm ihn als title). Als abgesagt markierte Veranstaltungen (z.B. 'Abgesagt', 'entfällt') weglassen. tags: passende aus der Liste, mehrere erlaubt, 'sonstiges' nur wenn nichts passt. "
              "genre: kurzes Stichwort (z.B. 'Free Jazz') oder null. location_override nur, wenn die Veranstaltung erkennbar woanders stattfindet.\n\n"
              "=== SEITENTEXT ===\n" + text)
    msg = create_msg(
        model=MODEL, max_tokens=8000, tools=[tool], tool_choice={"type": "tool", "name": "save_events"},
        messages=[{"role": "user", "content": prompt}])
    if msg.stop_reason == "max_tokens": print(f"  WARNUNG {v['id']}: Antwort abgeschnitten")
    out = []
    for b in msg.content:
        if b.type == "tool_use":
            for e in b.input.get("events", []):
                try: out.append(Raw(**e))
                except Exception as ex: print(f"  verworfen: {e.get('title')} ({ex})")
    return out


def classify(items, v):
    """Tags/Genre für Events ohne Tags (ICS/JSON-LD) per LLM bestimmen. Gibt {Index: (tags, genre)} zurück."""
    import anthropic
    out = {}
    tool = {"name": "save_tags", "description": "Speichert Tags je Veranstaltung.",
            "input_schema": {"type": "object", "required": ["events"], "properties": {"events": {"type": "array", "items": {
                "type": "object", "required": ["i", "tags"], "properties": {
                    "i": {"type": "integer"}, "tags": {"type": "array", "items": {"type": "string", "enum": TAGS}},
                    "genre": {"type": ["string", "null"]}}}}}}}
    for s in range(0, len(items), 60):
        chunk = items[s:s + 60]
        lines = "\n".join(f"{i}: {e['title']} | {e['description'][:200]}" for i, e in enumerate(chunk))
        prompt = (f"Ordne jeder Veranstaltung von '{v['name']}' ({v['city']}) passende Tags zu (mehrere erlaubt, 'sonstiges' nur wenn nichts passt) "
                  "und nenne optional ein kurzes Genre-Stichwort (z.B. 'Rock', 'Kabarett'). Nutze die Nummer i aus der Liste. Rufe save_tags auf.\n\n" + lines)
        msg = create_msg(model=MODEL, max_tokens=4000, tools=[tool],
              tool_choice={"type": "tool", "name": "save_tags"}, messages=[{"role": "user", "content": prompt}])
        for b in msg.content:
            if b.type == "tool_use":
                for x in b.input.get("events", []):
                    try: out[s + int(x["i"])] = ([t for t in x.get("tags", []) if t in TAGS], clean(x.get("genre") or "") or None)
                    except Exception: pass
    return out


def fetch_rendered(v, rounds=25):
    """Seite im Browser laden, bis zum Ende scrollen (optional 'Mehr laden'-Button klicken), dann HTML liefern."""
    from playwright.sync_api import sync_playwright
    btn = v.get("load_more")
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(user_agent=UA["User-Agent"])
        page.goto(v["source"], wait_until="load", timeout=60000)
        if v.get("wait_for"):
            try: page.wait_for_selector(v["wait_for"], timeout=20000)
            except Exception: print(f"  WARNUNG {v['id']}: wait_for {v['wait_for']!r} nicht erschienen")
        page.wait_for_timeout(int(v.get("wait", 1500)))
        last, stable = -1, 0
        for _ in range(rounds):
            if btn and page.locator(btn).count():
                try: page.locator(btn).first.click(timeout=3000)
                except Exception: pass
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1500)
            h = page.evaluate("document.body.scrollHeight")
            stable = stable + 1 if h == last else 0
            last = h
            if stable >= 2: break
        html = page.content()
        b.close()
    return html


def scrape_venue(v, cache):
    kind, url = v.get("type", "html"), v["source"]
    if kind == "ics":
        r = requests.get(url, headers=UA, timeout=30); r.raise_for_status()
        print(f"  {v['id']}: ICS")
        return from_ics(r.content)
    if v.get("render") == "browser":
        html, status = fetch_rendered(v), "Browser"
    else:
        r = requests.get(url, headers=UA, timeout=30); r.raise_for_status(); html, status = r.text, r.status_code
    if kind == "html":
        found = [x for x in from_jsonld(html, url) if TODAY <= x.date <= TODAY + MAX_AHEAD]
        if found:
            print(f"  {v['id']}: JSON-LD")
            return found
    text = page_text(html, v)
    h, c = sha(text), cache.get(v["id"])
    if c and c["hash"] == h:
        print(f"  {v['id']}: Seite unverändert, Cache")
        return [Raw(**e) for e in c["events"]]
    print(f"  {v['id']}: LLM, {len(text)} Zeichen Text" + (" (Browser)" if v.get("render") == "browser" else ""))
    evs = from_llm(text, v)
    if evs:
        cache[v["id"]] = {"hash": h, "events": [json.loads(e.model_dump_json()) for e in evs]}
    else:  # leere Ergebnisse nicht cachen, sonst bleibt ein Fehlgriff dauerhaft hängen
        cache.pop(v["id"], None)
        print(f"  WARNUNG {v['id']}: Modell fand 0 Events (Status {status}, {len(html)} Zeichen HTML). Text beginnt mit:\n    {text[:300]!r}")
    return evs


# ---------- Verarbeitung ----------
def make_event(r, v, cfg, recurring):
    tags = {t for t in r.tags if t in TAGS} | set(v.get("default_tags", []))
    hay = f"{r.title} {r.description}"
    for rule in cfg.get("global_rules", []) + v.get("rules", []):
        if re.search(rule["match"], hay, re.I): tags |= set(rule["tags"])
    if len(tags) > 1: tags.discard("sonstiges")
    if not tags: tags = {"sonstiges"}
    return {"id": sha(f"{v['id']}|{r.date}|{r.time or ''}|{norm(r.title)}")[:12], "venue_id": v["id"], "title": r.title.strip(),
            "date": r.date.isoformat(), "time": r.time, "time_known": bool(r.time), "tags": sorted(tags),
            "genre": r.genre, "description": (r.description or "")[:500], "url": r.url or v.get("url"),
            "location_override": r.location_override, "recurring": recurring}


def migrate(db):
    """Alte Einträge bereinigen (Entities/HTML) und IDs neu berechnen. Idempotent."""
    out = {}
    for e in db.values():
        t = clean(e["title"]); changed = t != e["title"]
        e["title"], e["description"] = t, clean(e.get("description", ""))
        if changed: e["id"] = sha(f"{e['venue_id']}|{e['date']}|{e.get('time') or ''}|{norm(e['title'])}")[:12]
        out[e["id"]] = e
    return out


def load(p):
    return json.loads(p.read_text("utf-8")) if p.exists() else {}


def write_ics(path, events, venues):
    cal = Calendar()
    cal.add("prodid", "-//event-collector//DE"); cal.add("version", "2.0")
    for e in events:
        ev, d, v = IEvent(), dt.date.fromisoformat(e["date"]), venues[e["venue_id"]]
        ev.add("uid", e["id"] + "@event-collector")
        ev.add("summary", ("[entfällt?] " if e.get("gone") else "") + e["title"])
        if e["time_known"]:
            s = dt.datetime.combine(d, dt.time.fromisoformat(e["time"]), TZ).astimezone(dt.timezone.utc)
            ev.add("dtstart", s); ev.add("dtend", s + dt.timedelta(hours=2))
        else:
            ev.add("dtstart", d)
        ev.add("location", e.get("location_override") or f"{v['name']}, {v['city']}")
        ev.add("description", "\n".join(x for x in [e["description"], e["url"]] if x))
        if e["url"]: ev.add("url", e["url"])
        ev.add("dtstamp", dt.datetime.now(dt.timezone.utc))
        cal.add_component(ev)
    path.write_bytes(cal.to_ical())


def export(db, venues):
    evs = sorted(db.values(), key=lambda e: (e["date"], e["time"] or "", e["title"]))
    used = sorted({t for e in evs for t in e["tags"]})
    DOCS.mkdir(exist_ok=True)
    (DOCS / "events.json").write_text(json.dumps({
        "generated": NOW.isoformat(timespec="seconds"), "tags": TAGS, "tags_used": used,
        "venues": [{k: v.get(k) for k in ("id", "name", "city", "url")} for v in venues.values()],
        "events": evs}, ensure_ascii=False, indent=1), "utf-8")
    fd = DOCS / "feeds"; fd.mkdir(exist_ok=True)
    for f in fd.glob("*.ics"): f.unlink()
    up = [e for e in evs if e["date"] >= TODAY.isoformat()]
    feeds = {"all": up}
    for t in used: feeds[f"tag-{slug(t)}"] = [e for e in up if t in e["tags"]]
    for c in {v["city"] for v in venues.values()}:
        feeds[f"stadt-{slug(c)}"] = [e for e in up if venues[e["venue_id"]]["city"] == c]
    for vid in venues: feeds[f"venue-{slug(vid)}"] = [e for e in up if e["venue_id"] == vid]
    for name, items in feeds.items(): write_ics(fd / f"{name}.ics", items, venues)


def main():
    cfg = yaml.safe_load((ROOT / "venues.yaml").read_text("utf-8"))
    venues = {v["id"]: v for v in cfg["venues"]}
    DATA.mkdir(exist_ok=True)
    db = consolidate(migrate({k: e for k, e in load(DATA / "events.json").items() if e["venue_id"] in venues}))
    cache = load(DATA / "cache.json")
    now, errors, seen = NOW.isoformat(timespec="seconds"), [], set()

    def in_range(r): return TODAY <= r.date <= TODAY + MAX_AHEAD

    def ingest(vid, raws, recurring):
        v, todo, fresh = venues[vid], [], []
        rs = dedupe_raws([r for r in raws if r.title.strip()])
        events = [make_event(r, v, cfg, recurring) for r in rs]
        match = resolve(events, [c for c in db.values() if c["venue_id"] == vid and c["recurring"] == recurring])
        for i, (r, e) in enumerate(zip(rs, events)):
            if i in match and match[i] != e["id"]:  # derselbe Termin mit anderem Titel -> alte ID behalten
                prev_ev = db[match[i]]
                e["id"] = prev_ev["id"]
                if not trunc(prev_ev["title"]) and sim(e["title"], prev_ev["title"]) > 0.5: e["title"] = prev_ev["title"]
            old = db.get(e["id"])
            if old and old.get("classified"):  # frühere Klassifikation behalten
                e["tags"] = merge_tags(e["tags"], old["tags"])
                e["genre"] = e["genre"] or old.get("genre")
                e["classified"] = True
            elif r.tags or recurring:
                e["classified"] = True
            else:
                e["classified"] = False; todo.append(e)
            if not old: fresh.append(e)
            e.update(first_seen=old["first_seen"] if old else now, last_seen=now, gone=False)
            db[e["id"]] = e
        if todo:
            try:
                res = classify(todo, v)
                for i, e in enumerate(todo):
                    if i in res:
                        tags, genre = res[i]
                        e["tags"] = merge_tags(e["tags"], tags); e["genre"] = e["genre"] or genre; e["classified"] = True
                print(f"  {len(res)}/{len(todo)} Events per LLM klassifiziert")
            except Exception as ex:
                print(f"  WARNUNG Klassifikation {vid}: {type(ex).__name__}: {ex}")
        for e in fresh:
            if e["tags"] == ["sonstiges"]: print(f"  [sonstiges] {vid}: {e['title']}")

    for v in venues.values():
        if not v.get("source"): continue
        print(f"{v['id']} ...")
        try:
            raws = [r for r in scrape_venue(v, cache) if in_range(r)]
        except Exception as ex:
            errors.append(f"{v['id']}: {type(ex).__name__}: {ex}"); continue
        prev = [e for e in db.values() if e["venue_id"] == v["id"] and not e["recurring"] and not e["gone"] and e["date"] >= TODAY.isoformat()]
        if not raws and prev and not v.get("allow_empty"):  # sieht nach kaputtem Scraper aus, nichts als "entfällt" markieren
            errors.append(f"{v['id']}: 0 Events gefunden, vorher {len(prev)}"); continue
        if not raws: print(f"  WARNUNG {v['id']}: 0 kommende Events erkannt, Quelle/Text prüfen")
        ingest(v["id"], raws, False); seen.add((v["id"], False))
        print(f"  {len(raws)} Events")

    for r in yaml.safe_load((ROOT / "recurring.yaml").read_text("utf-8")) or []:
        start = dt.datetime.combine(dt.date.fromisoformat(str(r["start"])), dt.time())
        days = rrule.rrulestr(r["rrule"], dtstart=start).between(
            dt.datetime.combine(TODAY, dt.time()), dt.datetime.combine(TODAY + dt.timedelta(days=90), dt.time()), inc=True)
        ingest(r["venue"], [Raw(title=r["title"], date=d.date(), time=str(r["time"]) if r.get("time") else None,
                                description=r.get("description", ""), tags=r.get("tags", [])) for d in days], True)
        seen.add((r["venue"], True))

    for e in db.values():  # Termin war schon bekannt, steht aber nicht mehr auf der Seite
        if (e["venue_id"], e["recurring"]) in seen and e["last_seen"] != now and e["date"] >= TODAY.isoformat():
            e["gone"] = True
    cutoff = (TODAY - dt.timedelta(days=14)).isoformat()
    db = {k: e for k, e in db.items() if e["date"] >= cutoff}

    (DATA / "events.json").write_text(json.dumps(db, ensure_ascii=False, indent=1), "utf-8")
    (DATA / "cache.json").write_text(json.dumps(cache, ensure_ascii=False), "utf-8")
    export(db, venues)
    if errors:
        print("\nFEHLER:\n" + "\n".join(errors)); sys.exit(1)


if __name__ == "__main__":
    main()
