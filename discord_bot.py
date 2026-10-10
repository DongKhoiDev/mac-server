import os
import sys
import psutil
import asyncio
import subprocess
from datetime import datetime, timedelta
import discord
from discord.ext import tasks, commands
import google.generativeai as genai

REPO_DIR = "/home/dongkhoi/mac-server"
CHANNEL_CACHE_FILE = "/home/dongkhoi/.last_channel_id"
SECRETS_FILE = "/home/dongkhoi/.secrets"
ALLOWED_USER_ID = 932202011812823070

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
    "gemini-2.5-flash",
    "gemini-2.0-flash",
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

# Biến lưu trữ phiên hội thoại dài hạn
active_chat_session = None
current_model_used = None

SYSTEM_INSTRUCTION = (
    "Bạn là trợ lý điều hành máy chủ MacServerControl tên là JARVIS. "
    "Bạn có trí nhớ ngữ cảnh để theo dõi mạch hội thoại với chủ nhân. "
    "Bạn sở hữu các công cụ tra cứu phần cứng, kiểm tra tiến trình và thực thi lệnh shell Linux. "
    "Khi người dùng yêu cầu kiểm tra máy chủ, xem dung lượng các ổ SSD và HDD, kiểm tra mạng, hoặc hỏi về các dịch vụ như Jellyfin, Immich, "
    "hãy chủ động gọi công cụ phù hợp để lấy thông tin thật từ hệ thống rồi trả lời ngắn gọn, rành mạch."
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
            disk_lines.append(f"  + {part.mountpoint} - thiết bị {part.device}: Trống {free_gb} GB trên tổng {total_gb} GB, đã dùng {usage.percent}%")
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
    """Tra cứu danh sách các tiến trình chiếm dụng nhiều tài nguyên nhất. sort_by có thể là memory hoặc cpu."""
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

def run_terminal_command(command: str) -> str:
    """Thực thi các lệnh Linux tra cứu an toàn như docker ps, kiểm tra log dịch vụ hoặc đo kiểm mạng."""
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

TOOLS_LIST = [get_system_stats, get_top_processes, run_terminal_command]

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

    # Tạo phiên nếu chưa khởi tạo
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

    # Gửi tin nhắn tiếp nối vào mạch hội thoại hiện tại
    try:
        def _send():
            resp = active_chat_session.send_message(prompt)
            return resp.text

        return await asyncio.to_thread(_send)
    except Exception as err:
        # Nếu lỗi ngữ cảnh quá dài hoặc phiên bị ngắt, tự phục hồi bằng phiên mới
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

    # Lệnh làm mới trí nhớ ngữ cảnh
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