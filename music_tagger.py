#!/usr/bin/env python3
"""
music_tagger.py - Advanced Music Metadata Tagger & Library Organizer
Designed for Jarvis Bot, Navidrome & Jellyfin on MacServer.

Pipeline:
  1. Option 3A: Acoustic Fingerprint via AcoustID (fpcalc) + MusicBrainz lookup
  2. Option 3B: Online Metadata Verification via iTunes Search API (with confidence matching)
  3. Option 1: Rule-based Regex & Heuristic Fallback (strips noise, detects OST, swaps Artist/Title)
  4. ID3v2.3 Tagging (Mutagen) + High-Res Cover Art Embedding (APIC)
  5. Library Organizing: /mnt/data/data/media/music/{Artist}/{Album}/{Title}.mp3
"""

import os
import sys
import re
import json
import glob
import uuid
import shutil
import subprocess
import urllib.request
import urllib.parse
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

import mutagen
from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TRCK, APIC, ID3NoHeaderError
import musicbrainzngs

os.environ["PATH"] = f"/home/dongkhoi/.local/bin:{os.environ.get('PATH', '')}"

try:
    import acoustid
except ImportError:
    acoustid = None

MUSIC_DIR = "/mnt/data/data/media/music"
STAGING_DIR = "/mnt/data/data/media/music/.staging"
ACOUSTID_API_KEY = "cSpUJKpD"

musicbrainzngs.set_useragent("JarvisServer", "1.0", "jarvis@macserver.local")

# Noise patterns commonly found in YouTube music titles
NOISE_PATTERNS = [
    r'(?i)\s*[\(\[\{]\s*(official\s*(music\s*video|mv|audio|lyric(\s*video)?|video|visualizer)?|mv|m/v|lyrics?|audio|visualizer|4k|hd|remastered|beat|karaoke|live|prod\.?.*?|ost)\s*[\)\]\}]',
    r'(?i)\s*[\|\-•/]\s*(official\s*(music\s*video|mv|audio|lyric(\s*video)?|video|visualizer)?|mv|m/v|lyrics?|visualizer|audio|4k|hd|remastered).*$',
    r'(?i)\s*[\(\[]\s*(bản\s*(chính\s*thức|audio|gốc|live|acoustics?)|vietsub|nhạc\s*phim)\s*[\)\]]',
    r'(?i)\s*\|\s*maydays\s*$',
    r'(?i)\s*\|\s*nguyễn\s*hùng\s*$',
]

# Known YouTube music distributors/aggregators that are NOT artists
GENERIC_UPLOADERS = {
    "mounter", "viechannel", "viechannel music", "1989s", "1989s entertainment",
    "metub", "metub network", "pops music", "pops worldwide", "yeah1", "yeah1 music",
    "hãng đĩa thời đại", "times records", "d&m music", "ct bắp", "yin yang media",
    "zing mp3", "nhaccuatui", "nam viet music", "cty bắp media", "acv music",
    "lofi chill", "chillies", "viva music", "bh media"
}

def clean_filename_str(text: str) -> str:
    """Loại bỏ ký tự không hợp lệ trên filesystem."""
    text = re.sub(r'[\\/*?:"<>|]', '', text)
    return text.strip().strip('.')

def clean_title_heuristics(raw_title: str) -> str:
    """Làm sạch tiêu đề thô bằng regex loại bỏ noise."""
    title = raw_title
    for pat in NOISE_PATTERNS:
        title = re.sub(pat, '', title)
    title = re.sub(r'\s+', ' ', title).strip()
    title = title.strip(' -|•')
    return title

def string_similarity(a: str, b: str) -> float:
    """Đo độ tương đồng chuỗi (0.0 -> 1.0)."""
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()

