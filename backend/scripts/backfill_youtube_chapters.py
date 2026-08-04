"""Backfill corrected YouTube chapter timestamps.

For each candidate video the offset is measured between the FLAC the tracklist
was detected against and the audio YouTube actually serves for that video —
i.e. the timestamps are derived from the published asset, not copied from
SoundCloud. Videos whose audio does not confidently align with the FLAC are
skipped: that means the wrong source file is attached to the mix, and no
offset would make those chapters right.

Only the description is written. Titles, thumbnails, visibility, tags,
category and everything else on the video are preserved verbatim.

Usage:  python yt_chapter_backfill.py [--apply] [video_id ...]
"""
import json, os, re, subprocess, sqlite3, sys, tempfile, asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from app.services import audio_alignment
from app.services.description_generator import _format_chapter_block

BACKUP = "/data/yt_description_backups.json"
REPORT = "/data/yt_backfill_report.json"
APPLY = "--apply" in sys.argv
ONLY = [a for a in sys.argv[1:] if not a.startswith("--")]

TS_LINE = re.compile(r"^\s*(?:\d+:)?\d{1,2}:\d{2}\s+\S.*$", re.M)
_ISO = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def _iso8601_seconds(text):
    h, m, sec = _ISO.match(text).groups()
    return int(h or 0) * 3600 + int(m or 0) * 60 + int(sec or 0)

db = sqlite3.connect("file:/data/fadeout.db?mode=ro", uri=True)
db.row_factory = sqlite3.Row
sj = json.loads(db.execute("select settings_json from app_settings limit 1").fetchone()[0])
creds = Credentials(
    token=None, refresh_token=sj["youtube_refresh_token"],
    client_id=os.environ["YOUTUBE_CLIENT_ID"],
    client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
    token_uri="https://oauth2.googleapis.com/token",
    scopes=["https://www.googleapis.com/auth/youtube"],
)
creds.refresh(Request())
yt = build("youtube", "v3", credentials=creds, cache_discovery=False)


def strip_tracklist(desc):
    """Remove the existing tracklist/chapter block, keep the prose."""
    lines = desc.splitlines()
    cut = len(lines)
    for i, ln in enumerate(lines):
        if ln.strip().lower().rstrip(":") in ("tracklist", "chapters", "track list"):
            cut = i
            break
    if cut == len(lines):
        run = 0
        for i, ln in enumerate(lines):
            if TS_LINE.match(ln):
                run += 1
                if run >= 3:
                    cut = i - run + 1
                    break
            elif ln.strip():
                run = 0
    return "\n".join(lines[:cut]).rstrip()


def fetch_audio(video_id, dest):
    subprocess.run([
        "python", "-m", "yt_dlp", "-q", "--no-warnings",
        "-f", "worstaudio/bestaudio",
        "-o", dest, f"https://www.youtube.com/watch?v={video_id}",
    ], check=True)


backups = json.load(open(BACKUP)) if os.path.exists(BACKUP) else {}
report = []

rows = list(db.execute("""
    select id, title, youtube_video_id, audio_file_path, duration_seconds, tracklist
    from mixes
    where youtube_video_id is not null and tracklist is not null
      and json_array_length(tracklist) >= 3
"""))

for r in rows:
    vid = r["youtube_video_id"]
    if ONLY and vid not in ONLY:
        continue
    flac = r["audio_file_path"]
    if not flac or not os.path.exists(flac):
        continue

    item = yt.videos().list(part="snippet,contentDetails", id=vid).execute().get("items")
    if not item:
        continue
    snippet = item[0]["snippet"]
    yt_seconds = _iso8601_seconds(item[0]["contentDetails"]["duration"])
    desc = snippet.get("description", "")
    if len(TS_LINE.findall(desc)) < 3:
        continue

    # ALWAYS back up before anything else.
    if vid not in backups:
        backups[vid] = {"title": snippet.get("title"), "description": desc}
        json.dump(backups, open(BACKUP, "w"), indent=1)

    entry = {"video_id": vid, "mix_id": r["id"], "yt_title": snippet.get("title"),
             "flac": os.path.basename(flac)}
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "a.%(ext)s")
        try:
            fetch_audio(vid, out)
        except Exception as exc:
            entry["skipped"] = f"download failed: {exc}"
            report.append(entry); print(json.dumps(entry), flush=True); continue
        got = [os.path.join(td, f) for f in os.listdir(td)]
        if not got:
            entry["skipped"] = "no audio downloaded"
            report.append(entry); print(json.dumps(entry), flush=True); continue
        try:
            al = asyncio.run(audio_alignment.measure_offset(
                flac, got[0], reference_duration=r["duration_seconds"]))
        except audio_alignment.AlignmentError as exc:
            entry["skipped"] = f"NO ALIGNMENT — wrong source file? ({exc})"
            report.append(entry); print(json.dumps(entry), flush=True); continue

    entry.update(offset=al.offset_seconds, confidence=al.confidence,
                 spread=al.spread_seconds, probes=al.probes)

    tracks = json.loads(r["tracklist"])
    shifted = [dict(t, timestamp_seconds=max(0.0, (t.get("timestamp_seconds") or 0) + al.offset_seconds))
               for t in tracks]
    # Bookend the tracks: the pre-roll runs exactly as long as the measured
    # offset, and the mix stops at offset + FLAC duration. Both come free from
    # numbers already in hand; the builder drops either bookend that would
    # break YouTube's rules.
    mix_end = al.offset_seconds + (r["duration_seconds"] or 0)
    block = _format_chapter_block(
        shifted,
        lead_in_seconds=al.offset_seconds,
        mix_end_seconds=mix_end if r["duration_seconds"] else None,
        video_duration_seconds=yt_seconds,
    )
    if not block:
        entry["skipped"] = "chapter block failed YouTube validation"
        report.append(entry); print(json.dumps(entry), flush=True); continue

    new_desc = f"{strip_tracklist(desc)}\n\n{block}".strip()
    lines = block.splitlines()
    entry["old_first_stamp"] = (TS_LINE.findall(desc) or [""])[0].strip()[:24]
    entry["new_first_stamp"] = lines[1][:24]
    entry["new_second_stamp"] = lines[2][:40] if len(lines) > 2 else ""
    entry["last_chapter"] = lines[-1][:32]
    entry["yt_seconds"] = yt_seconds
    entry["mix_end"] = round(mix_end, 1)
    entry["new_len"] = len(new_desc)

    if len(new_desc) > 4900:
        entry["skipped"] = f"description too long ({len(new_desc)})"
        report.append(entry); print(json.dumps(entry), flush=True); continue

    if APPLY:
        snippet["description"] = new_desc
        yt.videos().update(part="snippet", body={"id": vid, "snippet": snippet}).execute()
        entry["applied"] = True
    else:
        entry["applied"] = False
    report.append(entry)
    print(json.dumps(entry), flush=True)

json.dump(report, open(REPORT, "w"), indent=1)
print("\nbackups:", BACKUP, "| entries:", len(backups))
print("aligned:", sum(1 for e in report if "offset" in e),
      "| applied:", sum(1 for e in report if e.get("applied")),
      "| skipped:", sum(1 for e in report if "skipped" in e))
