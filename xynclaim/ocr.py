"""Read the reward popups DBOG shows after a daily claim.

Uses Windows' built-in OCR (Windows.Media.Ocr through pywinrt), so the standalone
build needs no bundled OCR engine. Every function returns None when OCR is
unavailable, and callers keep the claim as unverified in that case.

Reward popup samples: Omar Toketa <omar.toketa47@proton.me>
"""
from __future__ import annotations

import asyncio
import re

import cv2
import numpy as np

# Popups stack top-down. Unrelated ones (e.g. "Elder Kai's Online Training") share
# the stack, so only these titles count as the daily reward.
RECEIVED_FOR = re.compile(r"^received (.+?) for$", re.IGNORECASE)
RECEIVED_ITEMS = re.compile(r"^received \+?(\d+) of item$", re.IGNORECASE)
RECEIVED = re.compile(r"^received (.+)$", re.IGNORECASE)
GLOBAL_LOCKED = "global reward locked"
BALLS_COMPLETE = "dragon balls complete"
DRAGON_BALL = re.compile(r"^[1-7]-Star .*Dragon Ball$")
INTEGER_VALUE = re.compile(r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)")
# New daily panel (October 2026): "Claim Day 3" button and "Streak 2 / 14" footer.
# OCR reads the digit 0 as the letter O.
CLAIM_DAY = re.compile(r"claim\s*day\s*([0-9OoIl|]{1,2})", re.IGNORECASE)
# The "/ 14" is sometimes misread ("/ 11+"), so only the leading number counts.
STREAK = re.compile(r"streak\s*([0-9OoIl|]{1,2})\s*/", re.IGNORECASE)
ALREADY_CLAIMED_BOOST = "already claimed on this"
# Day 14 with every Add-On Pack owned: "Day 14 Bonus" / "1 Hour 2x Zeni + 2x Drops + 2x EXP".
DAY_BONUS = re.compile(r"^day \d+ bonus$", re.IGNORECASE)


def _ocr_int(text: str) -> int:
    return int(text.translate(str.maketrans("OoIl|", "00111")))


def parse_claim_day(lines: list[str]) -> int | None:
    for line in lines:
        if match := CLAIM_DAY.search(line):
            return _ocr_int(match.group(1))
    return None


def parse_streak(lines: list[str]) -> int | None:
    for line in lines:
        if match := STREAK.search(line):
            return _ocr_int(match.group(1))
    return None


def ocr_lines(image: np.ndarray) -> list[str] | None:
    try:
        from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage.streams import DataWriter
    except ImportError:
        return None
    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        return None
    # Native size on purpose: upscaling 2x produced misreads like "731250" for 31250.
    # Black-on-white text: on the raw white-on-navy popup, OCR dropped the "2X" in
    # "Received 2X EXP for".
    gray = cv2.cvtColor(np.ascontiguousarray(image[:, :, :3]), cv2.COLOR_BGR2GRAY)
    _, text_mask = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY_INV)
    bgra = cv2.cvtColor(text_mask, cv2.COLOR_GRAY2BGRA)
    height, width = bgra.shape[:2]
    writer = DataWriter()
    writer.write_bytes(bgra.tobytes())
    bitmap = SoftwareBitmap.create_copy_from_buffer(
        writer.detach_buffer(), BitmapPixelFormat.BGRA8, width, height
    )

    async def recognize():
        return await engine.recognize_async(bitmap)

    lines = (_printable(line.text) for line in asyncio.run(recognize()).lines)
    return [line for line in lines if line]


def _printable(text: str) -> str:
    return "".join(char for char in text if char.isprintable()).strip()


def _clean_value(value: str) -> str:
    # OCR reads the "1" in "1-Star" as I or l.
    return re.sub(r"^[Il|](?=-Star)", "1", value.strip())


def _integer_value(value: str) -> int | None:
    value = value.strip()
    return int(value.replace(",", "")) if INTEGER_VALUE.fullmatch(value) else None


def _format_duration(seconds_text: str) -> str | None:
    duration = _integer_value(seconds_text)
    if duration is None:
        return None
    minutes, seconds = divmod(duration, 60)
    return f"{minutes} min" if not seconds else f"{minutes} min {seconds} s"


def _toast_reward(body: str) -> str:
    """Normalise the October 2026 toast sentence to a reward name."""
    body = re.sub(r"^(you\s+)?received\s+", "", body.strip(), flags=re.IGNORECASE).rstrip(".")
    body = re.sub(r"^[Il|](?=-Star)", "1", body)
    # A wrapped name loses its short last line ("Ball.") to OCR; Dragon Balls are the
    # only daily rewards whose names wrap at "Dragon".
    if re.search(r"-Star .*Dragon$", body):
        body += " Ball"
    if zeni := re.fullmatch(r"\+?([0-9][0-9,]*) Zeni", body):
        body = f"{int(zeni.group(1).replace(',', '')):,} Zeni"
    return body


def parse_rewards(lines: list[str]) -> list[str]:
    """Turn popup title/value line pairs into reward strings, ignoring unrelated popups."""
    rewards = []
    index = 0
    while index < len(lines):
        title = lines[index].strip()
        value = _clean_value(lines[index + 1]) if index + 1 < len(lines) else ""
        lowered = title.lower()
        received = RECEIVED.match(title)
        titled = received or lowered in {GLOBAL_LOCKED, BALLS_COMPLETE} or DAY_BONUS.match(title)
        # Missing values must not consume the following popup's title.
        if value.lower() in {GLOBAL_LOCKED, BALLS_COMPLETE} or RECEIVED.match(value):
            value = ""
        index += 1
        if lowered.startswith("daily rew"):  # OCR sometimes garbles "reward"
            # October 2026 toast: a "Daily reward" title over one wrapped sentence.
            body = " ".join(_clean_value(line) for line in lines[index:]).strip()
            if ALREADY_CLAIMED_BOOST in body.lower():
                rewards.append("+10 Mastery Points (global reward locked)")
            elif body:
                rewards.append(_toast_reward(body))
            break
        if value and titled:
            index += 1
        if lowered == GLOBAL_LOCKED:
            rewards.append(f"{value or '+10 Mastery Points'} (global reward locked)")
        elif lowered == BALLS_COMPLETE:
            rewards.append(f"{value or '+100 Mastery Points'} (all Dragon Balls held)")
        elif DAY_BONUS.match(title):
            if value:
                rewards.append(value)
        elif match := RECEIVED_ITEMS.match(title):
            if value:
                rewards.append(f"{match.group(1)}x {value}")
        elif match := RECEIVED_FOR.match(title):
            duration = _format_duration(value)
            if duration is not None:
                rewards.append(f"{match.group(1)} for {duration}")
        elif received:
            thing = received.group(1)
            if thing.lower() == "zeni":
                quantity = _integer_value(value)
                if quantity is not None:
                    rewards.append(f"{quantity:,} Zeni")
            elif value:
                rewards.append(value if thing.lower() in value.lower() else f"{thing}: {value}")
        elif DRAGON_BALL.match(_clean_value(title)):
            # The title was clipped, but a Dragon Ball value names itself.
            rewards.append(_clean_value(title))
    return rewards


def is_global_locked(lines: list[str]) -> bool:
    text = " ".join(lines).lower()
    return GLOBAL_LOCKED in text or ALREADY_CLAIMED_BOOST in text
