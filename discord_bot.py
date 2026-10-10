import os
import sys
import re
import json
import psutil
import asyncio
import subprocess
from datetime import datetime, timedelta
import discord
from discord.ext import tasks, commands
import google.generativeai as genai
from difflib import SequenceMatcher
from typing import Optional, Dict, Any, List, Tuple

REPO_DIR = "/home/dongkhoi/mac-server"
CHANNEL_CACHE_FILE = "/home/dongkhoi/.last_channel_id"
SECRETS_FILE = "/home/dongkhoi/.secrets"
MEDIA_DOWNLOAD_DIR = "/home/dongkhoi/Downloads/Media"
MUSIC_DOWNLOAD_DIR = "/mnt/data/data/media/music"
JELLYFIN_URL = "http://127.0.0.1:8096"
JELLYFIN_API_KEY = "e0d6cdb1f3c04c84bb23bc77c07bf736"
ALLOWED_USER_ID = 932202011812823070

os.environ["PATH"] = f"/home/dongkhoi/.local/bin:{os.environ.get('PATH', '')}"

os.makedirs(MEDIA_DOWNLOAD_DIR, exist_ok=True)
os.makedirs(MUSIC_DOWNLOAD_DIR, exist_ok=True)

DISCORD_TOKEN = ""
GEMINI_API_KEY = ""

if os.path.exists(SECRETS_FILE):
    with open(SECRETS_FILE, "r") as f:
        for line in f:
            if "=" in line:
                k, v = line.strip().split("=", 1)
                if k == "DISCORD_TOKEN":
                    DISCORD_TOKEN = v
                elif k == "GEMINI_API_KEY":
                    GEMINI_API_KEY = v

MODELS_TO_TRY = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite"
]

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="", intents=intents)

last_alert_time = None
is_alert_active = False

active_chat_session = None
current_model_used = None



async def run_async_download_and_notify(cmd: str, target_name: str, channel_id: int, log_file: str, is_music: bool = True):
    try:
        proc = await asyncio.create_subprocess_shell(
            f"{cmd} > {log_file} 2>&1"
        )
        await proc.communicate()

        channel = None
        if channel_id:
            channel = bot.get_channel(channel_id)
            if not channel:
                try:
                    channel = await bot.fetch_channel(channel_id)
                except Exception:
                    channel = None

        if proc.returncode == 0:
            if is_music:
                try:
                    refresh_jellyfin_library()
                except Exception:
                    pass

                # Trích xuất metadata JSON từ log_file nếu do music_tagger tạo ra
                meta_info = None
                spot_info = None
                if os.path.exists(log_file):
                    try:
                        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                            content = f.read()
                            json_match = re.search(r'\{[\s\S]*"title"[\s\S]*\}', content)
                            if json_match:
                                meta_info = json.loads(json_match.group(0))

                            album_match = re.search(r'Found (\d+) songs in (.+?) \((Album|Playlist|Artist)\)', content)
                            if album_match:
                                d_count = len(re.findall(r'Downloaded "[^"]+"', content))
                                spot_info = {
                                    "total": album_match.group(1),
                                    "name": album_match.group(2),
                                    "type": album_match.group(3),
                                    "count": d_count
                                }
                    except Exception:
                        pass

                if channel:
                    if spot_info:
                        type_name = "ALBUM" if spot_info["type"].lower() == "album" else ("NGHỆ SĨ" if spot_info["type"].lower() == "artist" else "PLAYLIST")
                        await channel.send(
                            f"✅ **ĐÃ TẢI XONG TRỌN BỘ {type_name}:** `{spot_info['name']}`\n"
                            f"💿 **Số lượng bài:** `{spot_info['count']}/{spot_info['total']} bài`\n"
                            f"🎧 Đã tự động cập nhật vào **Jellyfin & Navidrome**.\n"
                            f"Sếp mở **Ampsonic** trên iPhone là có thể thưởng thức trọn vẹn rồi ạ! 🎵"
                        )
                    elif meta_info and meta_info.get("title") and meta_info.get("artist"):
                        src_desc = meta_info.get("source", "Tự động nhận diện")
                        await channel.send(
                            f"✅ **ĐÃ TẢI XONG:** `{meta_info['title']}`\n"
                            f"👤 **Ca sĩ:** `{meta_info['artist']}`\n"
                            f"💿 **Album:** `{meta_info.get('album', 'Single')}`\n"
                            f"🔍 **Phương thức nhận diện:** `{src_desc}`\n"
                            f"🎧 Đã tự động cập nhật vào **Jellyfin & Navidrome**.\n"
                            f"Sếp mở **Ampsonic** trên iPhone là có thể thưởng thức ngay rồi ạ! 🎵"
                        )
                    else:
                        await channel.send(
                            f"✅ **ĐÃ TẢI XONG:** `{target_name}`\n"
                            f"🎧 Đã tự động cập nhật vào **Jellyfin & Navidrome**.\n"
                            f"Sếp mở **Ampsonic** trên iPhone là có thể thưởng thức ngay rồi ạ! 🎵"
                        )
            else:
                if channel:
                    await channel.send(
                        f"✅ **ĐÃ TẢI XONG VIDEO:** `{target_name}`\n"
                        f"📁 Đã lưu vào thư mục Media trên máy chủ."
                    )
        else:
            err_summary = "Lỗi tiến trình tải."
            if os.path.exists(log_file):
                try:
                    with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                        lines = [line.strip() for line in f.readlines() if line.strip()]
                        if lines:
                            err_summary = "\n".join(lines[-4:])
                except Exception:
                    pass
            if channel:
                await channel.send(
                    f"⚠️ **TẢI THẤT BÀI:** `{target_name}`\n"
                    f"```text\n{err_summary[:400]}\n```"
                )
    except Exception as e:
        print(f"Lỗi task tải ngầm: {e}", flush=True)

