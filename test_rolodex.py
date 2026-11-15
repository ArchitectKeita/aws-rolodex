"""Rolodex app tests: DynamoDB is moto, no AWS, no network."""
import importlib
import json
import pathlib
import sys

import boto3
import pytest
from moto import mock_aws

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
KEY = "test-owner-key-123"


@pytest.fixture
def ctx(monkeypatch):
    for k, v in {"AWS_DEFAULT_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x",
                 "TABLE": "rolodex", "OWNER_KEY": KEY, "ROLODEX_NO_BOOT": "1"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("ECS_CONTAINER_METADATA_URI_V4", raising=False)
    with mock_aws():
        boto3.client("dynamodb").create_table(
            TableName="rolodex", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}])
        sys.path.insert(0, str(APP))
        sys.modules.pop("app", None)
        mod = importlib.import_module("app")
        sys.path.pop(0)
        mod.seed_if_empty()
        yield mod, mod.app.test_client()


def unlock(c):
    r = c.post("/api/unlock", json={"key": KEY})
    assert r.status_code == 200
    return c


def ids(c):
    return {p["name"]: p["id"] for p in c.get("/api/people").get_json()["people"]}


# ---------------- reading ----------------

def test_page_and_health(ctx):
    _, c = ctx
    assert c.get("/healthz").data == b"ok"
    r = c.get("/")
    assert r.status_code == 200 and b"ROLODEX" in r.data


def test_seed_runs_once(ctx):
    mod, c = ctx
    assert mod.seed_if_empty() is False          # a second container doesn't double the demo data
    j = c.get("/api/people").get_json()
    assert len(j["people"]) == len(mod.SEED) and j["intros"] == 1 and j["owner"] is False


def test_every_response_says_who_served_it(ctx):
    mod, c = ctx
    r = c.get("/api/people")
    assert r.headers["X-Served-By"].startswith(mod.ME["task"][:8])
    assert r.get_json()["served_by"]["task"] == mod.ME["task"]


def test_person_has_timeline_newest_first(ctx):
    _, c = ctx
    j = c.get(f"/api/people/{ids(c)['Marcus Reed']}").get_json()
    assert j["person"]["company"] == "Capital One"
    ats = [e["at"] for e in j["timeline"]]
    assert ats == sorted(ats, reverse=True) and any(e["kind"] == "intro" for e in j["timeline"])


def test_bad_or_missing_ids_are_404(ctx):
    _, c = ctx
    assert c.get("/api/people/../../etc").status_code == 404
    assert c.get("/api/people/aaaaaaaaaaaa").status_code == 404


# ---------------- the owner key ----------------

def test_writes_are_locked_without_the_key(ctx):
    _, c = ctx
    pid = ids(c)["Dana Whitfield"]
    assert c.post("/api/people", json={"name": "X"}).status_code == 401
    assert c.post(f"/api/people/{pid}/log", json={"kind": "text"}).status_code == 401
    assert c.post("/api/chaos", json={}).status_code == 401
    assert c.delete(f"/api/people/{pid}").status_code == 401


def test_wrong_key_then_rate_limit(ctx):
    _, c = ctx
    for _ in range(5):
        assert c.post("/api/unlock", json={"key": "nope"}).status_code == 403
    assert c.post("/api/unlock", json={"key": KEY}).status_code == 429     # even the right key, for 5 minutes


def test_forged_cookie_is_rejected(ctx):
    _, c = ctx
    c.set_cookie("rk", "0" * 64)
    assert c.post("/api/people", json={"name": "X"}).status_code == 401


def test_writes_must_be_json(ctx):
    _, c = ctx
    unlock(c)
    assert c.post("/api/people", data="name=X", content_type="application/x-www-form-urlencoded").status_code == 415


# ---------------- changing things ----------------

