#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import time
import shutil
import subprocess
from pathlib import Path
from typing import List, Tuple, Set

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from tqdm import tqdm
from yt_dlp import YoutubeDL
import eyed3
import mutagen

# ---------- CONFIG ----------
SPOTIFY_CLIENT_ID = "069f9949534e4563b074cf3c4ccebb28"
SPOTIFY_CLIENT_SECRET = "f453fa3e48444183831cddc07cec5af8"

GENRE_KEYWORD = "pop hits"
SAVE_DIR = "Top 40"
MAX_TRACKS = 10

# Duration limits (seconds)
MIN_DURATION = 130
MAX_DURATION = 720

# Your exported cookies file (txt)
COOKIES_FILE = "www.youtube.com_cookies.txt"   # put next to this script or set absolute path

EXCLUDE_PLAYLIST_KEYWORDS = [
    "k-pop", "rock", "edm", "rap", "hip hop", "r&b", "reggae",
    "trap", "jazz", "indie", "afro", "amapiano",
    "throwback", "oldies", "retro", "classic", "remix",
    "country", "metal", "acoustic", "instrumental", "lofi",
    "alt", "alternative", "underground", "covers",
    "workout", "party", "dance", "house",
    "emo", "punk", "folk", "soul", "afrobeats"
]
CLEAN_FILTER_KEYWORDS = [
    "clean", "radio edit", "no cursing", "clean version", "lyrics clean"
]

# Block non-original/undesired versions
TITLE_BLOCKLIST = [
    "explicit", "dirty", "uncut", "nsfw", "raw",
    "live", "karaoke", "instrumental", "remix", "cover", "performance",
    "mix", "megamix", "dj set", "set", "continuous mix",
    "full album", "album", "compilation", "playlist",
    "shorts", "preview", "snippet", "sample",
    "sped up", "sped-up", "speed up", "nightcore", "8d", "tribute", "fanmade", "ai cover",
    "reimagined", "reworked", "piano version", "guitar cover", "drum cover",
    "lyrics video", "visualizer", "slowed", "slow + reverb",
    "edit", "mashup", "bootleg", "reverb", "loop"
]
LONGFORM_PATTERNS = [
    r"\b\d{1,2}\s*(hour|hr|h)\b",
    r"\b\d{2,}\s*min\b",
    r"\b(best\s+of|nonstop|continuous)\b",
]

Path(SAVE_DIR).mkdir(parents=True, exist_ok=True)

# ---------- UTILS ----------
def sanitize_filename(text: str) -> str:
    for ch in ['/', '\\', ':', '*', '?', '"', '<', '>', '|']:
        text = text.replace(ch, '-')
    return text.strip()

