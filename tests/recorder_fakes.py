"""Fake recorder APIs for the per-rep connection tests: one httpx.MockTransport that answers as Fathom,
Fireflies, tl;dv and Granola, in the shapes the adapters were written from (research notes; the vendors'
public docs). Each API key is ONE account with its own meetings, so two reps' keys can list the same
meeting. Nothing leaves the process.

    fake = FakeRecorders()
    fake.account("fireflies", "key-a", email="asha@tessel.test", name="Asha Rao", meetings=[...])
    monkeypatch.setattr(connections, "TRANSPORT", fake.transport())
    fake.fail[("fireflies", "key-a")] = (429, {"retry-after": "120"})     # the next requests answer 429
"""
import json
import re
from urllib.parse import parse_qs

import httpx

FATHOM = "api.fathom.ai"
FIREFLIES = "api.fireflies.ai"
TLDV = "pasta.tldv.io"
GRANOLA = "public-api.granola.ai"


class FakeRecorders:
    def __init__(self, page_size: int = 50):
        self.accounts = {}                 # (kind, key) -> {"email", "name", "meetings": [...]}
        self.fail = {}                     # (kind, key) -> (status, headers)
        self.requests = []                 # (kind, key, method, path, params)
        self.page_size = page_size
        self.not_ready = set()             # meeting ids whose transcript is not there yet

    def account(self, kind, key, email=None, name=None, meetings=()):
        self.accounts[(kind, key)] = {"email": email, "name": name, "meetings": list(meetings)}
        return self.accounts[(kind, key)]

    def transport(self):
        return httpx.MockTransport(self.handler)

    # ---- dispatch ------------------------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        kind = {FATHOM: "fathom", FIREFLIES: "fireflies", TLDV: "tldv", GRANOLA: "granola"}.get(host)
        if kind is None:
            return httpx.Response(404, json={"error": "unknown host " + host})
        key = {"fathom": request.headers.get("x-api-key"), "tldv": request.headers.get("x-api-key")}.get(kind)
        if key is None:
            key = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        params = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        self.requests.append((kind, key, request.method, request.url.path, params))
        if (kind, key) in self.fail:
            status, headers = self.fail[(kind, key)]
            return httpx.Response(status, headers=headers, json={"error": "refused"})
        acct = self.accounts.get((kind, key))
        if acct is None:
            return httpx.Response(401, json={"error": "invalid api key"})
        return getattr(self, kind)(request, acct, params)

    def _page(self, items, start):
        return items[start:start + self.page_size], (start + self.page_size < len(items))

    # ---- Fathom ----------------------------------------------------------------------------------

    def fathom(self, request, acct, params):
        path = request.url.path
        if path == "/external/v1/meetings":
            items = [m for m in acct["meetings"] if not params.get("created_after")
                     or (m.get("created_at") or "") >= params["created_after"]]
            start = int(params.get("cursor") or 0)
            page, more = self._page(items, start)
            if params.get("include_transcript") != "true":
                page = [{k: v for k, v in m.items() if k != "transcript"} for m in page]
            return httpx.Response(200, json={"items": page, "limit": self.page_size,
                                             "next_cursor": str(start + self.page_size) if more else None})
        m = re.fullmatch(r"/external/v1/recordings/([^/]+)/transcript", path)
        if m:
            found = next((x for x in acct["meetings"] if str(x["recording_id"]) == m.group(1)), None)
            if found is None or m.group(1) in self.not_ready:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json={"transcript": found.get("transcript") or []})
        return httpx.Response(404, json={"error": path})

    # ---- Fireflies -------------------------------------------------------------------------------

    def fireflies(self, request, acct, params):
        body = json.loads(request.content or b"{}")
        query, variables = body.get("query") or "", body.get("variables") or {}
        if "user {" in query:
            return httpx.Response(200, json={"data": {"user": {"email": acct["email"], "name": acct["name"]}}})
        if "transcripts(" in query:
            assert "mine: true" in query, "the per-rep listing must ask for the key owner's own meetings"
            items = [{k: v for k, v in t.items() if k != "sentences"} for t in acct["meetings"]]
            skip, limit = int(variables.get("skip") or 0), int(variables.get("limit") or 50)
            return httpx.Response(200, json={"data": {"transcripts": items[skip:skip + limit]}})
        if "transcript(" in query:
            found = next((t for t in acct["meetings"] if t["id"] == variables.get("id")), None)
            if found is not None and found["id"] in self.not_ready:
                found = None
            return httpx.Response(200, json={"data": {"transcript": found}})
        return httpx.Response(400, json={"errors": [{"message": "unknown query"}]})

    # ---- tl;dv -----------------------------------------------------------------------------------

    def tldv(self, request, acct, params):
        path = request.url.path
        if path == "/v1alpha1/meetings":
            assert params.get("onlyParticipated") == "true", "the per-rep listing must ask for the rep's own meetings"
            size = int(params.get("pageSize") or 50)
            size = min(size, self.page_size)
            page_no = int(params.get("page") or 1)
            items = [{k: v for k, v in m.items() if k != "transcript"} for m in acct["meetings"]]
            pages = max(1, -(-len(items) // size))
            return httpx.Response(200, json={"page": page_no, "pages": pages, "total": len(items), "pageSize": size,
                                             "results": items[(page_no - 1) * size: page_no * size]})
        m = re.fullmatch(r"/v1alpha1/meetings/([^/]+)(/transcript)?", path)
        if m:
            found = next((x for x in acct["meetings"] if x["id"] == m.group(1)), None)
            if found is None:
                return httpx.Response(404, json={"error": "not found"})
            if m.group(2):
                if found["id"] in self.not_ready:
                    return httpx.Response(404, json={"error": "transcript not ready"})
                return httpx.Response(200, json={"id": "tr-" + found["id"], "meetingId": found["id"],
                                                 "data": found.get("transcript") or []})
            return httpx.Response(200, json={k: v for k, v in found.items() if k != "transcript"})
        return httpx.Response(404, json={"error": path})

    # ---- Granola ---------------------------------------------------------------------------------

    def granola(self, request, acct, params):
        path = request.url.path
        if path == "/v1/notes":
            items = [{k: v for k, v in n.items() if k != "transcript"} for n in acct["meetings"]]
            start = int(params.get("cursor") or 0)
            size = min(int(params.get("page_size") or 30), self.page_size)
            page = items[start:start + size]
            more = start + size < len(items)
            return httpx.Response(200, json={"notes": page, "hasMore": more, "cursor": str(start + size) if more else None})
        m = re.fullmatch(r"/v1/notes/([^/]+)", path)
        if m:
            found = next((x for x in acct["meetings"] if x["id"] == m.group(1)), None)
            if found is None:
                return httpx.Response(404, json={"error": "not found"})
            note = dict(found)
            if params.get("include") != "transcript" or found["id"] in self.not_ready:
                note.pop("transcript", None)
            return httpx.Response(200, json=note)
        return httpx.Response(404, json={"error": path})


# ---- meetings in each API's shape -----------------------------------------------------------------

def fathom_meeting(rid, owner_email, owner_name, buyer=("Chen Wu", "chen@buyer.example"), when="2026-09-24T10:00:00Z",
                   title="Buyer discovery"):
    return {
        "title": title, "meeting_title": title, "recording_id": rid, "created_at": when,
        "recording_start_time": when, "recording_end_time": when.replace("10:00", "10:30"),
        "recorded_by": {"name": owner_name, "email": owner_email},
        "calendar_invitees": [{"name": owner_name, "email": owner_email, "is_external": False},
                              {"name": buyer[0], "email": buyer[1], "is_external": True}],
        "transcript": [
            {"speaker": {"display_name": owner_name.split()[0] + " (host)", "matched_calendar_invitee_email": owner_email},
             "text": "Thanks for making time. I will send the pricing sheet by Friday.", "timestamp": "00:00:05"},
            {"speaker": {"display_name": buyer[0], "matched_calendar_invitee_email": buyer[1]},
             "text": "We lose two days on every dispute.", "timestamp": "00:00:40"},
        ],
    }


def fireflies_transcript(tid, speakers, buyer=("Chen Wu", "chen@buyer.example"), when=1790244000000,
                         title="Buyer discovery", organizer="chen@buyer.example", reps_emails=()):
    """speakers: [(name, text)] in order."""
    return {
        "id": tid, "title": title, "date": when, "duration": 30, "organizer_email": organizer, "host_email": organizer,
        "participants": [buyer[1], *reps_emails],
        "meeting_attendees": [{"displayName": buyer[0], "email": buyer[1], "name": buyer[0]}],
        "sentences": [{"index": i, "speaker_name": who, "text": text, "raw_text": text, "start_time": 5.0 * i,
                       "end_time": 5.0 * i + 4} for i, (who, text) in enumerate(speakers)],
        "summary": {"overview": "Discovery call."},
    }


def tldv_meeting(mid, owner_name, owner_email, buyer=("Chen Wu", "chen@buyer.example"),
                 when="2026-09-24T10:00:00Z", title="Buyer discovery"):
    return {"id": mid, "name": title, "happenedAt": when, "duration": 1800,
            "organizer": {"name": owner_name, "email": owner_email},
            "invitees": [{"name": buyer[0], "email": buyer[1]}],
            "transcript": [{"speaker": owner_name, "text": "I will send the pricing sheet by Friday.", "startTime": 5,
                            "endTime": 9},
                           {"speaker": buyer[0], "text": "We lose two days on every dispute.", "startTime": 10,
                            "endTime": 14}]}


def granola_note(nid, owner_name, owner_email, buyer=("Chen Wu", "chen@buyer.example"),
                 when="2026-09-24T10:00:00Z", title="Buyer discovery"):
    return {"id": nid, "title": title, "created_at": when, "owner": {"name": owner_name, "email": owner_email},
            "attendees": [{"name": owner_name, "email": owner_email}, {"name": buyer[0], "email": buyer[1]}],
            "calendar_event": {"event_title": title, "scheduled_start_time": when},
            "summary_text": "Discovery call.",
            "transcript": [{"speaker": {"source": "microphone", "attribution": "me"},
                            "text": "I will send the pricing sheet by Friday.", "start_time": 5, "end_time": 9},
                           {"speaker": {"source": "speaker", "attribution": "them"},
                            "text": "We lose two days on every dispute.", "start_time": 10, "end_time": 14}]}
