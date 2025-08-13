import os
import subprocess
import spotipy
import eyed3
from spotipy.oauth2 import SpotifyClientCredentials
from tqdm import tqdm
from yt_dlp import YoutubeDL

# ---------- CONFIG ----------
SPOTIFY_CLIENT_ID = "069f9949534e4563b074cf3c4ccebb28"
SPOTIFY_CLIENT_SECRET = "f453fa3e48444183831cddc07cec5af8"
GENRE_KEYWORD = "old school rnb"
SAVE_DIR = "Old School RnB + Soul"
MAX_TRACKS = 1000

EXCLUDE_PLAYLIST_KEYWORDS = [
    "trap", "hip hop", "rap", "edm", "drill", "k-pop", "afrobeats",
    "reggae", "dancehall", "tiktok", "club", "amapiano",
    "explicit", "dirty", "modern"
]
CLEAN_FILTER_KEYWORDS = [
    "clean", "radio edit", "no cursing", "clean version", "lyrics clean"
]

EXPLICIT_TITLE_BLOCKLIST = [
    "explicit", "dirty", "uncut", "nsfw", "raw",
    "live", "karaoke", "instrumental", "remix", "cover", "performance"
]

os.makedirs(SAVE_DIR, exist_ok=True)

# ---------- APPLE MUSIC HELPERS ----------
def is_in_apple_music(filepath):
    """Check if a file is already in Apple Music."""
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
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return result.stdout.strip() == "YES"

def add_to_apple_music_if_new(filepath):
    """Add file to Apple Music if not already there."""
    try:
        if not is_in_apple_music(filepath):
            subprocess.run([
                "osascript", "-e",
                f'tell application "Music" to add POSIX file "{os.path.abspath(filepath)}" to library playlist 1'
            ], check=True)
            print(f"🎶 Added to Apple Music: {os.path.basename(filepath)}")
        else:
            print(f"⏩ Already in Apple Music: {os.path.basename(filepath)}")
    except Exception as e:
        print(f"⚠️ Failed to add to Apple Music: {filepath} | {e}")

# ---------- GENRE TAGGING ----------
def tag_mp3_with_genre(filepath, genre):
    try:
        audiofile = eyed3.load(filepath)
        if audiofile is None:
            print(f"⚠️ Couldn't load: {filepath}")
            return
        if audiofile.tag is None:
            audiofile.initTag()
        audiofile.tag.genre = genre
        audiofile.tag.save()
        print(f"🏷️ Tagged file: {os.path.basename(filepath)} with genre: {genre}")
    except Exception as e:
        print(f"❌ Tagging error for {filepath}: {e}")

# Tag all existing MP3s in folder before downloading
for filename in os.listdir(SAVE_DIR):
    if filename.lower().endswith('.mp3'):
        filepath = os.path.join(SAVE_DIR, filename)
        tag_mp3_with_genre(filepath, SAVE_DIR)
        add_to_apple_music_if_new(filepath)

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
    filename_base = sanitize_filename(f"{artist_name} - {track_name}")
    target_path = os.path.join(SAVE_DIR, f"{filename_base}.mp3")

    # Skip if file already exists
    if os.path.exists(target_path):
        print(f"⏩ Already downloaded: {filename_base}")
        return

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
            print(f"🔍 Trying: {q}")
            try:
                search_results = ydl.extract_info(f"ytsearch5:{q}", download=False)['entries']
                for entry in search_results:
                    title = entry.get('title', '').lower()
                    if any(bad in title for bad in EXPLICIT_TITLE_BLOCKLIST):
                        print(f"❌ Skipping: {entry['title']}")
                        continue
                    print(f"✅ Downloading: {entry['title']}")
                    ydl.download([entry['webpage_url']])
                    tag_mp3_with_genre(target_path, SAVE_DIR)
                    add_to_apple_music_if_new(target_path)
                    return
            except Exception as e:
                print(f"⚠️ YouTube search failed for: {q} | {e}")

    print(f"🚫 No suitable version found: {filename_base}")

# ---------- MAIN ----------
track_list = get_tracks_from_genre_playlists(GENRE_KEYWORD)
for title, artist, explicit in tqdm(track_list, desc="Downloading"):
    try_download(title, artist, explicit)

print("🎉 Done.")
