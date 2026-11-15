"""Rolodex: a contacts app for someone who knows everybody, running as containers on ECS Fargate.

Two jobs in one process:
  1. The app: people, notes, a timeline per person, intros, and who's "going cold".
  2. The show: every container reports a heartbeat (task ID, availability zone, requests served)
     so the page can draw the live fleet, show which container answered you, and let the owner
     kill one on purpose to watch ECS replace it.

Reading is public (the contacts are fictional demo data). Changing anything needs the owner key,
which ECS injects from SSM Parameter Store; it is never in the image or the code.
"""
import hashlib
import hmac
import json
import os
import re
import signal
import threading
import time
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from flask import Flask, jsonify, request, send_from_directory

TABLE = os.environ.get("TABLE", "rolodex")
OWNER_KEY = os.environ.get("OWNER_KEY", "")
HEARTBEAT_SECONDS = int(os.environ.get("HEARTBEAT_SECONDS", "5"))
COOKIE = "rk"
MAX = {"name": 80, "title": 80, "company": 80, "city": 60, "notes": 1200, "text": 280, "tag": 24}
KINDS = {"met", "text", "email", "call", "coffee", "logged", "intro", "note"}

app = Flask(__name__, static_folder=None)
app.json.sort_keys = False
db = boto3.resource("dynamodb").Table(TABLE)
HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------- who am I (ECS task metadata) ----------------

def task_identity():
    """Read this container's task ID, AZ and private IP from the ECS metadata endpoint."""
    uri = os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
    if uri:
        try:
            with urllib.request.urlopen(uri + "/task", timeout=2) as r:
                meta = json.load(r)
            ip = ""
            for c in meta.get("Containers", []):
                for n in c.get("Networks", []):
                    ip = (n.get("IPv4Addresses") or [""])[0] or ip
            return {"task": meta["TaskARN"].rsplit("/", 1)[-1], "az": meta.get("AvailabilityZone", "?"), "ip": ip}
        except Exception as e:
            print(f"metadata unavailable: {e}")
    return {"task": "local" + uuid.uuid4().hex[:7], "az": os.environ.get("LOCAL_AZ", "local"), "ip": "127.0.0.1"}


ME = task_identity()
STARTED = datetime.now(timezone.utc)
served = 0
served_lock = threading.Lock()


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def heartbeat(state="alive"):
    db.put_item(Item={
        "pk": "HB", "sk": ME["task"], "az": ME["az"], "ip": ME["ip"], "state": state,
        "started": STARTED.strftime("%Y-%m-%dT%H:%M:%SZ"), "seen": now_iso(),
        "served": served, "ttl": int(time.time()) + 600,
    })


def heartbeat_loop():
    while True:
        try:
            heartbeat()
        except Exception as e:
            print(f"heartbeat failed: {type(e).__name__}: {e}")
        time.sleep(HEARTBEAT_SECONDS)


# ---------------- owner key ----------------

def owner_token():
    return hmac.new(OWNER_KEY.encode(), b"rolodex-owner-v1", hashlib.sha256).hexdigest() if OWNER_KEY else ""


def is_owner():
    tok = request.cookies.get(COOKIE, "")
    return bool(OWNER_KEY) and bool(tok) and hmac.compare_digest(tok, owner_token())


def needs_owner():
    # JSON-only writes + SameSite=Strict cookie = a form on another site can't act as the owner.
    if not is_owner():
        return jsonify(error="locked: unlock with the owner key first"), 401
    if not request.is_json:
        return jsonify(error="send JSON"), 415
    return None


attempts = {}