def dispatch_download_task(cmd: str, target_name: str, log_file: str, is_music: bool = True):
    ch_id = get_last_channel_id()
    try:
        if bot.loop and bot.loop.is_running():
            asyncio.run_coroutine_threadsafe(
                run_async_download_and_notify(cmd, target_name, ch_id, log_file, is_music=is_music),
                bot.loop
            )
            return True
    except Exception as e:
        print(f"Lỗi dispatch task: {e}", flush=True)
    return False

SYSTEM_INSTRUCTION = (
    "Bạn là trợ lý điều hành máy chủ MacServerControl tên là JARVIS. "
    "Bạn duy trì ngữ cảnh trò chuyện xuyên suốt nhiều lượt với sếp. "
    "Bạn sở hữu các công cụ tra cứu phần cứng, kiểm tra tiến trình, quản trị dịch vụ Docker, "
    "tìm kiếm nhạc, tải nhạc/album/ca sĩ vào thư viện Jellyfin & Navidrome, làm mới thư viện và thực thi lệnh an toàn. "
    "Khi sếp muốn tải cả Album (ví dụ: 'tải album 99% của MCK', 'tải album Gieo', 'tải album Ng\'bth của Ngọt'): "
    "  - Nếu sếp gửi link Spotify (hoặc bạn biết chính xác link Spotify album/playlist đó), hãy ưu tiên gọi download_music bằng chính liên kết Spotify đó (ví dụ https://open.spotify.com/album/...) để đảm bảo tải đúng 100% không bị lỗi tìm kiếm. "
    "  - Nếu dùng cú pháp 'album: <tên album>', bạn chỉ được điền tên album chính xác trên Spotify và loại bỏ tên ca sĩ (ví dụ 'album: Gieo' thay vì 'album: Gieo Ngọt', hoặc 'album: Ng\'bthg' cho album Ng'bth). "
    "Khi sếp muốn tải tất cả bài của ca sĩ (ví dụ: 'tải nhạc ca sĩ Vũ', 'tải bài hát của Sơn Tùng'), hãy gọi download_music với link Spotify artist nếu có hoặc 'artist: <tên ca sĩ chuẩn>'. "
    "Khi sếp gửi liên kết Spotify (bài hát, album, playlist, artist), hãy lập tức gọi công cụ download_music với link đó. "
    "Khi sếp gửi tên bài hát đơn lẻ, hãy gọi download_music với tên bài hát. "
    "Khi sếp muốn tìm bài hát hoặc xem danh sách các bản nhạc/remix trước khi tải, hãy gọi search_music. "
    "Khi sếp bảo quét lại Jellyfin, hãy gọi refresh_jellyfin_library. "
    "Khi sếp gửi liên kết video media, hãy gọi download_media_video. "
    "Khi sếp gửi liên kết magnet torrent, hãy gọi add_magnet_link. "
    "Khi sếp hỏi thăm tiến độ tải, hãy gọi check_download_progress. "
    "Tuyệt đối không tự ghép lệnh terminal phức tạp khi đã có công cụ sẵn."
)