def normalize(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', (text or '').lower())

def has_longform_pattern(text: str) -> bool:
    t = (text or "").lower()
    return any(re.search(p, t) for p in LONGFORM_PATTERNS)

def get_audio_duration(filepath: str) -> int:
    try:
        af = mutagen.File(filepath)
        return int(af.info.length) if af and af.info else 0
    except Exception:
        return 0

def ensure_ffmpeg_location() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError(
            "ffmpeg not found. Install it (e.g. `brew install ffmpeg`) "
            "or set `ffmpeg_location` below to its full path."
        )
    return path

# ---------- CANONICALIZATION FOR DE-DUPE ----------
PARENS_RE = re.compile(r"\s*[\(\[][^)\]]*[\)\]]")  # strip (...) and [...]
SUFFIXES_RE = re.compile(
    r"\s*-\s*(single version|radio edit|clean|explicit|remaster(?:ed)?(?: \d{4})?|edit|version)\b.*",
    re.IGNORECASE,
)

def canonical_title(title: str) -> str:
    t = title or ""
    t = PARENS_RE.sub("", t)
    t = SUFFIXES_RE.sub("", t)
    t = t.strip()
    return normalize(t)

def canonical_artist(artist: str) -> str:
    a = artist or ""
    # keep primary artist only (split on feat./with/&/x/,)
    a = re.split(r"\s*(feat\.?|with|&|,| x )\s*", a, maxsplit=1, flags=re.IGNORECASE)[0]
    a = a.strip()
    return normalize(a)

def key_for(artist: str, title: str) -> str:
    return f"{canonical_artist(artist)}::{canonical_title(title)}"

def parse_artist_title_from_path(path: Path) -> Tuple[str, str]:
    """
    Try to recover artist/title from either folder layout Artist/Title.mp3
    OR legacy 'Artist - Title.mp3' filenames.
    """
    artist = path.parent.name
    title = path.stem
    # Legacy "Artist - Title" in one filename
    if " - " in path.stem:
        a, t = path.stem.split(" - ", 1)
        # If parent folder is SAVE_DIR itself, prefer split parts
        if path.parent.name == Path(SAVE_DIR).name:
            artist, title = a, t
    return artist, title

# Build an index of what we already have (by canonical artist/title).
EXISTING_KEYS: Set[str] = set()

def build_library_index():
    global EXISTING_KEYS
    EXISTING_KEYS = set()
    for mp3 in Path(SAVE_DIR).rglob("*.mp3"):
        artist, title = None, None
        try:
            af = eyed3.load(str(mp3))
            if af and af.tag and (af.tag.artist or af.tag.title):
                artist = af.tag.artist
                title = af.tag.title
        except Exception:
            pass
        if not artist or not title:
            a2, t2 = parse_artist_title_from_path(mp3)
            artist = artist or a2
            title = title or t2
        if artist and title:
            EXISTING_KEYS.add(key_for(artist, title))

def already_have(artist: str, title: str) -> bool:
    return key_for(artist, title) in EXISTING_KEYS

def remember(artist: str, title: str):
    EXISTING_KEYS.add(key_for(artist, title))

# ---------- APPLE MUSIC (macOS) ----------
def is_in_apple_music(filepath: str) -> bool:
    script = f'''
    tell application "Music"
        set trackPath to POSIX file "{os.path.abspath(filepath)}"
        set trackList to every track of library playlist 1 whose location is trackPath
        if (count of trackList) > 0 then
            return "YES"
        else
            return "NO"
        end if
    end tell
    '''
    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
        return result.stdout.strip() == "YES"
    except Exception:
        return False

def add_to_apple_music_if_new(filepath: str):
    try:
        if not is_in_apple_music(filepath):
            subprocess.run([
                "osascript", "-e",
                f'tell application "Music" to add POSIX file "{os.path.abspath(filepath)}" to library playlist 1'
            ], check=True, timeout=10)
            print(f"🎶 Added to Apple Music: {os.path.basename(filepath)}")
        else:
            print(f"⏩ Already in Apple Music: {os.path.basename(filepath)}")
    except subprocess.TimeoutExpired:
        print(f"⏱️ Apple Music add timed out: {filepath}")
    except Exception as e:
        print(f"⚠️ Failed to add to Apple Music: {filepath} | {e}")

# ---------- TAGGING ----------
def ensure_id3v23(audiofile):
    if audiofile.tag is None:
        audiofile.initTag()
    audiofile.tag.version = (2, 3, 0)

def tag_mp3(filepath: str, genre: str, artist: str, title: str):
    try:
        audiofile = eyed3.load(filepath)
        if audiofile is None:
            print(f"⚠️ Couldn't load: {filepath}")
            return
        ensure_id3v23(audiofile)
        audiofile.tag.genre = genre
        audiofile.tag.artist = artist
        audiofile.tag.title = title
        audiofile.tag.save()
        print(f"🏷️ Tagged: {Path(filepath).name}  [Artist={artist} | Title={title} | Genre={genre}]")
    except Exception as e:
        print(f"❌ Tagging error for {filepath}: {e}")

# ---------- CLEAN EXISTING (optional) ----------
def clean_and_tag_existing_folder():
    for path in sorted(Path(SAVE_DIR).rglob("*.mp3")):
        dur = get_audio_duration(str(path))
        if dur == 0 or dur < MIN_DURATION or dur > MAX_DURATION:
            print(f"🗑️ Removing out-of-range/unreadable file ({dur}s): {path}")
            try: path.unlink()
            except Exception: pass
            continue
        artist_guess, title_guess = parse_artist_title_from_path(path)
        tag_mp3(str(path), SAVE_DIR, artist_guess, title_guess)
        add_to_apple_music_if_new(str(path))

# ---------- SPOTIFY ----------
auth_manager = SpotifyClientCredentials(client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET)
sp = spotipy.Spotify(auth_manager=auth_manager)

def is_irrelevant_playlist(name: str, desc: str) -> bool:
    full = f"{name} {desc}".lower()
    return any(bad in full for bad in EXCLUDE_PLAYLIST_KEYWORDS)

def get_tracks_from_genre_playlists(keyword: str) -> List[Tuple[str, str, bool]]:
    print(f"🔍 Searching for playlists related to: {keyword}")
    playlists = sp.search(q=keyword, type="playlist", limit=20)
    tracks: List[Tuple[str, str, bool]] = []
    for pl in playlists.get("playlists", {}).get("items", []):
        if not pl:
            continue
        name = pl.get("name", "")
        desc = pl.get("description", "")
        if is_irrelevant_playlist(name, desc):
            print(f"⛔ Skipping playlist: {name}")
            continue
        pid = pl.get("id")
        if not pid:
            continue
        try:
            results = sp.playlist_tracks(pid)
            for item in results.get("items", []):
                track = item.get("track") or {}
                title = track.get("name")
                if not title:
                    continue
                dur = int((track.get("duration_ms") or 0) / 1000)
                if dur < MIN_DURATION or dur > MAX_DURATION:
                    continue
                artist = (track.get("artists") or [{}])[0].get("name", "")
                explicit = track.get("explicit", False)
                tracks.append((title, artist, explicit))
                if len(tracks) >= MAX_TRACKS:
                    break
        except Exception as e:
            print(f"⚠️ Failed to fetch tracks from playlist: {name} | {e}")
        if len(tracks) >= MAX_TRACKS:
            break
    print(f"🎵 Found {len(tracks)} eligible tracks.")
    return tracks

# ---------- YOUTUBE HELPERS ----------
def title_is_blocked(text: str) -> bool:
    low = (text or "").lower()
    if any(b in low for b in TITLE_BLOCKLIST):
        return True
    if has_longform_pattern(low):
        return True
    return False

def looks_like_original_channel(channel: str, uploader: str, artist: str) -> bool:
    ch = normalize(channel)
    up = normalize(uploader)
    ar = normalize(artist)
    if not ch and not up:
        return False
    if (ch.startswith(ar) and ch.endswith("topic")) or (up.startswith(ar) and up.endswith("topic")):
        return True
    if (ch.endswith("vevo") and ar in ch) or (up.endswith("vevo") and ar in up):
        return True
    if ch == ar or up == ar:
        return True
    if ("official" in ch and ar in ch) or ("official" in up and ar in up):
        return True
    return False

def title_matches_song(video_title: str, song_title: str) -> bool:
    vt = normalize(re.sub(r"\(.*?\)|\[.*?\]", "", video_title))
    st = normalize(song_title)
    if not st or st not in vt:
        return False
    for bad in ["live","remix","cover","acoustic","instrumental","spedup","slowed","nightcore","8d","mashup","edit"]:
        if bad in vt:
            return False
    return True

def make_ydl_opts(outtmpl: str, cookiefile: str, client: str = "android") -> dict:
    return {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'outtmpl': outtmpl,
        'noplaylist': True,
        'default_search': 'ytsearch25',
        'cookiefile': cookiefile,
        'quiet': True,
        'no_warnings': True,
        'retries': 15,
        'fragment_retries': 15,
        'continuedl': True,
        'concurrent_fragment_downloads': 1,
        'throttledratelimit': 0,
        'sleep_interval_requests': 0.5,
        'max_sleep_interval_requests': 1.0,
        'http_headers': {'Accept-Language': 'en-US,en;q=0.9'},
        'extractor_args': {'youtube': {'player_client': [client]}},
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '0',
        }],
        'ffmpeg_location': ensure_ffmpeg_location(),
        'geo_bypass': True,
        'overwrites': True,
        'allow_multiple_video_streams': False,
        'allow_multiple_audio_streams': False
    }