@app.post("/api/unlock")
def unlock():
    # X-Forwarded-For is "<anything the client sent>, <viewer IP added by CloudFront>, <edge IP added by the ALB>".
    hops = [h.strip() for h in request.headers.get("X-Forwarded-For", "").split(",") if h.strip()]
    ip = hops[-2] if len(hops) >= 2 else (hops[0] if hops else request.remote_addr or "?")
    recent = [t for t in attempts.get(ip, []) if t > time.time() - 300]
    if len(recent) >= 5:
        return jsonify(error="too many tries; wait five minutes"), 429
    key = (request.get_json(silent=True) or {}).get("key", "")
    if not OWNER_KEY or not isinstance(key, str) or not hmac.compare_digest(key.encode(), OWNER_KEY.encode()):
        attempts[ip] = recent + [time.time()]
        return jsonify(error="wrong key"), 403
    attempts.pop(ip, None)
    r = jsonify(ok=True)
    r.set_cookie(COOKIE, owner_token(), max_age=60 * 60 * 24 * 14, httponly=True,
                 secure=os.environ.get("COOKIE_SECURE") == "1",   # set in ECS: viewers only ever reach it over HTTPS
                 samesite="Strict")
    return r


@app.post("/api/lock")
def lock():
    r = jsonify(ok=True)
    r.delete_cookie(COOKIE)
    return r


# ---------------- every response says who served it ----------------

@app.before_request
def count():
    global served
    if request.path != "/healthz":
        with served_lock:
            served += 1


@app.after_request
def stamp(resp):
    resp.headers["X-Served-By"] = f'{ME["task"][:8]} {ME["az"]}'
    resp.headers["Cache-Control"] = resp.headers.get("Cache-Control", "no-store")
    return resp


# ---------------- pages ----------------

@app.get("/")
def page():
    return send_from_directory(os.path.join(HERE, "static"), "index.html", max_age=0)


@app.get("/healthz")
def healthz():
    return "ok", 200, {"Content-Type": "text/plain"}


# ---------------- people ----------------

def clean_str(v, limit):
    if v is None:
        return ""
    if not isinstance(v, str):
        raise ValueError("expected text")
    return re.sub(r"\s+", " ", v).strip()[:limit] if limit < 300 else v.strip()[:limit]


def clean_person(body, partial=False):
    out = {}
    for f in ("name", "title", "company", "city", "notes"):
        if f in body or not partial:
            out[f] = clean_str(body.get(f), MAX[f])
    if not partial and not out["name"]:
        raise ValueError("a name is required")
    if "tags" in body or not partial:
        tags = body.get("tags") or []
        if isinstance(tags, str):
            tags = tags.split(",")
        if not isinstance(tags, list):
            raise ValueError("tags must be a list")
        out["tags"] = sorted({clean_str(t, MAX["tag"]).lower() for t in tags if isinstance(t, str) and t.strip()})[:8]
    if "cadence" in body or not partial:
        c = body.get("cadence", 30)
        if not isinstance(c, int) or isinstance(c, bool) or not 7 <= c <= 365:
            raise ValueError("cadence is days between check-ins, 7 to 365")
        out["cadence"] = c
    return out


def public(p):
    return {k: (int(v) if isinstance(v, Decimal) else v) for k, v in p.items() if k not in ("pk", "sk")}


def load_all():
    items, kw = [], {}
    while True:
        r = db.scan(**kw)
        items += r["Items"]
        if "LastEvaluatedKey" not in r:
            return items
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


@app.get("/api/people")
def people():
    items = load_all()
    per = {}
    for i in items:
        if i["pk"].startswith("C#") and i["sk"].startswith("E#") and i.get("kind") == "intro":
            per[i["pk"]] = per.get(i["pk"], 0) + 1
    ppl = [public(i) | {"intro_count": per.get(i["pk"], 0)} for i in items if i["pk"].startswith("C#") and i["sk"] == "PROFILE"]
    intros = sum(per.values()) // 2
    ppl.sort(key=lambda p: p.get("last_contact", ""), reverse=True)
    return jsonify(people=ppl, intros=intros, owner=is_owner(), today=date.today().isoformat(), served_by=ME)