def get_system_stats() -> str:
    """Tra cứu các chỉ số phần cứng hiện tại gồm CPU, RAM, dung lượng tất cả các ổ đĩa và thời gian hoạt động."""
    cpu = psutil.cpu_percent(interval=1)
    ram = psutil.virtual_memory()
    boot_time = datetime.fromtimestamp(psutil.boot_time()).strftime("%Y-%m-%d %H:%M:%S")

    disk_lines = []
    seen_mounts = set()
    for part in psutil.disk_partitions(all=False):
        if part.mountpoint in seen_mounts or "loop" in part.device:
            continue
        seen_mounts.add(part.mountpoint)
        try:
            usage = psutil.disk_usage(part.mountpoint)
            total_gb = usage.total // (1024**3)
            free_gb = usage.free // (1024**3)
            disk_lines.append(f"  + Điểm gắn {part.mountpoint} trên thiết bị {part.device}: Trống {free_gb} GB trên tổng {total_gb} GB, đã dùng {usage.percent}%")
        except PermissionError:
            continue

    disk_info = "\n".join(disk_lines) if disk_lines else "Không đọc được thông tin ổ đĩa."

    return (
        f"CPU: {cpu}%\n"
        f"RAM: {ram.percent}% - Dùng {ram.used // (1024**2)} MB trên tổng {ram.total // (1024**2)} MB\n"
        f"Dung lượng các phân vùng ổ đĩa:\n{disk_info}\n"
        f"Thời gian hoạt động từ: {boot_time}"
    )

def get_top_processes(sort_by: str = "memory", limit: int = 10) -> str:
    """Tra cứu danh sách các tiến trình chiếm dụng nhiều tài nguyên nhất."""
    key = "cpu_percent" if sort_by.lower() == "cpu" else "memory_percent"
    procs = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        try:
            procs.append(p.info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    procs.sort(key=lambda x: x[key] or 0, reverse=True)
    
    header = f"{'PID':<7}{'TIẾN TRÌNH':<16}{'RAM':<7}{'CPU':<6}\n"
    divider = "-" * 36 + "\n"
    lines = [header, divider]
    for p in procs[:limit]:
        name = p['name'][:14] if p['name'] else "unknown"
        mem = f"{p['memory_percent']:.1f}%" if p['memory_percent'] is not None else "0.0%"
        cpu = f"{p['cpu_percent']:.1f}%" if p['cpu_percent'] is not None else "0.0%"
        lines.append(f"{p['pid']:<7}{name:<16}{mem:<7}{cpu:<6}\n")
    return "".join(lines)

def list_docker_containers(all_containers: bool = True) -> str:
    """Liệt kê danh sách các container Docker kèm trạng thái đang chạy hoặc đã dừng."""
    flag = "-a" if all_containers else ""
    cmd = f"docker ps {flag} --format 'table {{{{.Names}}}}\t{{{{.Status}}}}\t{{{{.Ports}}}}'"
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        out = res.stdout.strip()
        return out if out else "Không tìm thấy container Docker nào."
    except Exception as e:
        return f"Lỗi kiểm tra Docker: {e}"

def get_docker_logs(container_name: str, lines: int = 30) -> str:
    """Trích xuất số dòng log mới nhất của một container Docker cụ thể để chẩn đoán lỗi."""
    safe_name = "".join(c for c in container_name if c.isalnum() or c in "-_")
    if not safe_name:
        return "Tên container không hợp lệ."
    cmd = f"docker logs --tail {int(lines)} {safe_name}"
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        out = res.stdout if res.stdout else res.stderr
        return out[-1500:] if out else f"Không có dữ liệu log cho container {safe_name}."
    except Exception as e:
        return f"Lỗi lấy log: {e}"

def restart_docker_container(container_name: str) -> str:
    """Khởi động lại một container Docker cụ thể theo tên dịch vụ."""
    safe_name = "".join(c for c in container_name if c.isalnum() or c in "-_")
    if not safe_name:
        return "Tên container không hợp lệ."
    cmd = f"docker restart {safe_name}"
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=25)
        if res.returncode == 0:
            return f"Đã khởi động lại thành công container {safe_name}."
        return f"Khởi động lại thất bại: {res.stderr.strip()}"
    except Exception as e:
        return f"Lỗi khi thực thi khởi động container: {e}"