def test_add_person_validates_and_cleans(ctx):
    _, c = ctx
    unlock(c)
    assert c.post("/api/people", json={"name": "  "}).status_code == 400
    assert c.post("/api/people", json={"name": "A", "cadence": 3}).status_code == 400
    assert c.post("/api/people", json={"name": "A", "tags": {"x": 1}}).status_code == 400
    r = c.post("/api/people", json={"name": "  Ada   Lovelace ", "tags": ["Math", "math", " Engines "],
                                    "cadence": 60, "met": "Babbage's salon"})
    assert r.status_code == 201
    j = c.get(f"/api/people/{r.get_json()['id']}").get_json()
    assert j["person"]["name"] == "Ada Lovelace" and j["person"]["tags"] == ["engines", "math"]
    assert j["timeline"][0]["kind"] == "met" and j["timeline"][0]["text"] == "Babbage's salon"


def test_logging_contact_resets_the_clock_but_a_note_doesnt(ctx):
    _, c = ctx
    unlock(c)
    pid = ids(c)["Sam Okafor"]
    before = c.get(f"/api/people/{pid}").get_json()["person"]["last_contact"]
    c.post(f"/api/people/{pid}/log", json={"kind": "note", "text": "Thinking about Ashburn"})
    assert c.get(f"/api/people/{pid}").get_json()["person"]["last_contact"] == before
    c.post(f"/api/people/{pid}/log", json={"kind": "text"})
    j = c.get(f"/api/people/{pid}").get_json()
    assert j["person"]["last_contact"] > before and j["timeline"][0]["text"] == "Texted"
    assert c.post(f"/api/people/{pid}/log", json={"kind": "intro"}).status_code == 400
    assert c.post("/api/people/aaaaaaaaaaaa/log", json={"kind": "text"}).status_code == 404


def test_intro_lands_on_both_timelines(ctx):
    _, c = ctx
    unlock(c)
    p = ids(c)
    a, b = p["Priya Nair"], p["Chris Ortega"]
    assert c.post("/api/intro", json={"a": a, "b": a}).status_code == 400
    assert c.post("/api/intro", json={"a": a, "b": b, "why": "both SA-track"}).status_code == 200
    ta = c.get(f"/api/people/{a}").get_json()["timeline"][0]
    tb = c.get(f"/api/people/{b}").get_json()["timeline"][0]
    assert ta["text"] == "Introduced to Chris Ortega · both SA-track" and tb["with"] == a
    j = c.get("/api/people").get_json()
    assert j["intros"] == 2 and next(x for x in j["people"] if x["id"] == a)["intro_count"] == 1


def test_edit_and_delete(ctx):
    _, c = ctx
    unlock(c)
    pid = ids(c)["Tom Alvarez"]
    assert c.patch(f"/api/people/{pid}", json={"title": "Director of SRE"}).status_code == 200
    assert c.get(f"/api/people/{pid}").get_json()["person"]["title"] == "Director of SRE"
    assert c.patch("/api/people/aaaaaaaaaaaa", json={"title": "x"}).status_code == 404
    assert c.delete(f"/api/people/{pid}").status_code == 200
    assert c.get(f"/api/people/{pid}").status_code == 404


# ---------------- the fleet ----------------

def test_fleet_shows_heartbeats_and_marks_silent_ones(ctx):
    mod, c = ctx
    mod.heartbeat()
    mod.db.put_item(Item={"pk": "HB", "sk": "deadbeef0000", "az": "us-east-1b", "ip": "10.80.11.9", "state": "alive",
                          "started": "2026-01-01T00:00:00Z", "seen": "2026-01-01T00:00:00Z", "served": 3, "ttl": 1})
    tasks = {t["task"]: t for t in c.get("/api/fleet").get_json()["tasks"]}
    assert tasks[mod.ME["task"]]["state"] == "alive"
    assert "deadbeef0000" not in tasks                 # silent for ages: dropped, not shown as a ghost


def test_chaos_marks_itself_killed_then_exits(ctx, monkeypatch):
    mod, c = ctx
    unlock(c)
    exits = []
    monkeypatch.setattr(mod.threading, "Timer", lambda s, fn: type("T", (), {"start": lambda self: exits.append(s)})())
    r = c.post("/api/chaos", json={})
    assert r.status_code == 200 and r.get_json()["killed"]["task"] == mod.ME["task"] and exits == [0.6]
    me = next(t for t in c.get("/api/fleet").get_json()["tasks"] if t["task"] == mod.ME["task"])
    assert me["state"] == "killed"
