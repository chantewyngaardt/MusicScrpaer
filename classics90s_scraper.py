#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import time
import subprocess
from pathlib import Path

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from tqdm import tqdm
from yt_dlp import YoutubeDL
import eyed3
import mutagen

# ---------- CONFIG ----------
SPOTIFY_CLIENT_ID = "069f9949534e4563b074cf3c4ccebb28"
SPOTIFY_CLIENT_SECRET = "f453fa3e48444183831cddc07cec5af8"

GENRE_KEYWORD = "90s classics"
SAVE_DIR = "90s Classics"
MAX_TRACKS = 1000

# Duration gate (seconds)
MIN_DURATION = 153   # 2:33
MAX_DURATION = 720   # 12:00

# Playlist/topic filters
EXCLUDE_PLAYLIST_KEYWORDS = [
    "k-pop", "rock", "edm", "rap", "hip hop", "r&b", "reggae",
    "trap", "jazz", "indie", "afro", "amapiano"
]

CLEAN_FILTER_KEYWORDS = [
    "clean", "radio edit", "no cursing", "clean version", "lyrics clean"
]

# Skip junk, previews, mixes, albums, etc.
TITLE_BLOCKLIST = [
    "explicit", "dirty", "uncut", "nsfw", "raw",
    "live", "karaoke", "instrumental", "remix", "cover", "performance",
    "mix", "megamix", "dj set", "set", "continuous mix",
    "full album", "album", "compilation", "playlist",
    "shorts", "preview", "snippet", "sample"
]
LONGFORM_PATTERNS = [
    r"\b\d{1,2}\s*(hour|hr|h)\b",
    r"\b\d{2,}\s*min\b",
    r"\b(best\s+of|nonstop|continuous)\b",
]

os.makedirs(SAVE_DIR, exist_ok=True)

# Show eyeD3 warnings like “Non standard genre name”
try:
    eyed3.log.setLevel("WARNING")
except Exception:
    pass

# ---------- HELPERS ----------
def sanitize_filename(text: str) -> str:
    for ch in ['/', '\\', ':', '*', '?', '"', '<', '>', '|']:
        text = text.replace(ch, '-')
    return text.strip()

def get_audio_duration(filepath: str) -> int:
    """Return duration in seconds (int) or 0 if unknown."""
    try:
        af = mutagen.File(filepath)
        return int(af.info.length) if af and af.info else 0
    except Exception:
        return 0

def is_irrelevant_playlist(name, desc) -> bool:
    full = f"{name} {desc}".lower()
    return any(bad in full for bad in EXCLUDE_PLAYLIST_KEYWORDS)

def has_longform_pattern(text: str) -> bool:
    t = text.lower()
    return any(re.search(p, t) for p in LONGFORM_PATTERNS)

def tag_mp3(filepath: str, genre: str, artist: str = None, title: str = None):
    try:
        audiofile = eyed3.load(filepath)
        if not audiofile:
            print(f"⚠️ Couldn't load: {filepath}")
            return
        if audiofile.tag is None:
            audiofile.initTag()
        audiofile.tag.genre = genre
        if artist:
            audiofile.tag.artist = artist
        if title:
            audiofile.tag.title = title
        audiofile.tag.save()
        basename = os.path.basename(filepath)
        print(f"🏷️ Tagged file: {basename} [{genre}]")
    except Exception as e:
        print(f"❌ Tagging error for {filepath}: {e}")

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
            subprocess.run(
                ["osascript", "-e",
                 f'tell application "Music" to add POSIX file "{os.path.abspath(filepath)}" to library playlist 1'],
                check=True, timeout=10
            )
            print(f"🎶 Added to Apple Music: {os.path.basename(filepath)}")
        else:
            print(f"⏩ Already in Apple Music: {os.path.basename(filepath)}")
    except subprocess.TimeoutExpired:
        print(f"⏱️ Apple Music add timed out: {filepath}")
    except Exception as e:
        print(f"⚠️ Failed to add to Apple Music: {filepath} | {e}")

# ---------- CLEAN EXISTING FOLDER ----------
def clean_and_tag_existing_folder():
    for path in sorted(Path(SAVE_DIR).glob("*.mp3")):
        dur = get_audio_duration(str(path))
        if dur == 0:
            print(f"🗑️ Removing unreadable file: {path.name}")
            try: path.unlink()
            except Exception: pass
            continue
        if dur < MIN_DURATION or dur > MAX_DURATION:
            print(f"🗑️ Removing out-of-range file ({dur}s): {path.name}")
            try: path.unlink()
            except Exception: pass
            continue
        # Keep — set genre; infer artist-title if possible
        stem = path.stem
        artist, title = (stem.split(" - ", 1) + [None])[:2] if " - " in stem else (None, stem)
        tag_mp3(str(path), SAVE_DIR, artist, title)
        add_to_apple_music_if_new(str(path))

# ---------- SPOTIFY AUTH ----------
auth_manager = SpotifyClientCredentials(
    client_id=SPOTIFY_CLIENT_ID,
    client_secret=SPOTIFY_CLIENT_SECRET
)
sp = spotipy.Spotify(auth_manager=auth_manager)