def download_media_video(url: str, subfolder: str = "") -> str:
    """Tải video chất lượng cao từ liên kết mạng bằng yt-dlp vào thư mục lưu trữ media máy chủ."""
    target_dir = os.path.join(MEDIA_DOWNLOAD_DIR, subfolder.strip()) if subfolder else MEDIA_DOWNLOAD_DIR
    os.makedirs(target_dir, exist_ok=True)
    
    cmd = (
        f"yt-dlp --js-runtimes node -f 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best' "
        f"--no-playlist -o '{target_dir}/%(title)s.%(ext)s' '{url}'"
    )
    dispatch_download_task(cmd, url, "/tmp/ytdlp_last.log", is_music=False)
    return f"Đã bắt đầu tải video ngầm từ '{url}' vào thư mục {target_dir}. Tôi sẽ tự động thông báo vào đây ngay khi video tải xong."

def add_magnet_link(magnet: str) -> str:
    """Nạp liên kết magnet torrent vào trình tải trên máy chủ."""
    if not magnet.startswith("magnet:?"):
        return "Liên kết không đúng định dạng magnet link hợp lệ."
    
    cmd = f"transmission-remote -a '{magnet}' || qbittorrent-nox '{magnet}'"
    subprocess.Popen(cmd, shell=True)
    return "Đã tiếp nhận và đưa liên kết magnet vào hàng đợi tải xuống của máy chủ."

def run_terminal_command(command: str) -> str:
    """Thực thi các lệnh Linux tra cứu an toàn trên hệ thống."""
    danger_keywords = ["rm -rf", "mkfs", "dd ", "shutdown", "reboot", "> /etc", "> /boot"]
    if any(k in command for k in danger_keywords):
        return "Lệnh bị chặn do nguy cơ gây hại hệ thống."
    try:
        res = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=15,
            cwd="/home/dongkhoi"
        )
        out = res.stdout if res.stdout else res.stderr
        return out[:1500] if out else "Lệnh hoàn tất không có output."
    except Exception as e:
        return f"Lỗi chạy lệnh: {e}"