@app.get("/api/people/<pid>")
def person(pid):
    if not re.fullmatch(r"[a-f0-9]{12}", pid):
        return jsonify(error="not found"), 404
    r = db.query(KeyConditionExpression=Key("pk").eq(f"C#{pid}"), ScanIndexForward=False)
    prof = next((i for i in r["Items"] if i["sk"] == "PROFILE"), None)
    if not prof:
        return jsonify(error="not found"), 404
    events = [public(i) | {"at": i["sk"][2:]} for i in r["Items"] if i["sk"].startswith("E#")]
    return jsonify(person=public(prof), timeline=events, owner=is_owner(), served_by=ME)


def add_event(pid, kind, text, when=None):
    # millisecond timestamps keep two entries logged in the same second in the order they happened
    at = when or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:23] + "Z"
    db.put_item(Item={"pk": f"C#{pid}", "sk": f"E#{at}#{uuid.uuid4().hex[:4]}", "kind": kind, "text": text})
    return at


@app.post("/api/people")
def create():
    if (deny := needs_owner()):
        return deny
    try:
        p = clean_person(request.get_json())
    except ValueError as e:
        return jsonify(error=str(e)), 400
    pid = uuid.uuid4().hex[:12]
    today = date.today().isoformat()
    met = clean_str((request.get_json() or {}).get("met"), MAX["text"]) or "Added to the Rolodex"
    db.put_item(Item={"pk": f"C#{pid}", "sk": "PROFILE", "id": pid, **p, "last_contact": today, "added": today})
    add_event(pid, "met", met)
    return jsonify(id=pid), 201


@app.patch("/api/people/<pid>")
def edit(pid):
    if (deny := needs_owner()):
        return deny
    try:
        p = clean_person(request.get_json(), partial=True)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    if not p:
        return jsonify(error="nothing to change"), 400
    names = {f"#{k}": k for k in p}
    try:
        db.update_item(Key={"pk": f"C#{pid}", "sk": "PROFILE"},
                       UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in p),
                       ExpressionAttributeNames=names, ExpressionAttributeValues={f":{k}": v for k, v in p.items()},
                       ConditionExpression="attribute_exists(pk)")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return jsonify(error="not found"), 404
        raise
    return jsonify(ok=True)


@app.post("/api/people/<pid>/log")
def log_touch(pid):
    if (deny := needs_owner()):
        return deny
    body = request.get_json()
    kind = body.get("kind", "logged")
    if kind not in KINDS - {"intro", "met"}:
        return jsonify(error="unknown kind"), 400
    try:
        text = clean_str(body.get("text"), MAX["text"]) or {"text": "Texted", "email": "Emailed", "call": "Called",
                                                             "coffee": "Got coffee", "note": "Note"}.get(kind, "Checked in")
    except ValueError as e:
        return jsonify(error=str(e)), 400
    try:
        if kind != "note":   # a note isn't contact; it shouldn't reset the clock
            db.update_item(Key={"pk": f"C#{pid}", "sk": "PROFILE"}, UpdateExpression="SET last_contact = :d",
                           ExpressionAttributeValues={":d": date.today().isoformat()},
                           ConditionExpression="attribute_exists(pk)")
        elif not db.get_item(Key={"pk": f"C#{pid}", "sk": "PROFILE"}).get("Item"):
            return jsonify(error="not found"), 404
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return jsonify(error="not found"), 404
        raise
    add_event(pid, kind, text)
    return jsonify(ok=True)