def identify_ost_album(title: str) -> Optional[Tuple[str, str]]:
    """Phát hiện nếu bài hát là nhạc phim (OST). Trả về (clean_title, album_name)."""
    ost_match = re.search(r'[\(\[\{]([^\)\]\}]+?(?:OST|Original Soundtrack|Nhạc phim))[^\)\]\}]*[\)\]\}]', title, re.IGNORECASE)
    if ost_match:
        album = ost_match.group(1).strip()
        cleaned_t = re.sub(re.escape(ost_match.group(0)), '', title).strip(' -|•')
        return cleaned_t, album
    return None

def is_artist_match(expected: str, candidate: str) -> bool:
    """Kiểm tra độ tương đồng ca sĩ, đặc biệt tránh nhầm lẫn các tên tiếng Việt (vd: Nguyễn Hùng != Nguyễn Phi Hùng != Nguyễn Hưng)."""
    if not expected or expected == "Various Artists":
        return True

    exp = expected.lower().strip()
    cand = candidate.lower().strip()

    if exp == cand:
        return True

    # Kiểm tra tiền tố có phân cách (vd: "Nguyễn Hùng feat. X" hoặc "Sơn Tùng M-TP (Official)")
    if cand.startswith(exp) and (len(cand) == len(exp) or cand[len(exp)] in " ,-(/"):
        return True
    if exp.startswith(cand) and (len(exp) == len(cand) or exp[len(cand)] in " ,-(/"):
        return True

    # Tách từ sau khi loại bỏ dấu câu
    exp_words = [re.sub(r'[^a-z0-9à-ỹ]', '', w) for w in exp.split() if re.sub(r'[^a-z0-9à-ỹ]', '', w)]
    cand_words = [re.sub(r'[^a-z0-9à-ỹ]', '', w) for w in cand.split() if re.sub(r'[^a-z0-9à-ỹ]', '', w)]

    if exp_words == cand_words:
        return True

    # Ngoại lệ hợp lệ: Sơn Tùng vs Sơn Tùng M-TP
    if {"sơn", "tùng"}.issubset(set(exp_words)) and {"sơn", "tùng"}.issubset(set(cand_words)):
        return True
    # Ngoại lệ MAYDAYs vs Nguyễn Hùng
    if ("maydays" in exp and "nguyễn" in cand and "hùng" in cand) or ("maydays" in cand and "nguyễn" in exp and "hùng" in exp):
        return True

    return False

def is_title_match(expected: str, candidate: str) -> bool:
    """Kiểm tra độ khớp tiêu đề bài hát."""
    if not expected:
        return True
    exp = clean_title_heuristics(expected).lower().strip()
    cand = clean_title_heuristics(candidate).lower().strip()
    if exp == cand:
        return True
    if exp in cand or cand in exp:
        return True
    return string_similarity(exp, cand) >= 0.7

def query_itunes(query: str, expected_title: str = "", expected_artist: str = "") -> Optional[Dict[str, Any]]:
    """
    Tìm kiếm metadata chính thức từ iTunes Search API.
    Xác thực nghiêm ngặt cả tiêu đề lẫn ca sĩ để tránh nhận diện nhầm bài hát/ca sĩ khác tên.
    """
    try:
        clean_q = re.sub(r'[\(\[\{].*?[\)\]\}]', '', query)
        clean_q = re.sub(r'\b(official|mv|audio|video|lyrics|lyric)\b', '', clean_q, flags=re.I).strip()
        if not clean_q:
            return None

        url = f"https://itunes.apple.com/search?term={urllib.parse.quote(clean_q)}&entity=song&limit=5"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            results = data.get('results', [])
            if not results:
                return None
            
            for r in results:
                track = r.get('trackName', '')
                artist = r.get('artistName', '')
                album = r.get('collectionName', f"{track} - Single")
                art_url = r.get('artworkUrl100', '')
                if art_url:
                    art_url = art_url.replace('100x100bb.jpg', '1000x1000bb.jpg')
                track_num = r.get('trackNumber', 1)

                # 1. Xác thực Tiêu đề
                if expected_title and not is_title_match(expected_title, track):
                    continue

                # 2. Xác thực Ca sĩ
                if expected_artist and not is_artist_match(expected_artist, artist):
                    continue

                return {
                    "title": track,
                    "artist": artist,
                    "album": album,
                    "track_number": str(track_num),
                    "artwork_url": art_url,
                    "source": "iTunes API"
                }
    except Exception as e:
        print(f"[Tagger] iTunes search error: {e}", file=sys.stderr)
    return None

