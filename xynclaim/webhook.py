"""Validate Discord destinations and send redacted run summaries."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import urllib.error
import urllib.parse
import urllib.request


ALLOWED_WEBHOOK_HOSTS = {"discord.com", "canary.discord.com", "ptb.discord.com"}


class _NoWebhookRedirects(urllib.request.HTTPRedirectHandler):
    """Never resend a credential-bearing webhook POST to a redirected host."""

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def validate_webhook_url(value: str) -> str:
    value = value.strip()
    parsed = urllib.parse.urlparse(value)
    path_parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() not in ALLOWED_WEBHOOK_HOSTS
        or len(path_parts) != 4
        or path_parts[0:2] != ["api", "webhooks"]
        or not path_parts[2].isdigit()
        or not path_parts[3]
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Enter a Discord webhook URL from discord.com/api/webhooks/…")
    return value


def webhook_payload(report: object) -> dict[str, object]:
    slots = getattr(report, "slots", [])
    errors = getattr(report, "errors", [])
    claimed = sum(1 for slot in slots if str(getattr(slot, "status", "")).startswith("claimed"))
    locked = sum(1 for slot in slots if getattr(slot, "status", "") == "claimed_global_locked")
    no_daily = sum(1 for slot in slots if getattr(slot, "status", "") == "no_daily")
    public_fallback = sum(
        1 for slot in slots if getattr(slot, "server_route", "private") == "public_fallback"
    )
    not_attempted = sum(
        1
        for slot in slots
        if getattr(slot, "status", "") in {"not_attempted", "slot_missing"}
    )
    failed_slots = sum(
        1 for slot in slots if getattr(slot, "status", "") in {"failed", "server_code_invalid"}
    )
    failed = max(len(errors), failed_slots)
    color = 0x7EB39A if failed == 0 else 0xC08A8A
    slot_lines: list[str] = []
    for slot in slots[:12]:
        number = getattr(slot, "slot", None)
        label = f"Slot {int(number):02d}" if isinstance(number, int) else "Slot"
        status = str(getattr(slot, "status", "unknown")).replace("_", " ").upper()
        day = getattr(slot, "day", None)
        day_label = f" · Day {day}" if isinstance(day, int) and 1 <= day <= 14 else ""
        route = " · PUBLIC FALLBACK" if getattr(slot, "server_route", "private") == "public_fallback" else ""
        received = getattr(slot, "received_reward", None)
        # OCR text: keep a misread from flooding the post or breaking Discord's field limit.
        if isinstance(received, str) and len(received) > 80:
            received = received[:77] + "..."
        reward_label = (
            f" · RECEIVED {received}"
            if status in {"CLAIMED GLOBAL LOCKED", "CLAIMED VERIFIED"} and received
            else ""
        )
        slot_lines.append(f"{label} · {status}{day_label}{reward_label}{route}")
    return {
        "username": "XynClaim",
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": "DBOG daily run complete" if failed == 0 else "DBOG daily run needs attention",
                "description": "Local automation report. Private server codes and screenshots are never included.",
                "color": color,
                "fields": [
                    {"name": "Claimed", "value": str(claimed), "inline": True},
                    {"name": "Global locked", "value": str(locked), "inline": True},
                    {"name": "No daily", "value": str(no_daily), "inline": True},
                    {"name": "Public fallback", "value": str(public_fallback), "inline": True},
                    {"name": "Not attempted", "value": str(not_attempted), "inline": True},
                    {"name": "Issues", "value": str(failed), "inline": True},
                    {"name": "Duration", "value": f"{float(getattr(report, 'total_seconds', 0)) / 60:.1f} min", "inline": True},
                    {
                        "name": "Access",
                        "value": "Shared private code"
                        if getattr(report, "access_mode", "owner") == "shared"
                        else "Owner private server",
                        "inline": True,
                    },
                    {
                        "name": "Slot outcomes",
                        "value": "\n".join(slot_lines) or "No slot results",
                        "inline": False,
                    },
                ],
                "footer": {"text": "XynClaim • 1920×1080 controlled mode"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }


def send_webhook(url: str, payload: dict[str, object], timeout: float = 10.0) -> None:
    safe_url = validate_webhook_url(url)
    request = urllib.request.Request(
        safe_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 XynClaim/1.0"},
        method="POST",
    )
    opener = urllib.request.build_opener(_NoWebhookRedirects())
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status not in {200, 204}:
                raise OSError(f"Discord returned HTTP {response.status}")
    except urllib.error.HTTPError as error:
        raise OSError(f"Discord returned HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise OSError(f"Couldn't reach Discord: {error.reason}") from error
