"""Render Lamplight Checkout demo video v1 (1280x720, narrated).

Terminal-style frames built with PIL from the real `scripts/demo.py
--mode mock` output (fresh run Oct 27, 2026 — deterministic ids
ORDER-0001, AUTH-0002, CAPTURE-0003), assembled with ffmpeg.
Narration: gTTS segments (build/demo-video-v1-audio/*.mp3, durations in
durations.json), mixed in with adelay so each segment starts 0.6s into its
section. On-screen caption bars carry the narration text.

Live-sandbox proof: the same code already ran live against the real
PayPal sandbox on Oct 27, 2026 (flow A order 2S552376TD187702C, $12.50
captured, capture 4UA62621MF676591E; flow B order 13A894540F527542B,
$8.00 voided). The approve URL on the live frame is built from the real
order ID -- NEVER from tool output, where the runtime redacts tokens.
Output: build/demo-video-v1.mp4
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).resolve().parent.parent
FRAMES_DIR = ROOT / "build" / "demo-video-v1-frames"
AUDIO_DIR = ROOT / "build" / "demo-video-v1-audio"
FRAMES_DIR.mkdir(parents=True, exist_ok=True)
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

W, H = 1280, 720
BG = (13, 17, 23)
FG = (230, 237, 243)
GREEN = (63, 185, 80)
BLUE = (88, 166, 255)
GOLD = (210, 153, 34)
MUTED = (139, 148, 158)
BAR = (8, 11, 16)

MONO = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 22)
MONO_BIG = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 34)
MONO_TITLE = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 44)
SANS = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)

# --- real values parsed from the fresh mock run (build/demo-mock-output.txt) ---
MOCK_OUT = (ROOT / "build" / "demo-mock-output.txt").read_text()
m = re.search(r"quote (\w+)\S* service='([^']+)' price=\$([\d.]+) worker=(\S+)", MOCK_OUT)
QA_ID, QA_SVC, QA_PRICE, QA_WORKER = m.group(1), m.group(2), m.group(3), m.group(4)
m = re.search(r"quote (\w+)\S* service='([^']+)' price=\$([\d.]+) worker=(\S+)",
              MOCK_OUT[MOCK_OUT.index("[B]"):])
QB_ID, QB_SVC, QB_PRICE, QB_WORKER = m.group(1), m.group(2), m.group(3), m.group(4)
m = re.search(r"delivered, delivery_hash=(\w+)\S*", MOCK_OUT)
DH = m.group(1)
m = re.search(r"receipt_hash=(0x[0-9a-f]+)", MOCK_OUT)
RH_A = m.group(1)
m = re.search(r"receipt_hash=(0x[0-9a-f]+)", MOCK_OUT[MOCK_OUT.index("[B]"):])
RH_B = m.group(1)

# --- live sandbox run (Oct 27, 2026): order IDs are the approve-link tokens ---
LIVE_ORDER_A = "2S552376TD187702C"
LIVE_CAPTURE_A = "4UA62621MF676591E"
LIVE_ORDER_B = "13A894540F527542B"
LIVE_APPROVE_URL = f"https://www.sandbox.paypal.com/checkoutnow?token={LIVE_ORDER_A}"
assert "<redacted>" not in LIVE_APPROVE_URL and "token=" in LIVE_APPROVE_URL

NARR = {
    "title": ("AI agents can already do the work. They can't pay each other "
              "safely — yet."),
    "diagram": ("If one agent hires another, somebody trusts blind. Pay up "
                "front and hope, or work up front and hope. Lamplight "
                "Checkout fixes that with something merchants already use "
                "every day: the PayPal authorization hold."),
    "demo_a1": ("Here's the demo, running against the PayPal sandbox. Ellis, "
                "the hiring agent, takes a plain brief — audit the top twenty "
                "holders, budget fifty dollars — matches a worker agent, and "
                "issues a quote. Twelve fifty."),
    "demo_a2": ("The payer approves the order, and PayPal places a hold. "
                "Authorized — not captured. The worker can see the money is "
                "held, but can't touch it."),
    "demo_a3": ("The worker delivers its report as a hash. The payer agent "
                "checks the hash, it matches, and PayPal captures the hold. "
                "The agent is paid. Both sides keep a receipt — and the "
                "receipt is hash-stable, so if anyone changes a single field, "
                "the hash stops matching."),
    "demo_b": ("Now the other path. A second job is authorized for eight "
               "dollars — and the worker never delivers. The deadline "
               "passes, the payer voids the authorization, and the hold goes "
               "straight back. No dispute, no chargeback, no trust needed."),
    "receipts": ("Every settlement ends in a receipt both agents keep. Same "
                 "code runs against the real PayPal sandbox — one approve "
                 "click in the browser — or fully offline against the mock "
                 "server in the repo. Twenty tests, all passing."),
    "tree": ("The code, the mock server, and twenty tests — all in the repo."),
    "live": ("And this is not just the mock. The same code ran live against "
             "the real PayPal sandbox today. Flow A: twelve fifty captured. "
             "Flow B: eight dollars voided after the deadline. The approve "
             "link is one click in the browser — the buyer approves, and the "
             "script continues."),
    "closing": ("Agents hiring agents, paid on delivery. That's Lamplight "
                "Checkout."),
}
CAPTIONS = dict(NARR)
LEAD = 0.6  # narration starts this far into each section


def short(tx: str) -> str:
    return tx[:10] + "…" + tx[-6:]


def wrap(text: str, font, max_w: int):
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if font.getlength(trial) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render(lines, caption: str, path: pathlib.Path):
    """lines: list of (text, color, font). caption: narration text for the bar."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 56
    for text, color, font in lines:
        d.text((70, y), text, fill=color, font=font)
        y += 46 if font is MONO else 62
    cap_lines = wrap(caption, SANS, W - 140)
    bar_h = 16 + len(cap_lines) * 27 + 16
    d.rectangle([0, H - bar_h, W, H], fill=BAR)
    d.line([0, H - bar_h, W, H - bar_h], fill=(48, 54, 61), width=2)
    cy = H - bar_h + 16
    for line in cap_lines:
        d.text((70, cy), line, fill=FG, font=SANS)
        cy += 27
    img.save(path)


