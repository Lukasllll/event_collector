#!/usr/bin/env python3
"""Sammelt Veranstaltungen -> data/events.json (Datenbank), docs/events.json + docs/feeds/*.ics (Ausgabe)."""
import datetime as dt, hashlib, json, os, re, sys
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

    @field_validator("description", mode="before")
    @classmethod
    def _desc(cls, v): return v or ""


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
                out.append(Raw(title=str(n.get("name", "")), date=d, time=tm, description=str(n.get("description") or "")[:500],
                               url=urljoin(base, n["url"]) if isinstance(n.get("url"), str) else None))
            for x in n.values(): walk(x)
    for tag in BeautifulSoup(html, "html.parser").find_all("script", type="application/ld+json"):
        try: walk(json.loads(tag.string or ""))
        except Exception: pass
    return out


def page_text(html, base):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "nav", "footer", "noscript", "svg"]): t.decompose()
    for a in soup.find_all("a", href=True): a.append(f" [{urljoin(base, a['href'])}]")
    return re.sub(r"\n+", "\n", soup.get_text("\n", strip=True))[:40000]


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
              "description: max. 300 Zeichen, Sprache wie im Original. url: Link zur Veranstaltung, falls im Text vorhanden "
              "(steht in eckigen Klammern hinter dem Linktext). tags: passende aus der Liste, mehrere erlaubt, 'sonstiges' nur wenn nichts passt. "
              "genre: kurzes Stichwort (z.B. 'Free Jazz') oder null. location_override nur, wenn die Veranstaltung erkennbar woanders stattfindet.\n\n"
              "=== SEITENTEXT ===\n" + text)
    msg = anthropic.Anthropic().messages.create(
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


def scrape_venue(v, cache):
    kind, url = v.get("type", "html"), v["source"]
    r = requests.get(url, headers=UA, timeout=30); r.raise_for_status()
    if kind == "ics":
        print(f"  {v['id']}: ICS")
        return from_ics(r.content)
    if kind == "html":
        found = [x for x in from_jsonld(r.text, url) if TODAY <= x.date <= TODAY + MAX_AHEAD]
        if found:
            print(f"  {v['id']}: JSON-LD")
            return found
    text = page_text(r.text, url)
    h, c = sha(text), cache.get(v["id"])
    if c and c["hash"] == h:
        print(f"  {v['id']}: Seite unverändert, Cache")
        return [Raw(**e) for e in c["events"]]
    print(f"  {v['id']}: LLM, {len(text)} Zeichen Text")
    evs = from_llm(text, v)
    cache[v["id"]] = {"hash": h, "events": [json.loads(e.model_dump_json()) for e in evs]}
    return evs


# ---------- Verarbeitung ----------
def make_event(r, v, cfg, recurring):
    tags = {t for t in r.tags if t in TAGS} | set(v.get("default_tags", []))
    hay = f"{r.title} {r.description}"
    for rule in cfg.get("global_rules", []) + v.get("rules", []):
        if re.search(rule["match"], hay, re.I): tags |= set(rule["tags"])
    if len(tags) > 1: tags.discard("sonstiges")
    if not tags:
        tags = {"sonstiges"}
        print(f"  [sonstiges] {v['id']}: {r.title}")
    return {"id": sha(f"{v['id']}|{r.date}|{norm(r.title)}")[:12], "venue_id": v["id"], "title": r.title.strip(),
            "date": r.date.isoformat(), "time": r.time, "time_known": bool(r.time), "tags": sorted(tags),
            "genre": r.genre, "description": (r.description or "")[:500], "url": r.url or v.get("url"),
            "location_override": r.location_override, "recurring": recurring}


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
    db = {k: e for k, e in load(DATA / "events.json").items() if e["venue_id"] in venues}
    cache = load(DATA / "cache.json")
    now, errors, seen = NOW.isoformat(timespec="seconds"), [], set()

    def in_range(r): return TODAY <= r.date <= TODAY + MAX_AHEAD

    def ingest(vid, raws, recurring):
        for r in raws:
            if not r.title.strip(): continue
            e = make_event(r, venues[vid], cfg, recurring)
            old = db.get(e["id"])
            e.update(first_seen=old["first_seen"] if old else now, last_seen=now, gone=False)
            db[e["id"]] = e

    for v in venues.values():
        if not v.get("source"): continue
        print(f"{v['id']} ...")
        try:
            raws = [r for r in scrape_venue(v, cache) if in_range(r)]
        except Exception as ex:
            errors.append(f"{v['id']}: {type(ex).__name__}: {ex}"); continue
        prev = [e for e in db.values() if e["venue_id"] == v["id"] and not e["recurring"] and not e["gone"] and e["date"] >= TODAY.isoformat()]
        if not raws and prev:  # sieht nach kaputtem Scraper aus, nichts als "entfällt" markieren
            errors.append(f"{v['id']}: 0 Events gefunden, vorher {len(prev)}"); continue
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