def get_tracks_from_genre_playlists(keyword: str):
    print(f"🔍 Searching for playlists related to: {keyword}")
    playlists = sp.search(q=keyword, type="playlist", limit=20)
    tracks = []
    for pl in playlists.get("playlists", {}).get("items", []):
        if not pl:
            continue
        name = pl.get("name", "")
        desc = pl.get("description", "")
        if is_irrelevant_playlist(name, desc):
            continue
        pid = pl.get("id")
        if not pid:
            continue
        try:
            results = sp.playlist_tracks(pid)
            for item in results["items"]:
                track = item.get("track")
                if not track:
                    continue
                artist = track["artists"][0]["name"]
                title = track["name"]
                # Spotify-side duration gate (keeps queue clean)
                dur = int((track.get("duration_ms") or 0) / 1000)
                if dur < MIN_DURATION or dur > MAX_DURATION:
                    continue
                explicit = track.get("explicit", False)
                tracks.append((title, artist, explicit))
                if len(tracks) >= MAX_TRACKS:
                    break
        except Exception:
            continue
        if len(tracks) >= MAX_TRACKS:
            break
    print(f"🎵 Found {len(tracks)} tracks")
    return tracks

# ---------- YOUTUBE SEARCH/DOWNLOAD ----------
def title_is_blocked(t: str) -> bool:
    t = (t or "").lower()
    if any(b in t for b in TITLE_BLOCKLIST):
        return True
    if has_longform_pattern(t):
        return True
    return False

def try_download(track_name: str, artist_name: str, is_explicit: bool):
    filename_base = sanitize_filename(f"{artist_name} - {track_name}")
    target_mp3 = Path(SAVE_DIR) / f"{filename_base}.mp3"
    if target_mp3.exists():
        print(f"⏩ Already downloaded: {target_mp3.name}")
        return

    base_query = f"{artist_name} {track_name}"
    queries = [f"{base_query} {v}" for v in CLEAN_FILTER_KEYWORDS] if is_explicit else [base_query]
    # Nudge toward right versions
    queries = [f"{base_query} official audio", f"{base_query} full song"] + queries + [base_query]

    # Base yt-dlp opts (prefer m4a but we will fallback per-video if needed)
    base_ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'outtmpl': f"{SAVE_DIR}/{filename_base}.%(ext)s",
        'noplaylist': True,
        'default_search': 'ytsearch25',
        'cookiesfrombrowser': ('chrome',),   # ← your old working method
        'quiet': True,
        'no_warnings': True,
        'retries': 15,
        'fragment_retries': 15,
        'sleep_interval_requests': 0.6,
        'max_sleep_interval_requests': 1.2,
        'extractor_args': {
            'youtube': {
                'player_client': ['android']  # helps avoid 403/throttling
            }
        },
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '0',
        }]
    }

    with YoutubeDL(base_ydl_opts) as ydl:
        for q in queries:
            try:
                print(f"🔎 Searching: {q}")
                results = ydl.extract_info(f"ytsearch25:{q}", download=False).get('entries', [])
                # Rank candidates
                ranked = []
                for e in results:
                    if not e:
                        continue
                    title = (e.get('title') or '')
                    channel = (e.get('channel') or '')
                    dur = int(e.get('duration') or 0)
                    url = (e.get('webpage_url') or '')
                    low = title.lower() + " " + channel.lower()
                    if '/shorts/' in url or '/clip/' in url:
                        continue
                    if title_is_blocked(low):
                        continue
                    if dur < MIN_DURATION or dur > MAX_DURATION:
                        continue
                    score = 0
                    # Prefer Official Audio / Topic
                    if 'official audio' in low or ' - topic' in channel.lower():
                        score += 10
                    # Closer to mid-range duration
                    mid = (MIN_DURATION + MAX_DURATION) / 2
                    score -= abs(dur - mid) / 60.0
                    ranked.append((score, e))
                ranked.sort(key=lambda x: x[0], reverse=True)

                for _, entry in ranked:
                    url = entry['webpage_url']
                    print(f"✅ Candidate: {entry.get('title','?')} [{entry.get('duration',0)}s] / {entry.get('channel','?')}")
                    try:
                        # First try with preferred format chain
                        ydl.download([url])
                    except Exception as de:
                        msg = str(de)
                        if "Requested format is not available" in msg:
                            print("↩️ Falling back to any bestaudio for this video…")
                            # Retry this one video with a looser format
                            fallback_opts = dict(base_ydl_opts)
                            fallback_opts['format'] = 'bestaudio/best'
                            with YoutubeDL(fallback_opts) as ydl_fb:
                                ydl_fb.download([url])
                        else:
                            print(f"⚠️ Download error, trying next: {de}")
                            time.sleep(0.8)
                            continue

                    # Find produced file (after postprocess it should be .mp3)
                    out = target_mp3
                    if not out.exists():
                        # fallback to any file with same stem (in case ext differs briefly)
                        cands = sorted(Path(SAVE_DIR).glob(f"{filename_base}.*"),
                                       key=lambda p: p.stat().st_mtime, reverse=True)
                        if cands:
                            out = cands[0]

                    # Final duration check; delete if out-of-range; then try next candidate
                    actual = get_audio_duration(str(out))
                    if actual < MIN_DURATION or actual > MAX_DURATION:
                        print(f"🗑️ Bad duration after download ({actual}s). Deleting and trying next.")
                        try: os.remove(str(out))
                        except Exception: pass
                        continue

                    # Tag and add to Music
                    artist = artist_name
                    title = track_name
                    tag_mp3(str(out), SAVE_DIR, artist, title)
                    add_to_apple_music_if_new(str(out))
                    return

            except Exception as e:
                print(f"⚠️ YouTube search/download failed for: {q} | {e}")

    print(f"🚫 No suitable version found: {artist_name} - {track_name}")

# ---------- MAIN ----------
if __name__ == "__main__":
    # 1) Clean/tag current folder first (eyeD3 will log “Non standard genre name” as before)
    clean_and_tag_existing_folder()

    # 2) Fetch from Spotify & download
    tracks = get_tracks_from_genre_playlists(GENRE_KEYWORD)
    for title, artist, explicit in tqdm(tracks, desc="Downloading"):
        try_download(title, artist, explicit)

    print("🎉 Done.")