TITLE = [
    ("Lamplight Checkout", GOLD, MONO_TITLE),
    ("", FG, MONO),
    ("Escrow for AI agents, on PayPal", FG, MONO_BIG),
    ("", FG, MONO),
    ("PayPal AI Hackathon  ·  Devpost", MUTED, MONO),
]

DIAGRAM = [
    ("Agent A hires Agent B — somebody trusts blind", GOLD, MONO_BIG),
    ("", FG, MONO),
    ('  "pay first, hope"    A pays, B may never deliver', FG, MONO),
    ('  "work first, hope"   B works, A may never pay', FG, MONO),
    ("", FG, MONO),
    ("Lamplight Checkout: the PayPal authorization hold", GREEN, MONO_BIG),
    ("  money held — not captured — until delivery is proven", FG, MONO),
]

PAGE_A1 = [
    ("$ .venv/bin/python scripts/demo.py --mode mock", GOLD, MONO),
    ("mock PayPal sandbox up on http://127.0.0.1:5001", MUTED, MONO),
    ("[A] happy path — agent delivers, payer releases", FG, MONO),
    (f"quote {QA_ID}… '{QA_SVC}'", FG, MONO),
    (f"    price=${QA_PRICE}  worker={QA_WORKER}", FG, MONO),
    ("order ORDER-0001 status=CREATED (AUTHORIZE intent)", FG, MONO),
]

PAGE_A2 = PAGE_A1 + [
    ("buyer approved (mock): order ORDER-0001", FG, MONO),
    ("AUTHORIZED hold: authorization AUTH-0002", GREEN, MONO),
]

