#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Spotify -> YouTube (yt-dlp) music downloader (90s Classics example)
- Cleans existing folder: removes < MIN or > MAX files before tagging/adding
- Finds tracks from Spotify playlists by keyword
- Searches YouTube for full-length versions (prefers Official Audio/Topic)
- Skips Shorts/clips/previews, mixes/sets/megamixes/full albums
- Verifies duration after download; deletes out-of-range files
- Tags MP3s and (optionally) adds to Apple Music on macOS (with timeout)

Requirements:
  pip install spotipy yt-dlp eyed3 mutagen tqdm
Also requires ffmpeg in PATH.
"""

import os
import json
import subprocess
import re
from pathlib import Path

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import eyed3
from tqdm import tqdm
from yt_dlp import YoutubeDL
import mutagen

# ---------- CONFIG ----------
SPOTIFY_CLIENT_ID = "6af04a18410144c1a173bf34d6563511"
SPOTIFY_CLIENT_SECRET = "b01f41462cf04bb6856e8a1062c56e2d"

GENRE_KEYWORD = "70s classics"
SAVE_DIR = "70s Classics"

MAX_TRACKS = 1000

MIN_DURATION = 153  # 2:33 in seconds
MAX_DURATION = 720  # 12:00 in seconds

# YouTube search behavior
USE_BROWSER_COOKIES = False
BROWSER_FOR_COOKIES = "chrome"
SEARCH_RESULTS_PER_QUERY = 25

EXCLUDE_PLAYLIST_KEYWORDS = [
    "k-pop", "edm", "rap", "hip hop", "r&b", "reggae",
    "trap", "jazz", "indie", "afro", "amapiano", "afrikaans"
]

CLEAN_FILTER_KEYWORDS = [
    "clean", "radio edit", "no cursing", "clean version", "lyrics clean"
]

BLOCKLIST_KEYWORDS = [
    "explicit", "dirty", "uncut", "nsfw", "raw",
    "live", "karaoke", "instrumental", "remix",
    "cover", "performance", "tribute", "medley"
]

# Block obvious junk & long-form content
BLOCKLIST_KEYWORDS = [
    "explicit", "dirty", "uncut", "nsfw", "raw",
    "karaoke", "instrumental", "remix", "cover",
    "performance", "tribute", "medley",
    "mix", "megamix", "dj set", "set", "continuous mix",
    "full album", "album", "compilation", "playlist",
    "concert", "live set", "live"
]

# Patterns that strongly imply “mix / too long”
LONGFORM_PATTERNS = [
    r"\b\d{1,2}\s*(hour|hr|h)\b",      # 1 hour, 2hr, 3 h
    r"\b\d{2,}\s*min\b",               # 60 min, 75 min
    r"\b(best\s+of|nonstop|continuous)\b",
]

LOG_FILE = "downloaded_tracks.json"

# ---------- PREP ----------
os.makedirs(SAVE_DIR, exist_ok=True)

# ---------- PERSISTENT LOG ----------
if os.path.exists(LOG_FILE):
    with open(LOG_FILE, "r") as f:
        try:
            downloaded_tracks = set(json.load(f))
        except Exception:
            downloaded_tracks = set()
else:
    downloaded_tracks = set()

def save_log():
    with open(LOG_FILE, "w") as f:
        json.dump(list(downloaded_tracks), f, indent=2)

# ---------- HELPERS ----------
REM_COMMENT_RE = re.compile(
    r'\s*-\s*(?:'
    r'(?:(?P<y1>\d{4})\s*(?P<label1>remaster(?:ed)?(?: version)?)|'
    r'(?P<label2>remaster(?:ed)?(?: version)?)\s*(?P<y2>\d{4}))'
    r')\s*$',
    re.IGNORECASE
)

def normalize_remaster_title(raw_title: str) -> str:
    if "(" in raw_title and "remaster" in raw_title.lower():
        return raw_title
    m = REM_COMMENT_RE.search(raw_title)
    if not m:
        return raw_title
    base = REM_COMMENT_RE.sub("", raw_title).rstrip()
    year = m.group("y1") or m.group("y2")
    label = m.group("label1") or m.group("label2") or "Remastered"
    label = "Remastered" if "remaster" in label.lower() else label
    return f"{base} ({label} {year})" if year else f"{base} ({label})"

def file_duration_seconds(path):
    try:
        audio = mutagen.File(path)
        return int(audio.info.length) if audio and audio.info else 0
    except Exception:
        return 0

def sanitize_filename(text):
    for char in ['/', '\\', ':', '*', '?', '"', '<', '>', '|']:
        text = text.replace(char, '-')
    return text.strip()

# ---------- APPLE MUSIC (macOS) ----------
def is_in_apple_music_by_metadata(artist, title):
    script = f'''
    tell application "Music"
        set trackList to every track of library playlist 1 whose artist is "{artist}" and name is "{title}"
        if (count of trackList) > 0 then
            return "YES"
        else
            return "NO"
        end if
    end tell
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10
        )
        return result.stdout.strip() == "YES"
    except subprocess.TimeoutExpired:
        print("⏱️ AppleScript metadata check timed out; assuming not present.")
        return False
    except Exception:
        return False

def add_to_apple_music_if_new(filepath, artist, title):
    try:
        if not is_in_apple_music_by_metadata(artist, title):
            subprocess.run(
                ["osascript", "-e",
                 f'tell application "Music" to add POSIX file "{os.path.abspath(filepath)}" to library playlist 1'],
                check=True, timeout=10
            )
            print(f"🎶 Added to Apple Music: {artist} - {title}")
        else:
            print(f"⏩ Already in Apple Music: {artist} - {title}")
    except subprocess.TimeoutExpired:
        print(f"⏱️ Apple Music add timed out: {filepath}")
    except Exception as e:
        print(f"⚠️ Failed to add to Apple Music: {filepath} | {e}")

# ---------- TAGGING ----------
def tag_mp3(filepath, genre, artist, title):
    try:
        audiofile = eyed3.load(filepath)
        if audiofile is None:
            print(f"⚠️ Couldn't load: {filepath}")
            return
        if audiofile.tag is None:
            audiofile.initTag()
        audiofile.tag.genre = genre
        audiofile.tag.artist = artist
        audiofile.tag.title = title
        audiofile.tag.save()
        print(f"🏷️ Tagged file: {artist} - {title} [{genre}]")
    except Exception as e:
        print(f"❌ Tagging error for {filepath}: {e}")

# ---------- CLEAN EXISTING FOLDER BEFORE ANYTHING ----------
def clean_and_tag_existing_folder():
    for path in sorted(Path(SAVE_DIR).glob("*.mp3")):
        dur = file_duration_seconds(str(path))
        if dur == 0:
            # unreadable; drop it
            print(f"🗑️ Removing unreadable file: {path.name}")
            try: path.unlink()
            except Exception: pass
            continue
        if dur < MIN_DURATION or dur > MAX_DURATION:
            print(f"🗑️ Removing out-of-range file ({dur}s): {path.name}")
            try: path.unlink()
            except Exception: pass
            continue
        # Keep: (re)tag + add
        name = path.name[:-4]  # strip .mp3
        if " - " in name:
            artist, title = name.split(" - ", 1)
        else:
            artist, title = "Unknown", name
        title = normalize_remaster_title(title)
        tag_mp3(str(path), SAVE_DIR, artist, title)
        add_to_apple_music_if_new(str(path), artist, title)

# ---------- SPOTIFY ----------
auth_manager = SpotifyClientCredentials(
    client_id=SPOTIFY_CLIENT_ID,
    client_secret=SPOTIFY_CLIENT_SECRET
)
sp = spotipy.Spotify(auth_manager=auth_manager)

def is_irrelevant_playlist(name, desc):
    full_text = f"{name} {desc}".lower()
    return any(bad in full_text for bad in EXCLUDE_PLAYLIST_KEYWORDS)

def get_tracks_from_genre_playlists(keyword):
    print(f"🔍 Searching for playlists related to: {keyword}")
    playlists = sp.search(q=keyword, type="playlist", limit=20)
    tracks = []
    for playlist in playlists.get("playlists", {}).get("items", []):
        if not playlist or not isinstance(playlist, dict):
            continue
        name = playlist.get("name", "")
        desc = playlist.get("description", "")
        if is_irrelevant_playlist(name, desc):
            print(f"⛔ Skipping playlist: {name}")
            continue
        print(f"📁 Reading playlist: {name}")
        playlist_id = playlist.get("id")
        if not playlist_id:
            continue
        try:
            results = sp.playlist_tracks(playlist_id)
            for item in results["items"]:
                track = item.get("track")
                if not track or not track.get("name"):
                    continue
                duration_sec = (track.get("duration_ms") or 0) / 1000
                if duration_sec < MIN_DURATION or duration_sec > MAX_DURATION:
                    print(f"⏩ Skipping (duration {duration_sec:.0f}s): {track.get('name','?')} by {track.get('artists',[{'name':'?'}])[0]['name']}")
                    continue
                artist = track["artists"][0]["name"]
                title = track["name"]
                is_explicit = track.get("explicit", False)
                tracks.append((title, artist, is_explicit))
                if len(tracks) >= MAX_TRACKS:
                    break
        except Exception as e:
            print(f"⚠️ Failed to fetch tracks from playlist: {name} | {e}")
        if len(tracks) >= MAX_TRACKS:
            break
    print(f"🎵 Found {len(tracks)} total tracks after filtering.")
    return tracks

# ---------- YOUTUBE FILTERS ----------
def has_longform_pattern(text):
    t = text.lower()
    return any(re.search(p, t) for p in LONGFORM_PATTERNS)

def is_bad_source(entry):
    url = (entry.get('webpage_url') or '').lower()
    ch  = (entry.get('channel') or '').lower()
    ttl = (entry.get('title') or '').lower()

    if entry.get('is_live'):
        return True
    if '/shorts/' in url or '/clip/' in url:
        return True
    if any(n in ttl for n in ['preview', 'snippet', 'sample']):
        return True
    if any(n in ch for n in ['shorts', 'clips', 'samples']):
        return True

    if any(b in ttl for b in BLOCKLIST_KEYWORDS) or any(b in ch for b in BLOCKLIST_KEYWORDS):
        return True
    if has_longform_pattern(ttl):
        return True
    return False

def good_audio_entry(entry):
    ttl = (entry.get('title') or '').lower()
    ch  = (entry.get('channel') or '').lower()
    badges = ['official audio', 'audio only', 'topic']
    return any(b in ttl for b in badges) or ' - topic' in ch

# ---------- DOWNLOAD ----------
def try_download(track_name, artist_name, is_explicit):
    key = f"{artist_name}::{track_name}"
    if key in downloaded_tracks:
        print(f"⏩ Already processed: {artist_name} - {track_name}")
        return

    base_title = normalize_remaster_title(track_name)
    filename_base = sanitize_filename(f"{artist_name} - {base_title}")
    expected_mp3 = Path(SAVE_DIR) / f"{filename_base}.mp3"

    base_query = f"{artist_name} {track_name}"
    queries = [f"{base_query} {variant}" for variant in CLEAN_FILTER_KEYWORDS] if is_explicit else [base_query]
    queries = [f"{base_query} official audio", f"{base_query} full song"] + queries + [base_query]

    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'outtmpl': f"{SAVE_DIR}/{filename_base}.%(ext)s",
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'default_search': f'ytsearch{SEARCH_RESULTS_PER_QUERY}',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '0',
        }],
        'retries': 10,
        'fragment_retries': 10,
    }
    if USE_BROWSER_COOKIES:
        ydl_opts['cookiesfrombrowser'] = (BROWSER_FOR_COOKIES,)

    with YoutubeDL(ydl_opts) as ydl:
        for q in queries:
            try:
                print(f"🔎 Searching: {q}")
                results = ydl.extract_info(f"ytsearch{SEARCH_RESULTS_PER_QUERY}:{q}", download=False)['entries']

                ranked = []
                for e in results:
                    dur = e.get('duration') or 0
                    if is_bad_source(e):
                        continue
                    if dur < MIN_DURATION or dur > MAX_DURATION:
                        continue
                    score = 0
                    if good_audio_entry(e):
                        score += 10
                    mid = (MIN_DURATION + MAX_DURATION) / 2
                    score -= abs(dur - mid) / 60.0
                    ranked.append((score, e))
                ranked.sort(key=lambda x: x[0], reverse=True)

                for _, entry in ranked:
                    print(f"✅ Candidate: {entry.get('title','?')} [{entry.get('duration',0)}s] / {entry.get('channel','?')}")
                    ydl.download([entry['webpage_url']])

                    out_path = expected_mp3
                    if not out_path.exists():
                        candidates = sorted(Path(SAVE_DIR).glob(f"{filename_base}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
                        if candidates:
                            out_path = candidates[0]

                    actual = file_duration_seconds(str(out_path))
                    if actual < MIN_DURATION or actual > MAX_DURATION:
                        print(f"🗑️ Bad duration after download ({actual}s). Deleting and trying next result.")
                        try: os.remove(str(out_path))
                        except Exception: pass
                        continue

                    tag_mp3(str(out_path), SAVE_DIR, artist_name, base_title)
                    add_to_apple_music_if_new(str(out_path), artist_name, base_title)
                    downloaded_tracks.add(key)
                    save_log()
                    return

            except Exception as e:
                print(f"⚠️ YouTube search/download failed for: {q} | {e}")

    print(f"🚫 No suitable version found: {artist_name} - {track_name}")

# ---------- MAIN ----------
if __name__ == "__main__":
    # 1) Clean up existing folder so short/long files don’t get re-added
    clean_and_tag_existing_folder()

    # 2) Fetch and download
    track_list = get_tracks_from_genre_playlists(GENRE_KEYWORD)
    for title, artist, explicit in tqdm(track_list, desc="Downloading"):
        try_download(title, artist, explicit)

    print("🎉 Done.")