@app.post("/api/intro")
def intro():
    """Introduce two people: one timeline entry on each, both count as contact."""
    if (deny := needs_owner()):
        return deny
    body = request.get_json()
    a, b = body.get("a"), body.get("b")
    if not (isinstance(a, str) and isinstance(b, str)) or a == b:
        return jsonify(error="pick two different people"), 400
    profs = {}
    for pid in (a, b):
        it = db.get_item(Key={"pk": f"C#{pid}", "sk": "PROFILE"}).get("Item") if re.fullmatch(r"[a-f0-9]{12}", pid) else None
        if not it:
            return jsonify(error="not found"), 404
        profs[pid] = it
    why = clean_str(body.get("why"), MAX["text"])
    today = date.today().isoformat()
    with db.batch_writer() as w:
        for me, other in ((a, b), (b, a)):
            w.put_item(Item={"pk": f"C#{me}", "sk": f"E#{now_iso()}#{uuid.uuid4().hex[:4]}", "kind": "intro",
                             "text": f'Introduced to {profs[other]["name"]}' + (f" · {why}" if why else ""), "with": other})
    for pid in (a, b):
        db.update_item(Key={"pk": f"C#{pid}", "sk": "PROFILE"}, UpdateExpression="SET last_contact = :d",
                       ExpressionAttributeValues={":d": today})
    return jsonify(ok=True)


@app.delete("/api/people/<pid>")
def remove(pid):
    if not is_owner():
        return jsonify(error="locked"), 401
    r = db.query(KeyConditionExpression=Key("pk").eq(f"C#{pid}"))
    if not r["Items"]:
        return jsonify(error="not found"), 404
    with db.batch_writer() as w:
        for i in r["Items"]:
            w.delete_item(Key={"pk": i["pk"], "sk": i["sk"]})
    return jsonify(ok=True)


# ---------------- the fleet ----------------