def query_acoustid(file_path: str) -> Optional[Dict[str, Any]]:
    """Nhận diện dấu vân tay âm thanh (Acoustic Fingerprint) bằng AcoustID / fpcalc."""
    if not acoustid:
        return None
    try:
        matches = list(acoustid.match(ACOUSTID_API_KEY, file_path))
        for score, recording_id, title, artist in matches:
            if score >= 0.65 and title and artist:
                album = f"{title} - Single"
                try:
                    mb_rec = musicbrainzngs.get_recording_by_id(recording_id, includes=['releases'])
                    releases = mb_rec.get('recording', {}).get('release-list', [])
                    if releases:
                        album = releases[0].get('title', album)
                except Exception:
                    pass
                
                return {
                    "title": title,
                    "artist": artist,
                    "album": album,
                    "track_number": "1",
                    "artwork_url": None,
                    "source": f"AcoustID Fingerprint (độ khớp: {int(score*100)}%)"
                }
    except Exception as e:
        print(f"[Tagger] AcoustID error: {e}", file=sys.stderr)
    return None

def fallback_heuristics(raw_title: str, uploader: str = "", query_hint: str = "") -> Dict[str, Any]:
    """Option 1 Fallback: Dùng regex & rules để bóc tách Artist, Title, Album sạch sẽ."""
    # 0. Loại bỏ tiền tố số thứ tự (vd: "01 - ", "01. ", "02 — ")
    clean_raw = re.sub(r'^\s*\d{1,3}\s*[\.\-—_]\s*', '', raw_title)
    cleaned = clean_title_heuristics(clean_raw)
    
    # 1. Kiểm tra OST (dạng ngoặc hoặc dạng gạch nối)
    ost_res = identify_ost_album(cleaned)
    album = "Single"
    if ost_res:
        cleaned, album = ost_res
    else:
        ost_split = re.search(r'\s*[\-—]\s*([^\-—]+?(?:OST|Original Soundtrack|Nhạc phim)[^\-—]*)$', cleaned, re.I)
        if ost_split:
            album = ost_split.group(1).strip()
            cleaned = re.sub(re.escape(ost_split.group(0)), '', cleaned).strip(' -|•—')

    artist = ""
    title = cleaned

    clean_uploader = re.sub(r'\s*-\s*topic$', '', uploader, flags=re.I).strip()

    # 2. Bóc tách Title và Artist khi có phân tách ' - '
    if " - " in cleaned or "-" in cleaned:
        sep = " - " if " - " in cleaned else "-"
        parts = cleaned.split(sep, 1)
        part0 = parts[0].strip()
        part1 = parts[1].strip()

        part0_lower = part0.lower()
        part1_lower = part1.lower()
        hint_lower = query_hint.lower() if query_hint else ""

        # Kiểm tra xem part nào là Artist dựa trên query_hint hoặc uploader
        if hint_lower and (hint_lower in part1_lower or "nguyễn hùng" in part1_lower or "maydays" in part1_lower):
            artist = part1
            title = part0
        elif hint_lower and (hint_lower in part0_lower or "nguyễn hùng" in part0_lower or "maydays" in part0_lower):
            artist = part0
            title = part1
        elif clean_uploader and (clean_uploader.lower() in part1_lower):
            artist = part1
            title = part0
        elif clean_uploader and (clean_uploader.lower() in part0_lower):
            artist = part0
            title = part1
        else:
            # Mặc định theo quy ước YouTube phổ biến: "Artist - Title"
            artist = part0
            title = part1
    else:
        # Không có dấu gạch ngang
        if query_hint and query_hint.lower() in raw_title.lower():
            artist = query_hint.strip()
            title = re.sub(re.escape(artist), '', title, flags=re.I).strip(' -|•')
        elif clean_uploader and clean_uploader.lower() not in GENERIC_UPLOADERS:
            artist = clean_uploader
        else:
            artist = "Various Artists"

    # Quy tắc nghiệp vụ ưu tiên:
    # 1. MAYDAYs là nhóm của Nguyễn Hùng -> Artist = Nguyễn Hùng
    if "maydays" in artist.lower() or "maydays" in clean_uploader.lower() or "maydays" in raw_title.lower():
        artist = "Nguyễn Hùng"
        if album == "Single":
            album = "MAYDAYs"
    
    if "nguyễn hùng" in query_hint.lower() or "nguyen hung" in query_hint.lower():
        if not artist or artist == "Various Artists" or artist.lower() in GENERIC_UPLOADERS:
            artist = "Nguyễn Hùng"

    if not title:
        title = cleaned or raw_title

    if album == "Single":
        album = f"{title} - Single"

    return {
        "title": title.strip(),
        "artist": artist.strip(),
        "album": album.strip(),
        "track_number": "1",
        "artwork_url": None,
        "source": "Bộ lọc Regex Heuristics (Fallback)"
    }