def refresh_jellyfin_library() -> str:
    """Kích hoạt quét và làm mới toàn bộ thư viện phim ảnh, âm nhạc trên Jellyfin máy chủ."""
    try:
        import urllib.request
        req = urllib.request.Request(
            f"{JELLYFIN_URL}/Library/Refresh",
            data=b"",
            headers={"X-Emby-Token": JELLYFIN_API_KEY},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status in (200, 204):
                return "✅ Đã kích hoạt quét làm mới thư viện Jellyfin thành công. Media mới sẽ xuất hiện ngay trên giao diện."
            return f"Phản hồi từ Jellyfin: mã HTTP {resp.status}"
    except Exception as e:
        return f"Lỗi khi gửi yêu cầu quét Jellyfin: {e}"

def get_spotify_client():
    try:
        from spotdl.utils.spotify import SpotifyClient
        if not SpotifyClient.get_instance():
            SpotifyClient.init(
                client_id="5f573c9620494bae87890c0f08a60293",
                client_secret="212476d9b0f3472eaa762d90b19b0ba8"
            )
        return SpotifyClient()
    except Exception as e:
        print(f"Lỗi khởi tạo SpotifyClient: {e}", flush=True)
    return None

def find_album_url_by_search(query: str) -> Optional[str]:
    """Tự động tra cứu link Spotify Album chính xác từ tên album text để khắc phục triệt để lỗi SongListError của spotdl."""
    client = get_spotify_client()
    if not client:
        return None

    clean_q = re.sub(r'^(album|tải album):\s*', '', query.strip(), flags=re.IGNORECASE)
    clean_q = re.sub(r'\s+(của|by)\s+.+$', '', clean_q, flags=re.IGNORECASE).strip()

    candidates = [clean_q]
    if query != clean_q and query not in candidates:
        candidates.append(query)

    found_albums = {}
    for cq in candidates:
        try:
            res = client.search(cq)
            tracks = res.get("tracks", {}).get("items", []) if res else []
            for t in tracks:
                alb = t.get("album")
                if alb and alb.get("id"):
                    alb_id = alb["id"]
                    alb_name = alb.get("name", "")
                    alb_artists = [a.get("name", "") for a in alb.get("artists", [])]
                    if alb_id not in found_albums:
                        found_albums[alb_id] = {
                            "name": alb_name,
                            "artists": alb_artists,
                            "url": f"https://open.spotify.com/album/{alb_id}"
                        }
        except Exception:
            pass

    if not found_albums:
        return None

    target_lower = clean_q.lower()
    best_id = None
    best_score = -1.0

    for alb_id, data in found_albums.items():
        name_lower = data["name"].lower()
        score = SequenceMatcher(None, target_lower, name_lower).ratio()

        if name_lower == target_lower:
            score += 1.0
        elif target_lower in name_lower:
            score += 0.5

        for art in data["artists"]:
            if art.lower() in query.lower():
                score += 0.4

        if score > best_score:
            best_score = score
            best_id = alb_id

    if best_id and best_score >= 0.4:
        return found_albums[best_id]["url"]

    return None

def find_artist_url_by_search(query: str) -> Optional[str]:
    """Tự động tra cứu link Spotify Artist chính xác từ tên ca sĩ text."""
    client = get_spotify_client()
    if not client:
        return None

    clean_q = re.sub(r'^(artist|ca sĩ|nghệ sĩ):\s*', '', query.strip(), flags=re.IGNORECASE)
    try:
        res = client.search(clean_q)
        tracks = res.get("tracks", {}).get("items", []) if res else []
        found_artists = {}
        for t in tracks:
            for a in t.get("artists", []):
                a_id = a.get("id")
                a_name = a.get("name")
                if a_id and a_name not in found_artists:
                    found_artists[a_name] = a_id

        best_id = None
        best_score = -1.0
        target_lower = clean_q.lower()
        for name, a_id in found_artists.items():
            score = SequenceMatcher(None, target_lower, name.lower()).ratio()
            if name.lower() == target_lower:
                score += 1.0
            if score > best_score:
                best_score = score
                best_id = a_id

        if best_id and best_score >= 0.5:
            return f"https://open.spotify.com/artist/{best_id}"
    except Exception:
        pass
    return None

def download_music(query_or_url: str) -> str:
    """Tải bài hát, album, ca sĩ hoặc playlist về máy chủ và tự động đồng bộ vào Jellyfin & Navidrome.
    Tham số có thể là:
    - Liên kết Spotify: bài hát (/track/), album (/album/), ca sĩ (/artist/), playlist (/playlist/).
    - Cú pháp tìm kiếm album hoặc ca sĩ: 'album: tên album' hoặc 'artist: tên ca sĩ'.
    - Tên bài hát đơn lẻ hoặc liên kết YouTube."""
    target = query_or_url.strip()
    if not target:
        return "Vui lòng cung cấp tên bài hát, album hoặc liên kết hợp lệ."

    # Danh mục ánh xạ trực tiếp các album/nghệ sĩ phổ biến để tránh lỗi tìm kiếm không chính xác
    KNOWN_ALBUM_URLS = {
        "ái": "https://open.spotify.com/album/5zyztYlsHoAeQKCsu8rVbo",
        "ai": "https://open.spotify.com/album/5zyztYlsHoAeQKCsu8rVbo",
        "ng'bth": "https://open.spotify.com/album/4BwnV3YdCtxn8ydFVrstbI",
        "ng'bthg": "https://open.spotify.com/album/4BwnV3YdCtxn8ydFVrstbI",
        "nguoi binh thuong": "https://open.spotify.com/album/4BwnV3YdCtxn8ydFVrstbI",
        "người bình thường": "https://open.spotify.com/album/4BwnV3YdCtxn8ydFVrstbI",
        "gieo": "https://open.spotify.com/album/1ZnJrvDY8ih3ppPWR2Tc2a",
        "gieo ngọt": "https://open.spotify.com/album/1ZnJrvDY8ih3ppPWR2Tc2a",
        "gieo ngot": "https://open.spotify.com/album/1ZnJrvDY8ih3ppPWR2Tc2a",
        "99%": "https://open.spotify.com/album/1vi1WySkgPGkbR8NnQzlXu",
        "99% mck": "https://open.spotify.com/album/1vi1WySkgPGkbR8NnQzlXu",
        "3": "https://open.spotify.com/album/1vWpL5qW4Jt3L64gQ0xMkJ",
        "3 ngọt": "https://open.spotify.com/album/1vWpL5qW4Jt3L64gQ0xMkJ",
        "3 ngot": "https://open.spotify.com/album/1vWpL5qW4Jt3L64gQ0xMkJ",
    }

    clean_target = target
    lower_target = target.lower()

    # 1. Kiểm tra ánh xạ nhanh
    for alias, s_url in KNOWN_ALBUM_URLS.items():
        if alias in lower_target:
            if "album" in lower_target or not target.startswith("http"):
                clean_target = s_url
                lower_target = s_url.lower()
                break

    # 2. Tự động tìm Spotify URL cho Album nếu là tìm kiếm text (giải quyết triệt để SongListError)
    if "open.spotify.com" not in clean_target and any(lower_target.startswith(p) for p in ["album:", "tải album"]):
        resolved_album = find_album_url_by_search(clean_target)
        if resolved_album:
            clean_target = resolved_album
            lower_target = resolved_album.lower()

    # 3. Tự động tìm Spotify URL cho Artist nếu là tìm kiếm text
    if "open.spotify.com" not in clean_target and any(lower_target.startswith(p) for p in ["artist:", "ca sĩ:", "nghệ sĩ:"]):
        resolved_artist = find_artist_url_by_search(clean_target)
        if resolved_artist:
            clean_target = resolved_artist
            lower_target = resolved_artist.lower()

    output_tpl = f"{MUSIC_DOWNLOAD_DIR}/{{artist}}/{{album}}/{{track-number}} - {{title}}.{{output-ext}}"
    is_spotify = "open.spotify.com" in clean_target or any(lower_target.startswith(p) for p in ["album:", "artist:", "playlist:"])

    if is_spotify:
        final_spotify_target = clean_target.split("?")[0].strip() if "open.spotify.com" in clean_target else clean_target
        # Thoát dấu nháy để đảm bảo an toàn cho shell command, thêm --dont-filter-results để tránh lọc mất interlude/visualizer
        safe_spotify_target = final_spotify_target.replace('"', '\\"')
        cmd = f"spotdl --output \"{output_tpl}\" --dont-filter-results \"{safe_spotify_target}\""
        dispatch_download_task(cmd, final_spotify_target, "/tmp/music_download_last.log", is_music=True)
        if "album" in final_spotify_target:
            return f"Đã tìm thấy Album và bắt đầu tải trọn bộ từ Spotify ('{final_spotify_target}') về kho nhạc máy chủ. Tôi sẽ tự động thông báo vào đây ngay khi tải xong và đồng bộ vào Jellyfin & Navidrome."
        elif "artist" in final_spotify_target:
            return f"Đã tìm thấy Nghệ sĩ và bắt đầu tải trọn bộ bài hát từ Spotify ('{final_spotify_target}') về kho nhạc máy chủ. Tôi sẽ tự động thông báo vào đây ngay khi hoàn tất."
        elif "playlist" in final_spotify_target:
            return f"Đã bắt đầu tải toàn bộ Playlist '{final_spotify_target}' về kho nhạc máy chủ. Tôi sẽ tự động thông báo vào đây ngay khi hoàn tất."
        return f"Đã bắt đầu tải bài hát từ Spotify vào kho nhạc. Tôi sẽ tự động thông báo vào đây ngay khi hoàn tất."

    # Nếu là link YouTube/web hoặc từ khóa bài hát đơn lẻ
    safe_target = target.replace('"', '\\"')
    cmd = f"python3 {REPO_DIR}/music_tagger.py --download \"{safe_target}\" --hint \"{safe_target}\""
    dispatch_download_task(cmd, target, "/tmp/music_download_last.log", is_music=True)
    return f"Đã bắt đầu tải bài hát '{target}'. Hệ thống sẽ tự động nhận diện metadata chuẩn bằng vân tay âm thanh (AcoustID / iTunes) với cơ chế fallback bộ lọc regex, gắn ảnh bìa và sắp xếp vào đúng thư viện của Jellyfin & Navidrome."

def download_spotify_track(url: str) -> str:
    """Tải bài hát hoặc album từ Spotify (tương thích ngược)."""
    return download_music(url)

def search_music(query: str, limit: int = 5) -> str:
    """Tìm kiếm danh sách bài hát trên mạng theo tên bài hát hoặc ca sĩ (trả về danh sách tên, ca sĩ, thời lượng, URL)."""
    clean_query = query.strip()
    if not clean_query:
        return "Vui lòng nhập từ khóa tìm kiếm."
    max_results = min(max(int(limit), 1), 8)
    cmd = (
        f"yt-dlp --js-runtimes node --flat-playlist --print '%(title)s | %(uploader)s | %(duration_string)s | %(url)s' "
        f"'ytsearch{max_results}:{clean_query}'"
    )
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
        lines = [line.strip() for line in res.stdout.strip().splitlines() if line.strip()]
        if not lines:
            return f"Không tìm thấy bài hát nào phù hợp với từ khóa '{clean_query}'."
        
        results = [f"🔍 **Kết quả tìm kiếm cho '{clean_query}':**"]
        for idx, line in enumerate(lines, 1):
            parts = line.split(" | ")
            if len(parts) >= 4:
                title, uploader, duration, url = parts[0], parts[1], parts[2], parts[3]
                results.append(f"{idx}. **{title}**\n   - Kênh: {uploader} | Thời lượng: {duration}\n   - Link: <{url}>")
            else:
                results.append(f"{idx}. {line}")
        return "\n".join(results)
    except Exception as e:
        return f"Lỗi tìm kiếm: {e}"

def check_download_progress() -> str:
    """Kiểm tra tiến độ tải nhạc hoặc video media gần nhất trên máy chủ."""
    logs_to_check = [
        ("Tiến trình Tải Nhạc", "/tmp/music_download_last.log"),
        ("Tiến trình Spotify", "/tmp/spotdl_last.log"),
        ("Tiến trình Media Video", "/tmp/ytdlp_last.log")
    ]
    reports = []
    for label, log_path in logs_to_check:
        if os.path.exists(log_path):
            try:
                with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                    lines = [line.strip() for line in f.readlines() if line.strip()]
                    if lines:
                        last_lines = "\n".join(lines[-4:])
                        reports.append(f"📌 **{label}:**\n```text\n{last_lines}\n```")
            except Exception:
                pass
    if not reports:
        return "Hiện chưa có tiến trình tải nào ghi nhận nhật ký gần đây."
    return "\n\n".join(reports)

TOOLS_LIST = [
    get_system_stats,
    get_top_processes,
    list_docker_containers,
    get_docker_logs,
    restart_docker_container,
    refresh_jellyfin_library,
    download_media_video,
    add_magnet_link,
    download_music,
    download_spotify_track,
    search_music,
    check_download_progress,
    run_terminal_command
]


def save_last_channel(channel_id):
    try:
        with open(CHANNEL_CACHE_FILE, "w") as f:
            f.write(str(channel_id))
    except Exception:
        pass

def get_last_channel_id():
    if os.path.exists(CHANNEL_CACHE_FILE):
        try:
            with open(CHANNEL_CACHE_FILE, "r") as f:
                return int(f.read().strip())
        except Exception:
            return None
    return None

async def send_chunked(destination, text: str, is_code: bool = False):
    max_len = 1800
    if not text:
        return
    for i in range(0, len(text), max_len):
        chunk = text[i:i+max_len]
        if is_code:
            await destination.send(f"```bash\n{chunk}\n```")
        else:
            await destination.send(chunk)

async def ask_jarvis_agent(prompt: str) -> str:
    global active_chat_session, current_model_used
    errors = []

    if active_chat_session is None:
        for model_name in MODELS_TO_TRY:
            try:
                model = genai.GenerativeModel(
                    model_name=model_name,
                    tools=TOOLS_LIST,
                    system_instruction=SYSTEM_INSTRUCTION
                )
                active_chat_session = model.start_chat(enable_automatic_function_calling=True)
                current_model_used = model_name
                break
            except Exception as e:
                errors.append(f"{model_name}: {e}")
                continue

    if active_chat_session is None:
        return "⚠️ Không thể kết nối mô hình AI:\n" + "\n".join(errors[:2])

    try:
        def _send():
            resp = active_chat_session.send_message(prompt)
            return resp.text

        return await asyncio.to_thread(_send)
    except Exception as err:
        active_chat_session = None
        return f"⚠️ Trục trặc phiên làm việc: {err}. Phiên đối thoại đã được đặt lại."

@bot.event
async def on_ready():
    print(f"Bot đã online: {bot.user}")
    if not monitor_health.is_running():
        monitor_health.start()

@bot.event
async def on_message(message):
    global active_chat_session
    if message.author == bot.user:
        return

    if message.author.id != ALLOWED_USER_ID:
        return

    save_last_channel(message.channel.id)
    raw_content = message.content.strip()
    content = raw_content.lower()

    if content in ["ping", "!ping"]:
        await message.reply("Pong! Hệ thống MacServerControl sẵn sàng phục vụ.")
        return

    if content in ["reset", "làm mới", "quên hết đi", "new chat"]:
        active_chat_session = None
        await message.reply("🧠 Đã xóa sạch bộ nhớ đệm hội thoại. Bắt đầu phiên làm việc mới.")
        return

    if content in ["restart", "!restart", "reboot bot"]:
        await message.reply("🔄 Đang khởi động lại dịch vụ bot qua systemd...")
        subprocess.Popen(
            "nohup bash -c 'sleep 2 && sudo systemctl restart discord-bot' > /dev/null 2>&1 &",
            shell=True
        )
        return

    if content in ["cập nhật bot", "cap nhat bot", "update bot", "git pull"]:
        status_msg = await message.reply("🔄 Đang kéo mã nguồn mới từ kho Git...")
        proc_pull = await asyncio.create_subprocess_shell(
            "git pull origin main",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=REPO_DIR
        )
        stdout, stderr = await proc_pull.communicate()
        out = stdout.decode().strip()

        if "Already up to date" in out:
            await status_msg.edit(content="✅ Bot đã ở phiên bản mới nhất trên Git.")
            return

        proc_check = await asyncio.create_subprocess_shell(
            f"python3 -m py_compile {REPO_DIR}/discord_bot.py",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        _, err_check = await proc_check.communicate()

        if proc_check.returncode != 0:
            await asyncio.create_subprocess_shell("git reset --hard HEAD@{1}", cwd=REPO_DIR)
            err_msg = err_check.decode().strip()[:400]
            await status_msg.edit(content=f"⚠️ Bản cập nhật lỗi cú pháp. Đã tự rollback về bản cũ.\n```{err_msg}```")
            return

        await status_msg.edit(content=f"✅ Cập nhật thành công.\n```{out[:250]}```\nKhởi động lại sau 2 giây...")
        subprocess.Popen("nohup bash -c 'sleep 2 && sudo systemctl restart discord-bot' > /dev/null 2>&1 &", shell=True)
        return

    if raw_content.startswith("$") or raw_content.startswith("sh "):
        cmd = raw_content[1:].strip() if raw_content.startswith("$") else raw_content[3:].strip()
        status_msg = await message.reply(f"⚡ Đang thực thi: `{cmd[:50]}`...")
        try:
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd="/home/dongkhoi"
            )
            stdout, stderr = await process.communicate()
            out = stdout.decode().strip()
            err = stderr.decode().strip()
            result = out if out else (err if err else "Lệnh hoàn thành không có output.")

            if len(result) <= 1700:
                await status_msg.edit(content=f"**Kết quả tại /home/dongkhoi:**\n```bash\n{result}\n```")
            else:
                await status_msg.edit(content="**Kết quả xuất theo khối:**")
                await send_chunked(message.channel, result, is_code=True)
        except Exception as e:
            await status_msg.edit(content=f"❌ Lỗi thực thi: `{e}`")
        return

    async with message.channel.typing():
        try:
            reply_text = await ask_jarvis_agent(raw_content)
            await send_chunked(message.channel, reply_text, is_code=False)
        except Exception as e:
            await message.reply(f"❌ Lỗi xử lý: {e}")

@tasks.loop(minutes=2)
async def monitor_health():
    global last_alert_time, is_alert_active
    cpu = psutil.cpu_percent()
    ram = psutil.virtual_memory().percent

    if cpu > 90 or ram > 90:
        now = datetime.now()
        if not is_alert_active or (last_alert_time and now - last_alert_time > timedelta(minutes=30)):
            is_alert_active = True
            last_alert_time = now
            alert_msg = f"🚨 **CẢNH BÁO QUÁ TẢI TRÊN MACSERVER:**\n- CPU: {cpu}%\n- RAM: {ram}%"
            channel_id = get_last_channel_id()
            if channel_id:
                channel = bot.get_channel(channel_id)
                if channel:
                    try:
                        await channel.send(alert_msg)
                    except Exception:
                        pass
    else:
        is_alert_active = False

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("Chưa cấu hình DISCORD_TOKEN trong /home/dongkhoi/.secrets")
        sys.exit(1)
    bot.run(DISCORD_TOKEN)