@app.get("/api/fleet")
def fleet():
    r = db.query(KeyConditionExpression=Key("pk").eq("HB"))
    now = datetime.now(timezone.utc)
    tasks = []
    for i in r["Items"]:
        age = (now - datetime.strptime(i["seen"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds()
        state = i.get("state", "alive")
        if state == "alive" and age > HEARTBEAT_SECONDS * 4:
            state = "silent"
        if state != "alive" and age > 180:
            continue     # long gone; ECS has already replaced it
        tasks.append({"task": i["sk"], "az": i["az"], "ip": i.get("ip", ""), "state": state,
                      "started": i["started"], "served": int(i.get("served", 0)), "age": int(age)})
    tasks.sort(key=lambda t: (t["state"] != "alive", t["az"], t["started"]))
    return jsonify(tasks=tasks, served_by=ME, owner=is_owner(), at=now_iso())


def die():
    """Take the whole container down, not just this worker: gunicorn would quietly respawn a worker.
    SIGINT tells the gunicorn master (PID 1 in the container) to quit at once; the container exits and ECS
    has to launch a replacement to get back to the desired count."""
    try:
        os.kill(os.getppid(), signal.SIGINT)
    finally:
        os._exit(1)


@app.post("/api/chaos")
def chaos():
    """Kill whichever container answers this request. ECS notices and launches a replacement."""
    if (deny := needs_owner()):
        return deny
    try:
        heartbeat(state="killed")
    except Exception:
        pass
    print(json.dumps({"event": "chaos", "task": ME["task"], "az": ME["az"]}))
    threading.Timer(0.6, die).start()
    return jsonify(killed=ME)


# ---------------- demo data ----------------

SEED = [
    ("Marcus Reed", "Cloud Engineering Manager", "Capital One", "McLean, VA", ["hiring", "cloud", "fintech"], 30, 34,
     "Met at the AWS DC meetup in Arlington. His team is hiring cloud ops in Q1. Big Formula 1 fan.",
     [("met", "Met at the AWS DC meetup", 58), ("email", "Sent portfolio link", 50)]),
    ("Dana Whitfield", "Technical Recruiter", "Navy Federal", "Vienna, VA", ["recruiter", "fintech"], 30, 41,
     "Handles infrastructure roles. Prefers email over LinkedIn.", [("met", "Connected after a career fair", 70)]),
    ("Sam Okafor", "Data Center Operations Lead", "Equinix", "Ashburn, VA", ["data center", "ashburn"], 45, 52,
     "Knows everyone in Data Center Alley. Plays pickup basketball Sundays.", [("met", "Met through a friend's cookout", 90)]),
    ("Priya Nair", "Solutions Architect", "AWS", "Herndon, VA", ["aws", "mentor"], 21, 2,
     "Offered to review my Terraform. Ask about the SA associate exam.", [("met", "Met at re:Invent watch party", 2)]),
    ("Lena Park", "Platform Engineer", "Fannie Mae", "Reston, VA", ["cloud", "terraform"], 30, 4,
     "Runs their internal Terraform modules. Into film photography.", [("coffee", "Coffee in Reston", 4)]),
    ("Andre Coleman", "Founder", "Coleman Events", "Washington, DC", ["founder", "events"], 60, 12,
     "Throws the best networking events in the city. Knows half the DMV.", [("met", "Met at his rooftop mixer", 40)]),
    ("Kia Johnson", "IT Support Analyst", "Freelance", "Bowie, MD", ["friend", "job search"], 30, 12,
     "Looking to move into a tech role. Sharp; reliable.", [("text", "Checked in about her search", 12)]),
    ("Tom Alvarez", "SRE Manager", "GEICO", "Chevy Chase, MD", ["hiring", "sre"], 30, 37,
     "Said to ping him when I finish the SAA. Coaches youth soccer.", [("met", "Met at a Terraform user group", 37)]),
    ("Grace Liu", "Director of Infrastructure", "Marriott", "Bethesda, MD", ["leadership", "cloud"], 60, 20,
     "Moving their workloads to containers this year.", [("email", "Thank-you note after her talk", 20)]),
    ("Jordan Blake", "DevOps Engineer", "Booz Allen", "McLean, VA", ["devops", "peer"], 30, 9,
     "Trades CI/CD war stories. Into vinyl.", [("call", "Talked GitHub Actions for an hour", 9)]),
    ("Nadia Rahman", "Talent Partner", "Insight Global", "Washington, DC", ["recruiter", "agency"], 21, 26,
     "Places cloud support roles quickly. Wants my updated resume.", [("met", "Intro call", 26)]),
    ("Chris Ortega", "Cloud Architect", "Freddie Mac", "McLean, VA", ["cloud", "architect"], 45, 15,
     "Studied aerospace too. Offered a mock interview.", [("coffee", "Lunch in Tysons", 15)]),
]


def seed_if_empty():
    try:
        db.put_item(Item={"pk": "SEED", "sk": "v1", "at": now_iso()}, ConditionExpression="attribute_not_exists(pk)")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False      # another container already seeded
        raise
    ids = {}
    with db.batch_writer() as w:
        for name, title, company, city, tags, cadence, days_ago, notes, events in SEED:
            pid = hashlib.sha1(name.encode()).hexdigest()[:12]
            ids[name] = pid
            last = (date.today() - timedelta(days=days_ago)).isoformat()
            w.put_item(Item={"pk": f"C#{pid}", "sk": "PROFILE", "id": pid, "name": name, "title": title,
                             "company": company, "city": city, "tags": sorted(tags), "cadence": cadence,
                             "notes": notes, "last_contact": last, "added": last, "demo": True})
            for kind, text, ago in events:
                at = (datetime.now(timezone.utc) - timedelta(days=ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
                w.put_item(Item={"pk": f"C#{pid}", "sk": f"E#{at}#seed", "kind": kind, "text": text})
        at = (datetime.now(timezone.utc) - timedelta(days=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for me, other in (("Kia Johnson", "Marcus Reed"), ("Marcus Reed", "Kia Johnson")):
            w.put_item(Item={"pk": f"C#{ids[me]}", "sk": f"E#{at}#intr", "kind": "intro",
                             "text": f"Introduced to {other} · looking for a tech role", "with": ids[other]})
    return True


def boot():
    try:
        if seed_if_empty():
            print("seeded demo contacts")
    except Exception as e:
        print(f"seed skipped: {type(e).__name__}: {e}")
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    print(json.dumps({"event": "start", **ME, "owner_key_set": bool(OWNER_KEY)}))


if os.environ.get("ROLODEX_NO_BOOT") != "1":
    boot()