PAGE_A3 = PAGE_A2 + [
    (f"delivered, delivery_hash={DH}…", FG, MONO),
    ("payer agent verified the delivery hash — matches", GREEN, MONO),
    ("CAPTURED: capture CAPTURE-0003 — agent paid $12.50", GREEN, MONO),
    (f"receipt_hash={short(RH_A)}", BLUE, MONO),
]

PAGE_B = [
    ("$ .venv/bin/python scripts/demo.py --mode mock  (continued)", GOLD, MONO),
    ("[B] timeout — agent never delivers, hold is voided", FG, MONO),
    (f"quote {QB_ID}… '{QB_SVC}'", FG, MONO),
    (f"    price=${QB_PRICE}  worker={QB_WORKER}", FG, MONO),
    ("order ORDER-0004 status=CREATED", FG, MONO),
    ("buyer approved (mock): order ORDER-0004", FG, MONO),
    ("AUTHORIZED hold: authorization AUTH-0005 (deadline: 5s)", FG, MONO),
    ("[guard] capture before delivery correctly refused", GREEN, MONO),
    ("    cannot capture in state AUTHORIZED (needs DELIVERED)", MUTED, MONO),
    ("agent never delivers… waiting past the 5s deadline", FG, MONO),
    ("VOIDED after deadline — hold released back to payer", GREEN, MONO),
    (f"receipt_hash={short(RH_B)}", BLUE, MONO),
]

RECEIPT = [
    ("demo-receipts/receipt-A-released.json", GOLD, MONO_BIG),
    ("", FG, MONO),
    ('  "paypal_order_id": "ORDER-0001",', FG, MONO),
    ('  "authorization_id": "AUTH-0002",', FG, MONO),
    ('  "capture_id": "CAPTURE-0003",', FG, MONO),
    ('  "amount_cents": 1250,  "outcome": "released",', FG, MONO),
    ('  "receipt_hash":', FG, MONO),
    (f'  "{RH_A}"', GREEN, MONO),
    ('  "protocol": "lamplight-checkout/1"', MUTED, MONO),
]

TREE = [
    ("lamplight-checkout/", GOLD, MONO_BIG),
    ("", FG, MONO),
    ("  app/      escrow.py  hiring_agent.py  paypal_client.py", FG, MONO),
    ("            quote.py  receipt.py  server.py", FG, MONO),
    ("  scripts/  demo.py  mock_paypal.py", FG, MONO),
    ("  tests/    3 files — 20 tests, all passing", GREEN, MONO),
    ("  docs/     SUBMISSION.md  VIDEO-SCRIPT.md", FG, MONO),
    ("  demo-receipts/  README.md  LICENSE  requirements.txt", FG, MONO),
]

LIVE = [
    ("Ran live on the real PayPal sandbox — Oct 27, 2026", GOLD, MONO_BIG),
    ("", FG, MONO),
    (f"Flow A: order {LIVE_ORDER_A} — $12.50 CAPTURED", FG, MONO),
    (f"        capture {LIVE_CAPTURE_A}", FG, MONO),
    (f"Flow B: order {LIVE_ORDER_B} — $8.00 VOIDED", FG, MONO),
    ("", FG, MONO),
    ("Approve link — one click in the browser:", MUTED, MONO),
    (LIVE_APPROVE_URL, BLUE, MONO),
]

CLOSING = [
    ("Lamplight Checkout", GOLD, MONO_TITLE),
    ("", FG, MONO),
    ("Agents hiring agents, paid on delivery.", FG, MONO_BIG),
    ("", FG, MONO),
    ("PayPal AI Hackathon  ·  Devpost", MUTED, MONO),
    ("Authorize  ·  Capture  ·  Void — on PayPal", FG, MONO),
]

# (key, lines, accumulate?, tail_s)
SECTIONS = [
    ("title", TITLE, False, 0.5),
    ("diagram", DIAGRAM, False, 0.5),
    ("demo_a1", PAGE_A1, True, 0.5),
    ("demo_a2", PAGE_A2, True, 0.5),
    ("demo_a3", PAGE_A3, True, 0.5),
    ("demo_b", PAGE_B, True, 0.5),
    ("receipts", RECEIPT, False, 0.5),
    ("tree", TREE, False, 0.5),
    ("live", LIVE, False, 1.0),
    ("closing", CLOSING, False, 2.0),
]
STEP = 1.0  # seconds between accumulated terminal lines


