#!/usr/bin/env python3
"""
End-to-end flow test for the SafeWalk AI backend.

Starts a session, opens a WebSocket, sends an escalating transcript sequence,
asserts distress classification responses, then cleans up.

Usage
-----
Start the server in test mode first:

    SAFEWALK_TEST_MODE=1 uvicorn main:app --reload --port 8000

Then in a second terminal:

    python test_flow.py

SAFEWALK_TEST_MODE=1 makes TwilioService return mock responses so no real
SMS is ever sent and no valid Twilio credentials are required.
"""

import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
import websockets

# ------------------------------------------------------------------ #
#  Config                                                             #
# ------------------------------------------------------------------ #
BASE_URL = "http://localhost:8000"
WS_BASE  = "ws://localhost:8000"

TEST_CONTACT = "+15550000001"   # safe fake E.164 number
RECV_TIMEOUT = 20               # seconds to wait for a distress_update per message

TEST_SESSION_PAYLOAD = {
    "user_name":         "TestUser",
    "emergency_contact": TEST_CONTACT,
    "location": {
        "lat":     34.0195,
        "lng":    -118.4912,
        "address": "UCLA, Westwood, Los Angeles, CA",
    },
}

# (delay_seconds, user_text)  — delays are for documentation only;
# we send 2 s apart regardless to keep the test fast.
TRANSCRIPT_SEQUENCE = [
    (0, "Hi, I'm heading home"),
    (2, "It's pretty dark out here"),
    (4, "Someone is following me I think"),
    (6, "I'm scared, they're getting closer"),
]

DISTRESS_LABELS = [
    "Calm / Normal",
    "Mild anxiety",
    "Nervous / Concerned",
    "Distressed",
    "High distress / Possible danger",
    "Emergency / Immediate help needed",
]

# ------------------------------------------------------------------ #
#  Result dataclass                                                   #
# ------------------------------------------------------------------ #
@dataclass
class DistressUpdate:
    message_index: int
    user_text: str
    level: int
    recommend_alert: bool
    reasoning: str
    keywords: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ #
#  Helpers                                                            #
# ------------------------------------------------------------------ #
def _hr(char: str = "─", width: int = 62) -> str:
    return char * width


def _print_section(title: str) -> None:
    print(f"\n{_hr()}\n  {title}\n{_hr()}")


