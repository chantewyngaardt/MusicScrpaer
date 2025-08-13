import os
import subprocess
import eyed3
from yt_dlp import YoutubeDL
from tqdm import tqdm

# ---------- CONFIG ----------
GENRE_KEYWORD = "deep house"       # Search keyword
SAVE_DIR = "Deep House"            # Save folder
MIN_DURATION = 120                 # Min duration in seconds (2 mins)
MAX_DURATION = 720                 # Max duration in seconds (12 mins)

EXCLUDE_KEYWORDS = [
    "k-pop", "rock", "edm", "rap", "hip hop", "r&b", "reggae",
    "trap", "jazz", "indie", "lofi", "study", "sleep", "afro-house"
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

# ---------- MP3 TAGGING ----------
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
        print(f"🏷️ Tagged: {title} ({artist}) as {genre}")
    except Exception as e:
        print(f"❌ Tagging error for {filepath}: {e}")

# ---------- UTILS ----------
def sanitize_filename(text):
    for char in ['/', '\\', ':', '*', '?', '"', '<', '>', '|']:
        text = text.replace(char, '-')
    return text.strip()

def normalize_remaster_title(title):
    # Remove common junk from titles
    for junk in ["(Original Mix)", "[Original Mix]", "(Extended Mix)", "[Extended Mix]"]:
        title = title.replace(junk, "").strip()
    return title

# ---------- MAIN SOUNDClOUD DOWNLOADER ----------
def download_from_soundcloud(keyword):
    search_query = f"scsearch20:{keyword}"  # SoundCloud search shortcut

    # Step 1: Search for tracks
    with YoutubeDL({'quiet': True, 'no_warnings': True}) as ydl:
        print(f"🔍 Searching SoundCloud for: {keyword}")
        info = ydl.extract_info(search_query, download=False)
        results = info.get('entries', [])

    if not results:
        print("⚠️ No results found.")
        return

    # Step 2: Process results
    for entry in tqdm(results, desc="Downloading"):
        if not entry:
            continue

        title = entry.get('title', '')
        uploader = entry.get('uploader', '')
        duration = entry.get('duration', 0)
        track_url = entry.get('webpage_url')

        # Filters
        if any(bad in title.lower() for bad in EXPLICIT_TITLE_BLOCKLIST):
            print(f"❌ Skipping explicit: {title}")
            continue
        if any(bad in title.lower() for bad in EXCLUDE_KEYWORDS) or any(bad in uploader.lower() for bad in EXCLUDE_KEYWORDS):
            print(f"⏩ Skipping excluded keyword: {title}")
            continue
        if duration < MIN_DURATION or duration > MAX_DURATION:
            print(f"⏩ Skipping by duration: {title} ({duration}s)")
            continue

        # Prepare filename
        clean_title = normalize_remaster_title(title)
        filename_base = sanitize_filename(f"{uploader} - {clean_title}")
        target_path = os.path.join(SAVE_DIR, f"{filename_base}.mp3")

        if os.path.exists(target_path):
            print(f"⏩ Already downloaded: {filename_base}")
            continue

        # Download the track
        download_opts = {
            'format': 'bestaudio/best',
            'outtmpl': f"{SAVE_DIR}/{filename_base}.%(ext)s",
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '0',
            }]
        }
        print(f"✅ Downloading: {title} from {uploader}")
        with YoutubeDL(download_opts) as ydl:
            ydl.download([track_url])

        # Tag and add to Apple Music
        tag_mp3(target_path, SAVE_DIR, uploader, clean_title)
        add_to_apple_music_if_new(target_path)

# ---------- RUN ----------
if __name__ == "__main__":
    download_from_soundcloud(GENRE_KEYWORD)
    print("🎉 Done.")