# ---------- YOUTUBE DOWNLOAD ----------
def try_download(track_name: str, artist_name: str, is_explicit: bool):
    # De-dupe: skip if the library already has an equivalent track
    if already_have(artist_name, track_name):
        print(f"⏩ Skipping (already in library): {artist_name} / {track_name}")
        return

    # Save as Top 40/<Artist>/<Title>.mp3
    artist_dir = Path(SAVE_DIR) / sanitize_filename(artist_name)
    artist_dir.mkdir(parents=True, exist_ok=True)
    filename_base = sanitize_filename(track_name)
    target_mp3 = artist_dir / f"{filename_base}.mp3"

    # Also protect against exact target existing (new layout)
    if target_mp3.exists():
        print(f"⏩ Already downloaded at target: {artist_name} / {track_name}")
        remember(artist_name, track_name)
        return

    cookiefile = os.path.abspath(COOKIES_FILE)
    if not os.path.exists(cookiefile):
        raise FileNotFoundError(f"cookies file not found at: {cookiefile}")

    base = f"{artist_name} {track_name}"
    queries = [
        f"{base} official audio",
        f"{base} - topic",
        f"{base} clean",
        base,
    ]
    if is_explicit:
        queries = [f"{base} {v}" for v in CLEAN_FILTER_KEYWORDS] + queries

    clients = ["android_music", "android", "web"]
    format_loosened = False

    for q in queries:
        print(f"🔎 Searching: {q}")
        for client in clients:
            ydl_opts = make_ydl_opts(f"{artist_dir}/{filename_base}.%(ext)s", cookiefile, client=client)
            if format_loosened:
                ydl_opts['format'] = 'bestaudio/best'

            try:
                with YoutubeDL(ydl_opts) as ydl:
                    results = ydl.extract_info(f"ytsearch25:{q}", download=False).get('entries', []) or []
                    ranked = []
                    for e in results:
                        if not e:
                            continue
                        title = e.get('title') or ''
                        channel = e.get('channel') or ''
                        uploader = e.get('uploader') or ''
                        dur = int(e.get('duration') or 0)
                        url = e.get('webpage_url') or ''
                        low = (title + " " + channel + " " + uploader).lower()

                        if '/shorts/' in url or '/clip/' in url:
                            continue
                        if title_is_blocked(low):
                            continue
                        if dur < MIN_DURATION or dur > MAX_DURATION:
                            continue
                        if not looks_like_original_channel(channel, uploader, artist_name):
                            continue
                        if not title_matches_song(title, track_name):
                            continue

                        score = 0
                        if 'official audio' in low: score += 10
                        if ' - topic' in channel.lower() or channel.lower().endswith('topic'): score += 9
                        if 'vevo' in channel.lower() or 'vevo' in uploader.lower(): score += 8
                        mid = (MIN_DURATION + MAX_DURATION) / 2
                        score -= abs(dur - mid) / 60.0
                        ranked.append((score, e))
                    ranked.sort(key=lambda x: x[0], reverse=True)

                    for _, entry in ranked:
                        url = entry['webpage_url']
                        print(f"✅ Candidate: {entry.get('title','?')} [{entry.get('duration',0)}s] / {entry.get('channel','?')} ({client})")
                        try:
                            ydl.download([url])
                        except Exception as de:
                            msg = str(de)
                            if "403" in msg or "Forbidden" in msg or "Sign in to confirm" in msg:
                                print("🚧 403/Sign-in issue — rotating client / loosening format…")
                                format_loosened = True
                                time.sleep(0.8)
                                continue
                            if "Requested format is not available" in msg:
                                print("↩️ Format not available — loosening to bestaudio/best and retrying…")
                                format_loosened = True
                                time.sleep(0.5)
                                continue
                            print(f"⚠️ Download error, trying next candidate: {de}")
                            time.sleep(0.5)
                            continue

                        # Verify final duration; delete and retry next if short/long
                        out = target_mp3
                        if not out.exists():
                            cands = list(artist_dir.glob(f"{filename_base}.*"))
                            cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                            if cands:
                                out = cands[0]

                        actual = get_audio_duration(str(out))
                        if actual < MIN_DURATION or actual > MAX_DURATION:
                            print(f"🗑️ Bad duration after download ({actual}s). Deleting and trying next.")
                            try: os.remove(str(out))
                            except Exception: pass
                            continue

                        # Proper tags + Apple Music add
                        tag_mp3(str(out), SAVE_DIR, artist_name, track_name)
                        add_to_apple_music_if_new(str(out))

                        # Record in de-dupe index so later tracks get skipped
                        remember(artist_name, track_name)
                        return

            except RuntimeError as rte:
                print(f"❌ {rte}")
                return
            except Exception as e:
                print(f"⚠️ YouTube search/download failed for: {q} ({client}) | {e}")
                time.sleep(0.6)
                continue

    print(f"🚫 No suitable version found: {artist_name} - {track_name}")

# ---------- MAIN ----------
if __name__ == "__main__":
    # Build the in-memory index of existing tracks (legacy + new layout)
    build_library_index()

    # Optional cleanup/retag pass:
    # clean_and_tag_existing_folder()

    tracks = get_tracks_from_genre_playlists(GENRE_KEYWORD)
    for title, artist, explicit in tqdm(tracks, desc="Downloading"):
        try_download(title, artist, explicit)

    print("🎉 Done.")