def convert_image_to_jpg(source_path: str, output_path: str) -> bool:
    """Chuyển đổi ảnh webp hoặc png sang jpeg bằng ffmpeg."""
    try:
        cmd = ["ffmpeg", "-y", "-i", source_path, "-q:v", "2", output_path]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        return res.returncode == 0 and os.path.exists(output_path)
    except Exception as e:
        print(f"[Tagger] Image conversion error: {e}", file=sys.stderr)
        return False

def fetch_and_embed_artwork(id3_tag: ID3, art_source: str):
    """Tải và nhúng ảnh bìa vào thẻ APIC ID3."""
    img_data = None
    mime = "image/jpeg"
    
    if art_source.startswith("http://") or art_source.startswith("https://"):
        try:
            req = urllib.request.Request(art_source, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=8) as resp:
                img_data = resp.read()
                if "png" in art_source.lower():
                    mime = "image/png"
        except Exception as e:
            print(f"[Tagger] Artwork download failed: {e}", file=sys.stderr)
    elif os.path.exists(art_source):
        # Nếu là file webp, convert sang jpg tạm
        tmp_jpg = None
        target_path = art_source
        if art_source.lower().endswith(".webp"):
            tmp_jpg = art_source + ".jpg"
            if convert_image_to_jpg(art_source, tmp_jpg):
                target_path = tmp_jpg
        try:
            with open(target_path, "rb") as f:
                img_data = f.read()
            if target_path.lower().endswith(".png"):
                mime = "image/png"
        except Exception as e:
            print(f"[Tagger] Local artwork read failed: {e}", file=sys.stderr)
        finally:
            if tmp_jpg and os.path.exists(tmp_jpg):
                try:
                    os.remove(tmp_jpg)
                except Exception:
                    pass

    if img_data:
        id3_tag.delall("APIC")
        id3_tag.add(
            APIC(
                encoding=3, # UTF-8
                mime=mime,
                type=3,     # Cover (front)
                desc='Cover',
                data=img_data
            )
        )

def write_id3_tags(file_path: str, meta: Dict[str, Any], local_thumbnail: Optional[str] = None):
    """Ghi thẻ ID3v2.3 tương thích với Apple Music, Navidrome & Jellyfin."""
    try:
        audio = ID3(file_path)
    except ID3NoHeaderError:
        audio = ID3()

    title = meta["title"]
    artist = meta["artist"]
    album = meta["album"]
    track = meta.get("track_number", "1")

    audio["TIT2"] = TIT2(encoding=3, text=title)
    audio["TPE1"] = TPE1(encoding=3, text=artist)
    audio["TPE2"] = TPE2(encoding=3, text=artist) # Album Artist
    audio["TALB"] = TALB(encoding=3, text=album)
    audio["TRCK"] = TRCK(encoding=3, text=track)

    art_to_embed = meta.get("artwork_url") or local_thumbnail
    if art_to_embed:
        fetch_and_embed_artwork(audio, art_to_embed)

    audio.save(file_path, v2_version=3)