def main() -> None:
    # --- narration via gTTS (US English, plain honest voice) ---
    from gtts import gTTS

    durations = {}
    for key, text in NARR.items():
        mp3 = AUDIO_DIR / f"{key}.mp3"
        if not mp3.exists():
            gTTS(text=text, lang="en", tld="com").save(str(mp3))
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(mp3)], capture_output=True, text=True, check=True)
        durations[key] = float(probe.stdout.strip())
    (AUDIO_DIR / "durations.json").write_text(json.dumps(durations, indent=2))
    print(f"narration durations: { {k: round(v, 2) for k, v in durations.items()} }")
    print(f"total narration: {sum(durations.values()):.2f}s, "
          f"{sum(len(v.split()) for v in NARR.values())} words")

    concat_entries = []  # (frame path, duration)
    section_starts = {}
    t = 0.0
    n = 0
    for key, lines, accum, tail in SECTIONS:
        section_starts[key] = t
        dur = max(LEAD + durations[key] + tail, len(lines) * STEP if accum else 0.0)
        if accum:
            for k in range(len(lines)):
                p = FRAMES_DIR / f"f{n:04d}.png"
                render(lines[: k + 1], CAPTIONS[key], p)
                d = STEP if k < len(lines) - 1 else dur - (len(lines) - 1) * STEP
                concat_entries.append((p, d))
                n += 1
        else:
            p = FRAMES_DIR / f"f{n:04d}.png"
            render(lines, CAPTIONS[key], p)
            concat_entries.append((p, dur))
            n += 1
        t += dur

    total = t
    print(f"sections: { {k: round(v, 2) for k, v in section_starts.items()} }")
    print(f"total video: {total:.2f}s ({total/60:.2f} min), {len(concat_entries)} unique frames")

    lst = FRAMES_DIR / "concat.txt"
    with lst.open("w") as f:
        for p, d in concat_entries:
            f.write(f"file '{p}'\nduration {d:.3f}\n")
        f.write(f"file '{concat_entries[-1][0]}'\n")

    silent = ROOT / "build" / "demo-video-v1-silent.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-vf", "fps=30", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
         "-movflags", "+faststart", str(silent)],
        check=True, capture_output=True, text=True)

    wavs, delays = [], []
    for i, (key, *_rest) in enumerate(SECTIONS):
        wav = AUDIO_DIR / f"{key}.wav"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(AUDIO_DIR / f"{key}.mp3"),
                        "-ar", "48000", "-ac", "1", str(wav)], check=True)
        wavs.append(wav)
        delays.append(int(round((section_starts[key] + LEAD) * 1000)))

    filt = "".join(f"[{i}:a]adelay={d}|{d}[a{i}];" for i, d in enumerate(delays))
    filt += "".join(f"[a{i}]" for i in range(len(wavs))) + f"amix=inputs={len(wavs)}:normalize=0[aout]"
    audio = AUDIO_DIR / "mixed.m4a"
    cmd = ["ffmpeg", "-y"]
    for w in wavs:
        cmd += ["-i", str(w)]
    cmd += ["-filter_complex", filt, "-map", "[aout]", "-c:a", "aac", "-b:a", "128k", str(audio)]
    subprocess.run(cmd, check=True, capture_output=True, text=True)

    out = ROOT / "build" / "demo-video-v1.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(silent), "-i", str(audio),
         "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "copy",
         "-t", f"{total:.2f}", str(out)],
        check=True, capture_output=True, text=True)

    (ROOT / "build" / "demo-video-v1-narration.txt").write_text(
        "\n\n".join(f"[{k.upper()}]\n{v}" for k, v in NARR.items()) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