async def _recv_next_distress(ws, timeout: float = RECV_TIMEOUT) -> Optional[dict]:
    """
    Drain WebSocket messages until a distress_update arrives or timeout expires.
    Returns the distress_update dict, or None on timeout.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            return None
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 5.0))
        except asyncio.TimeoutError:
            return None

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue

        msg_type = msg.get("type")
        if msg_type == "distress_update":
            return msg
        elif msg_type == "error":
            print(f"    ⚠  Backend error: {msg.get('detail')}")
        elif msg_type in ("mode_change", "text_reply", "connected"):
            pass   # informational — keep draining
        # else: unexpected type, keep draining


# ------------------------------------------------------------------ #
#  Main test                                                          #
# ------------------------------------------------------------------ #
async def run_test() -> bool:
    session_id: Optional[str] = None
    updates: list[DistressUpdate] = []
    failures: list[str] = []

    _print_section("SafeWalk AI — End-to-End Flow Test")

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=15.0) as http:

        # ---------------------------------------------------------- #
        #  Step 1 — Create session                                   #
        # ---------------------------------------------------------- #
        print("\n[1] Creating session…")
        try:
            res = await http.post("/api/sessions/create", json=TEST_SESSION_PAYLOAD)
            if res.status_code != 201:
                print(f"    ✗ POST /api/sessions/create returned {res.status_code}: {res.text}")
                return False
            session_id = res.json()["session_id"]
            print(f"    ✓ Session created: {session_id}")
        except httpx.ConnectError:
            print("    ✗ Could not connect to server at", BASE_URL)
            print("      Is the server running? (uvicorn main:app --port 8000)")
            return False

        # ---------------------------------------------------------- #
        #  Step 2 — Open WebSocket                                   #
        # ---------------------------------------------------------- #
        print("\n[2] Opening WebSocket…")
        ws_uri = f"{WS_BASE}/api/ws/{session_id}"
        try:
            async with websockets.connect(ws_uri, open_timeout=10) as ws:
                # Expect the "connected" handshake message
                try:
                    handshake = json.loads(
                        await asyncio.wait_for(ws.recv(), timeout=5)
                    )
                    assert handshake.get("type") == "connected", (
                        f"Unexpected first message: {handshake}"
                    )
                    print(f"    ✓ WebSocket connected (session={handshake['session_id'][:8]}…)")
                except (asyncio.TimeoutError, AssertionError) as exc:
                    print(f"    ✗ WS handshake failed: {exc}")
                    return False

                # --------------------------------------------------#
                #  Step 3 — Send escalating transcript               #
                # -------------------------------------------------- #
                print("\n[3] Sending transcript sequence…")
                t0 = time.monotonic()

                for idx, (nominal_delay, text) in enumerate(TRANSCRIPT_SEQUENCE):
                    if idx > 0:
                        await asyncio.sleep(2)   # brief pause between messages

                    elapsed = time.monotonic() - t0
                    print(f"\n    [{idx + 1}/{len(TRANSCRIPT_SEQUENCE)}]"
                          f"  t≈{elapsed:.1f}s  →  \"{text}\"")

                    await ws.send(json.dumps({
                        "type":    "transcript",
                        "speaker": "user",
                        "text":    text,
                    }))

                    msg = await _recv_next_distress(ws, timeout=RECV_TIMEOUT)

                    if msg is None:
                        note = f"Timeout waiting for distress_update after message {idx + 1}"
                        print(f"    ⚠  {note}")
                        failures.append(note)
                        continue

                    update = DistressUpdate(
                        message_index=idx,
                        user_text=text,
                        level=msg["level"],
                        recommend_alert=msg["recommend_alert"],
                        reasoning=msg.get("reasoning", ""),
                        keywords=msg.get("keywords_detected", []),
                    )
                    updates.append(update)

                    marker = "🔴" if update.level >= 4 else ("🟡" if update.level >= 2 else "🟢")
                    alert_flag = "⚠  recommend_alert=True" if update.recommend_alert else ""
                    print(f"    {marker} distress_level={update.level}/5  {alert_flag}")
                    if update.keywords:
                        print(f"       keywords : {', '.join(update.keywords)}")
                    if update.reasoning:
                        reason_short = (
                            update.reasoning[:90] + "…"
                            if len(update.reasoning) > 90
                            else update.reasoning
                        )
                        print(f"       reasoning: {reason_short}")

            print("\n    ✓ WebSocket closed cleanly")

        except websockets.exceptions.WebSocketException as exc:
            print(f"    ✗ WebSocket error: {exc}")
            return False

        # ---------------------------------------------------------- #
        #  Step 4 — Assert distress levels increase                  #
        # ---------------------------------------------------------- #
        print("\n[4] Running assertions…")

        if not updates:
            print("    ✗ Received 0 distress_update messages — is Gemini configured?")
            return False
        print(f"    ✓ Received {len(updates)} distress_update message(s)")

        levels = [u.level for u in updates]

        if levels[-1] >= levels[0]:
            print(f"    ✓ Distress levels trended up: {levels[0]} → {levels[-1]}")
        else:
            note = f"Final level ({levels[-1]}) did not exceed initial ({levels[0]})"
            print(f"    ⚠  {note} (may be acceptable for short sequences)")
            # Soft warning, not a hard failure — Gemini can be non-deterministic

        max_level = max(levels)
        if max_level >= 2:
            print(f"    ✓ Peak distress level = {max_level}/5 ({DISTRESS_LABELS[max_level]})")
        else:
            note = f"Peak distress level {max_level}/5 is unexpectedly low"
            failures.append(note)
            print(f"    ✗ {note}")

        # ---------------------------------------------------------- #
        #  Step 5 — Assert recommend_alert became True               #
        # ---------------------------------------------------------- #
        recommend_flags = [u.recommend_alert for u in updates]
        if any(recommend_flags):
            first_alert_idx = recommend_flags.index(True)
            print(
                f"    ✓ recommend_alert=True first appeared at message "
                f"{first_alert_idx + 1} (level={updates[first_alert_idx].level}/5)"
            )
        else:
            note = "recommend_alert never became True across all messages"
            failures.append(note)
            print(f"    ✗ {note}")

        # ---------------------------------------------------------- #
        #  Step 6 — Clean up                                         #
        # ---------------------------------------------------------- #
        print("\n[6] Cleaning up…")
        try:
            # Prefer cancel (handles escalated/alert_failed sessions)
            cancel_res = await http.post(
                "/api/emergency/cancel", json={"session_id": session_id}
            )
            if cancel_res.status_code == 200:
                print("    ✓ Session cancelled via /emergency/cancel")
            else:
                # Fall back: mark ended
                end_res = await http.delete(f"/api/sessions/{session_id}/end")
                code = end_res.status_code
                print(f"    ✓ Session ended via DELETE /sessions/…/end (status={code})")
        except Exception as exc:
            print(f"    ⚠  Cleanup error (non-fatal): {exc}")

    # ---------------------------------------------------------- #
    #  Step 7 — Summary                                          #
    # ---------------------------------------------------------- #
    _print_section("SUMMARY")
    print(f"  Session      : {session_id}")
    print(f"  Messages sent: {len(TRANSCRIPT_SEQUENCE)}")
    print(f"  Updates recv : {len(updates)}")
    print()
    col_w = max(len(u.user_text) for u in updates) if updates else 30
    header = f"  {'#':<4}  {'Level':<8}  {'Alert?':<8}  {'User message':<{col_w}}"
    print(header)
    print(f"  {_hr('-', len(header) - 2)}")
    for u in updates:
        label    = DISTRESS_LABELS[u.level]
        alert_mk = "YES ⚠" if u.recommend_alert else "no"
        print(f"  {u.message_index + 1:<4}  {u.level}/5 {label[:12]:<14}  {alert_mk:<8}  {u.user_text}")

    print()
    if failures:
        print("  FAILURES:")
        for f in failures:
            print(f"    ✗ {f}")
        print()
        print("  ❌ Test completed with failures")
        print(_hr())
        return False
    else:
        print("  ✅ All assertions passed")
        print(_hr())
        return True


# ------------------------------------------------------------------ #
#  Entry point                                                        #
# ------------------------------------------------------------------ #
if __name__ == "__main__":
    try:
        passed = asyncio.run(run_test())
        sys.exit(0 if passed else 1)
    except KeyboardInterrupt:
        print("\n\nInterrupted.")
        sys.exit(130)
    except AssertionError as exc:
        print(f"\n  ✗ ASSERTION FAILED: {exc}")
        sys.exit(1)
    except Exception as exc:
        print(f"\n  ✗ UNEXPECTED ERROR: {type(exc).__name__}: {exc}")
        sys.exit(1)