def remove_empty_dirs(path: str):
    """Xóa các thư mục rỗng sau khi di chuyển file."""
    try:
        parent = os.path.dirname(path)
        while parent and parent != MUSIC_DIR and parent.startswith(MUSIC_DIR):
            if os.path.exists(parent) and not os.listdir(parent):
                os.rmdir(parent)
                parent = os.path.dirname(parent)
            else:
                break
    except Exception:
        pass

def sanitize_and_organize_audio(
    file_path: str,
    raw_title: str = "",
    uploader: str = "",
    query_hint: str = "",
    thumbnail_path: Optional[str] = None
) -> Tuple[str, Dict[str, Any]]:
    """
    Quy trình nhận diện Option 3 (AcoustID -> iTunes) với fallback Option 1 (Heuristics),
    ghi ID3 tags và di chuyển file vào cấu trúc chuẩn:
    /mnt/data/data/media/music/{Artist}/{Album}/{Title}.mp3
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File không tồn tại: {file_path}")

    # 1. Khởi tạo metadata sơ bộ từ heuristics để làm căn cứ tìm kiếm & đối soát
    heuristic_meta = fallback_heuristics(raw_title or Path(file_path).stem, uploader, query_hint)
    expected_title = heuristic_meta["title"]
    expected_artist = heuristic_meta["artist"]

    # 2. Thử Option 3A: AcoustID Fingerprint
    meta = query_acoustid(file_path)
    if meta and not meta.get("artwork_url"):
        # Thử tìm artwork độ phân giải cao trên iTunes nếu có
        art_lookup = query_itunes(f"{meta['title']} {meta['artist']}", expected_title=meta['title'], expected_artist=meta['artist'])
        if art_lookup and art_lookup.get("artwork_url"):
            meta["artwork_url"] = art_lookup["artwork_url"]

    # 3. Thử Option 3B: iTunes API Search (với xác thực tên bài & ca sĩ)
    if not meta:
        search_candidates = []
        if expected_title and expected_artist and expected_artist != "Various Artists":
            search_candidates.append(f"{expected_title} {expected_artist}")
        if expected_title:
            search_candidates.append(expected_title)
        if query_hint and query_hint not in search_candidates:
            search_candidates.append(query_hint)

        for q in search_candidates:
            itunes_meta = query_itunes(q, expected_title=expected_title, expected_artist=expected_artist)
            if itunes_meta:
                meta = itunes_meta
                break

    # 4. Fallback Option 1: Dùng Regex & Heuristics đã bóc tách
    if not meta:
        meta = heuristic_meta

    # Đảm bảo logic ưu tiên cho Nguyễn Hùng theo chỉ đạo của người dùng
    if query_hint and "nguyễn hùng" in query_hint.lower():
        if meta["artist"].lower() in ["maydays", "unknown", "various artists"] or not meta["artist"]:
            meta["artist"] = "Nguyễn Hùng"
            if meta["album"] == "Single":
                meta["album"] = "MAYDAYs"

    # Ghi ID3 tags
    write_id3_tags(file_path, meta, local_thumbnail=thumbnail_path)

    # Di chuyển file vào cấu trúc thư viện chuẩn
    clean_art = clean_filename_str(meta["artist"]) or "Various Artists"
    clean_alb = clean_filename_str(meta["album"]) or "Single"
    clean_tit = clean_filename_str(meta["title"]) or Path(file_path).stem

    dest_folder = os.path.join(MUSIC_DIR, clean_art, clean_alb)
    os.makedirs(dest_folder, exist_ok=True)

    dest_file = os.path.join(dest_folder, f"{clean_tit}.mp3")

    if os.path.abspath(file_path) != os.path.abspath(dest_file):
        shutil.move(file_path, dest_file)
        remove_empty_dirs(file_path)

    return dest_file, meta

def download_and_process_music(target: str, query_hint: str = "") -> Dict[str, Any]:
    """
    Hàm thực thi tải nhạc ngầm qua yt-dlp vào thư mục tạm .staging,
    sau đó chạy qua bộ lọc nhận diện Option 3 + Fallback Option 1,
    ghi tag và sắp xếp thư viện.
    """
    job_id = str(uuid.uuid4())[:8]
    job_staging = os.path.join(STAGING_DIR, job_id)
    os.makedirs(job_staging, exist_ok=True)

    # Chuẩn bị search target cho yt-dlp
    if target.startswith("http://") or target.startswith("https://"):
        search_target = target
    else:
        search_target = f"ytsearch1:{target}"

    cmd = [
        "yt-dlp",
        "--js-runtimes", "node",
        "-x", "--audio-format", "mp3", "--audio-quality", "0",
        "--write-thumbnail", "--write-info-json",
        "--no-playlist",
        "-o", f"{job_staging}/%(title)s.%(ext)s",
        search_target
    ]

    print(f"[Download] Bắt đầu tải: {target} (Job: {job_id})", flush=True)
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        err_msg = res.stderr[-500:] if res.stderr else "Lỗi tải yt-dlp."
        shutil.rmtree(job_staging, ignore_errors=True)
        raise RuntimeError(f"yt-dlp download failed: {err_msg}")

    # Tìm file mp3 đã tải
    mp3_files = glob.glob(os.path.join(job_staging, "*.mp3"))
    if not mp3_files:
        shutil.rmtree(job_staging, ignore_errors=True)
        raise RuntimeError("Không tìm thấy file MP3 sau khi yt-dlp hoàn tất.")

    downloaded_mp3 = mp3_files[0]

    # Đọc info.json nếu có
    raw_title = ""
    uploader = ""
    json_files = glob.glob(os.path.join(job_staging, "*.info.json"))
    if json_files:
        try:
            with open(json_files[0], "r", encoding="utf-8") as f:
                info_data = json.load(f)
                raw_title = info_data.get("title", "")
                uploader = info_data.get("uploader", "") or info_data.get("channel", "")
        except Exception:
            pass

    # Tìm file ảnh thumbnail
    thumbnail_path = None
    for ext in ["*.jpg", "*.jpeg", "*.png", "*.webp"]:
        thumb_candidates = glob.glob(os.path.join(job_staging, ext))
        if thumb_candidates:
            thumbnail_path = thumb_candidates[0]
            break

    # Tiến hành xử lý metadata và dọn dẹp
    final_path, meta = sanitize_and_organize_audio(
        file_path=downloaded_mp3,
        raw_title=raw_title,
        uploader=uploader,
        query_hint=query_hint or target,
        thumbnail_path=thumbnail_path
    )

    # Xóa sạch thư mục staging của job này
    shutil.rmtree(job_staging, ignore_errors=True)

    meta["file_path"] = final_path
    return meta

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 music_tagger.py --download <target> [--hint <hint>]")
        print("  python3 music_tagger.py --file <path> [--hint <hint>] [--title <title>]")
        sys.exit(1)

    mode = sys.argv[1]
    hint = ""
    title = ""
    
    for idx, arg in enumerate(sys.argv):
        if arg == "--hint" and idx + 1 < len(sys.argv):
            hint = sys.argv[idx + 1]
        elif arg == "--title" and idx + 1 < len(sys.argv):
            title = sys.argv[idx + 1]

    if mode == "--download" and len(sys.argv) > 2:
        target_query = sys.argv[2]
        res = download_and_process_music(target_query, query_hint=hint)
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif mode == "--file" and len(sys.argv) > 2:
        target_file = sys.argv[2]
        dest, res = sanitize_and_organize_audio(target_file, raw_title=title, query_hint=hint)
        print(json.dumps(res, ensure_ascii=False, indent=2))
