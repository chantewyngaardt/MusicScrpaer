import os
import json
import subprocess
import re
import spotipy
import eyed3
from spotipy.oauth2 import SpotifyClientCredentials
from tqdm import tqdm
from yt_dlp import YoutubeDL

# ---------- CONFIG ----------
SPOTIFY_CLIENT_ID = "069f9949534e4563b074cf3c4ccebb28"
SPOTIFY_CLIENT_SECRET = "f453fa3e48444183831cddc07cec5af8"
GENRE_KEYWORD = "afro"  # change this for different genres
SAVE_DIR = "Afro-pop"
MAX_TRACKS = 1000
MIN_DURATION = 153  # 2:33 in seconds
MAX_DURATION = 720  # 12:00 in seconds

EXCLUDE_PLAYLIST_KEYWORDS = [
    "trap", "hip hop", "rap", "edm", "drill", "k-pop", "afrobeats",
    "reggae", "dancehall", "tiktok", "club", "amapiano",
    "explicit", "dirty", "modern"
]

CLEAN_FILTER_KEYWORDS = [
    "clean", "radio edit", "no cursing", "clean version", "lyrics clean"
]

BLOCKLIST_KEYWORDS = [
    "explicit", "dirty", "uncut", "nsfw", "raw",
    "live", "karaoke", "instrumental", "remix",
    "cover", "performance", "tribute", "medley"
]

LOG_FILE = "downloaded_tracks.json"
os.makedirs(SAVE_DIR, exist_ok=True)

# ---------- PERSISTENT LOG ----------
if os.path.exists(LOG_FILE):
    with open(LOG_FILE, "r") as f:
        downloaded_tracks = set(json.load(f))
else:
    downloaded_tracks = set()

def save_log():
    with open(LOG_FILE, "w") as f:
        json.dump(list(downloaded_tracks), f, indent=2)

# ---------- HELPERS: REMASTER NORMALIZATION ----------
REM_COMMENT_RE = re.compile(
    r'\s*-\s*(?:'
    r'(?:(?P<y1>\d{4})\s*(?P<label1>remaster(?:ed)?(?: version)?)|'
    r'(?P<label2>remaster(?:ed)?(?: version)?)\s*(?P<y2>\d{4}))'
    r')\s*$',
    re.IGNORECASE
)

def normalize_remaster_title(raw_title: str) -> str:
    """Convert '- Remastered 2016' style into '(Remastered 2016)'."""
    if "(" in raw_title and "remaster" in raw_title.lower():
        return raw_title  # already parenthesized
    m = REM_COMMENT_RE.search(raw_title)
    if not m:
        return raw_title
    base = REM_COMMENT_RE.sub("", raw_title).rstrip()
    year = m.group("y1") or m.group("y2")
    label = m.group("label1") or m.group("label2") or "Remastered"
    label = "Remastered" if "remaster" in label.lower() else label
    return f"{base} ({label} {year})" if year else f"{base} ({label})"

# ---------- APPLE MUSIC HELPERS ----------
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
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return result.stdout.strip() == "YES"

def add_to_apple_music_if_new(filepath, artist, title):
    try:
        if not is_in_apple_music_by_metadata(artist, title):
            subprocess.run([
                "osascript", "-e",
                f'tell application "Music" to add POSIX file "{os.path.abspath(filepath)}" to library playlist 1'
            ], check=True)
            print(f"🎶 Added to Apple Music: {artist} - {title}")
        else:
            print(f"⏩ Already in Apple Music: {artist} - {title}")
    except Exception as e:
        print(f"⚠️ Failed to add to Apple Music: {filepath} | {e}")

# ---------- GENRE & METADATA ----------
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

# ---------- TAG EXISTING FILES ----------
for filename in os.listdir(SAVE_DIR):
    if filename.lower().endswith('.mp3') and " - " in filename:
        # split on FIRST " - " so rest stays in title
        artist, title = filename.split(" - ", 1)
        title = title[:-4]  # strip .mp3
        title = normalize_remaster_title(title)
        filepath = os.path.join(SAVE_DIR, filename)
        tag_mp3(filepath, SAVE_DIR, artist, title)
        add_to_apple_music_if_new(filepath, artist, title)

# ---------- SPOTIFY AUTH ----------
auth_manager = SpotifyClientCredentials(
    client_id=SPOTIFY_CLIENT_ID,
    client_secret=SPOTIFY_CLIENT_SECRET
)
sp = spotipy.Spotify(auth_manager=auth_manager)

# ---------- PLAYLIST HELPERS ----------
def sanitize_filename(text):
    for char in ['/', '\\', ':', '*', '?', '"', '<', '>', '|']:
        text = text.replace(char, '-')
    return text.strip()

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

    print(f"🎵 Found {len(tracks)} total tracks.")
    return tracks

# ---------- YOUTUBE DOWNLOAD ----------
def try_download(track_name, artist_name, is_explicit):
    key = f"{artist_name}::{track_name}"
    if key in downloaded_tracks:
        print(f"⏩ Already processed: {artist_name} - {track_name}")
        return

    # Normalize title before building filename
    base_title = normalize_remaster_title(track_name)
    filename_base = sanitize_filename(f"{artist_name} - {base_title}")
    target_path = os.path.join(SAVE_DIR, f"{filename_base}.mp3")

    base_query = f"{artist_name} {track_name}"
    queries = [f"{base_query} {variant}" for variant in CLEAN_FILTER_KEYWORDS] if is_explicit else [base_query]
    queries.append(base_query)

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': f"{SAVE_DIR}/{filename_base}.%(ext)s",
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'default_search': 'ytsearch5',
        'cookiesfrombrowser': ('chrome',),
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '0',
        }]
    }

    with YoutubeDL(ydl_opts) as ydl:
        for q in queries:
            try:
                search_results = ydl.extract_info(f"ytsearch5:{q}", download=False)['entries']
                for entry in search_results:
                    title = entry.get('title', '').lower()
                    channel = entry.get('channel', '').lower()
                    duration = entry.get('duration', 0)

                    if any(bad in title for bad in BLOCKLIST_KEYWORDS) or any(bad in channel for bad in BLOCKLIST_KEYWORDS):
                        continue
                    if duration < MIN_DURATION or duration > MAX_DURATION:
                        continue

                    yt_rem = re.search(r'\(([^)]*remaster[^)]*)\)', entry.get('title', ''), re.IGNORECASE)
                    track_title_final = base_title
                    if yt_rem and "remaster" in yt_rem.group(1).lower() and "remaster" not in base_title.lower():
                        ytm = re.search(r'(\d{4})', yt_rem.group(1))
                        if ytm:
                            track_title_final = normalize_remaster_title(f"{track_name} - Remastered {ytm.group(1)}")

                    print(f"✅ Downloading: {entry['title']} from {entry['channel']}")
                    ydl.download([entry['webpage_url']])
                    tag_mp3(target_path, SAVE_DIR, artist_name, track_title_final)
                    add_to_apple_music_if_new(target_path, artist_name, track_title_final)
                    downloaded_tracks.add(key)
                    save_log()
                    return
            except Exception as e:
                print(f"⚠️ YouTube search failed for: {q} | {e}")

    print(f"🚫 No suitable version found: {artist_name} - {track_name}")

# ---------- MAIN ----------
track_list = get_tracks_from_genre_playlists(GENRE_KEYWORD)
for title, artist, explicit in tqdm(track_list, desc="Downloading"):
    try_download(title, artist, explicit)

print("🎉 Done.")
