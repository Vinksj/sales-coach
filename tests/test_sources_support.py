"""Fixtures for the transcript-source tests. All text is invented; no customer words."""
import json

# The seller in conftest is "Maya Iyer" (maya@tessel.test). A recorder prints real names.
NAMED = """Maya Iyer: Thanks for making time, Arjun. Let me walk you through the three levers.
Arjun Kumar: Sure. Before that, our CFO will want to see the savings split by plant.
Maya Iyer: Understood. I'll send you the plant-wise breakdown by Friday.
Arjun Kumar: Okay. And I'll try to set up a meeting with our CFO and CEO, probably first week of next month.
Maya Iyer: Great. What happens if nothing changes on load planning this year?
Arjun Kumar: Hi chloral cara march salrat weather.
Maya Iyer: Right. So the pilot would start at Pant Nagar."""

STRANGERS = """Priya S.: Thanks for joining. I'll send the pricing sheet by Monday.
Dev Anand Rao: Good. We will review it with finance.
Priya S.: Perfect."""

OTTER = """Asha Rao  0:03
Good afternoon. Can you hear me?
We had some trouble with the line.

Maya Iyer  0:12
Loud and clear. Note: I'll send the deck tomorrow.

Jean-Luc O'Neil  1:05:40
Bonjour, I am joining late.
"""

VTT = """WEBVTT

NOTE exported by a recorder

1
00:00:01.000 --> 00:00:04.200
<v Asha Rao>Good afternoon,</v>

2
00:00:04.200 --> 00:00:06.000
<v Asha Rao>can you hear me?</v>

3
00:00:06.500 --> 00:00:09.000
<v.loud Maya Iyer>Loud &amp; clear.</v>

00:01:10.000 --> 00:01:12.000
Dr. A. K. Sharma: We need the numbers <i>by site</i>.
"""

SRT = """1
00:00:01,000 --> 00:00:03,000
Asha Rao: Good afternoon.

2
00:00:03,500 --> 00:00:07,250
Maya Iyer: Thanks. I will share
the proposal on Tuesday.

3
01:02:03,000 --> 01:02:05,000
[María-José Núñez] Gracias a todos.
"""

FIREFLIES = {
    "id": "ff-01HZX", "title": "Acme <> Tessel weekly", "date": 1788343200000, "duration": 31.5,
    "organizer_email": "maya@tessel.test", "participants": ["maya@tessel.test", "asha.rao@acmefreight.test"],
    "meeting_attendees": [{"displayName": "Maya Iyer", "email": "maya@tessel.test", "name": None},
                          {"displayName": "Asha Rao", "email": "asha.rao@acmefreight.test", "name": None}],
    "sentences": [
        {"index": 0, "speaker_name": "Asha Rao", "text": "Good afternoon.", "raw_text": "good afternoon",
         "start_time": 1.2, "end_time": 2.4},
        {"index": 1, "speaker_name": "Asha Rao", "text": "We need lane data first.", "raw_text": "",
         "start_time": 2.5, "end_time": 5.0},
        {"index": 2, "speaker_name": "Maya Iyer", "text": "I'll send the lane template by Thursday.",
         "raw_text": "", "start_time": 5.5, "end_time": 9.0},
        {"index": 3, "speaker_name": "Asha Rao", "text": "Fine.", "raw_text": "", "start_time": 9.2, "end_time": 9.8},
    ],
    "summary": {"overview": "Asha wants lane data first."},
}

FATHOM_MEETING = {
    "title": "Acme pilot review", "meeting_title": "Acme pilot review", "recording_id": 4471203,
    "url": "https://fathom.video/calls/4471203", "created_at": "2026-09-16T10:05:00Z",
    "scheduled_start_time": "2026-09-16T09:30:00Z", "scheduled_end_time": "2026-09-16T10:00:00Z",
    "recording_start_time": "2026-09-16T09:31:10Z", "recording_end_time": "2026-09-16T10:02:00Z",
    "calendar_invitees": [{"name": "Maya Iyer", "email": "maya@tessel.test", "is_external": False},
                          {"name": "Asha Rao", "email": "asha.rao@acmefreight.test", "is_external": True}],
    "recorded_by": {"name": "Maya Iyer", "email": "maya@tessel.test"},
    "default_summary": {"template_name": "general", "markdown_formatted": "## Summary\nPilot scope agreed."},
}
FATHOM_TRANSCRIPT = [
    {"speaker": {"display_name": "Asha Rao", "matched_calendar_invitee_email": "asha.rao@acmefreight.test"},
     "text": "Let us agree the pilot scope.", "timestamp": "00:00:04"},
    {"speaker": {"display_name": "S. Jain", "matched_calendar_invitee_email": "maya@tessel.test"},
     "text": "I'll send the scope note by Wednesday.", "timestamp": "00:00:11"},
    {"speaker": {"display_name": "Asha Rao", "matched_calendar_invitee_email": "asha.rao@acmefreight.test"},
     "text": "Good.", "timestamp": "00:01:02"},
]

GENERIC = {
    "id": "zap-7781", "source": "otter", "title": "Acme discovery", "started_at": "2026-09-16T15:00:00+05:30",
    "participants": [{"name": "Asha Rao", "email": "asha.rao@acmefreight.test"},
                     {"name": "Maya Iyer", "email": "maya@tessel.test"}],
    "turns": [{"speaker": "Asha Rao", "text": "We lose two days on every detention dispute.", "start": 3, "end": 9.5},
              {"speaker": "Maya Iyer", "text": "I'll send a one-page plan by Friday.", "start": "0:10", "end": "0:15"},
              {"speaker": "Asha Rao", "text": "Please do.", "start": 16}],
}


def generic_bytes(**over) -> bytes:
    return json.dumps({**GENERIC, **over}).encode()
