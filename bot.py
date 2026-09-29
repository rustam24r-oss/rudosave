"""
Pro Telegram video bot
- Instagram / YouTube havola -> video yuklab beradi (yt-dlp)
- Video yuborilsa -> dumaloq video yoki 4K
- Vaqtinchalik fayllar avtomatik tozalanadi (30 daqiqadan keyin)
"""
import asyncio
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from yt_dlp import YoutubeDL


# --- .env (lokal ishga tushirish uchun; Render'da Environment ishlatiladi) ---
def load_env(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
BOT_TOKEN = os.environ.get("BOT_TOKEN") or "8985454094:AAEVsG8TRVSfZU6uT2Zqoj3b0ytsD3wtl9Q"

COOKIES = os.environ.get("COOKIES_FILE", "/etc/secrets/cookies.txt")
BASE_DIR = Path(tempfile.gettempdir()) / "videobot"
BASE_DIR.mkdir(parents=True, exist_ok=True)
TTL = 30 * 60                   # fayllar saqlanish muddati (soniya)
MAX_UPLOAD = 49 * 1024 * 1024   # oddiy Bot API limiti ~50MB

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
sem = asyncio.Semaphore(2)      # bir vaqtda 2 ta og'ir ish
last_file: dict[int, str] = {}

URL_RE = re.compile(
    r"(https?://(?:www\.|m\.)?(?:instagram\.com|youtube\.com|youtu\.be)/\S+)", re.I
)


def new_dir() -> str:
    return tempfile.mkdtemp(dir=BASE_DIR)


async def cleaner() -> None:
    """Eski papkalarni o'chiradi va last_file ni tozalaydi."""
    while True:
        await asyncio.sleep(300)
        now = time.time()
        for d in BASE_DIR.iterdir():
            try:
                if d.is_dir() and now - d.stat().st_mtime > TTL:
                    shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
        for uid, path in list(last_file.items()):
            if not Path(path).exists():
                last_file.pop(uid, None)


async def run(*cmd: str) -> tuple[int, str]:
    p = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await p.communicate()
    return p.returncode, out.decode(errors="ignore")


def download(url: str, out_dir: str) -> str:
    opts = {
        "outtmpl": f"{out_dir}/%(id)s.%(ext)s",
        "format": "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "retries": 5,
    }
    if Path(COOKIES).exists():
        opts["cookiefile"] = COOKIES
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info).rsplit(".", 1)[0] + ".mp4"


async def to_circle(src: str, dst: str) -> None:
    code, log = await run(
        "ffmpeg", "-y", "-i", src, "-t", "20",
        "-vf", "crop='min(iw,ih)':'min(iw,ih)',scale=640:640",
        "-c:v", "libx264", "-preset", "fast", "-crf", "26",
        "-maxrate", "3M", "-bufsize", "6M",
        "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", dst,
    )
    if code:
        raise RuntimeError(log[-500:])


async def to_4k(src: str, dst: str) -> None:
    code, log = await run(
        "ffmpeg", "-y", "-i", src,
        "-vf", "scale=3840:2160:force_original_aspect_ratio=decrease:flags=lanczos,"
               "pad=3840:2160:(ow-iw)/2:(oh-ih)/2,unsharp=5:5:0.8",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "copy", "-movflags", "+faststart", dst,
    )
    if code:
        raise RuntimeError(log[-500:])


def kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⭕ Dumaloq", callback_data="circle"),
        InlineKeyboardButton(text="🖥 4K", callback_data="4k"),
    ]])


@dp.message(CommandStart())
async def start(m: types.Message):
    await m.answer(
        "Salom! 👋\n"
        "• Instagram yoki YouTube havolasini yuboring — videoni yuklab beraman.\n"
        "• Video yuboring — dumaloq video yoki 4K qilib beraman."
    )


@dp.message(F.text.regexp(URL_RE))
async def on_link(m: types.Message):
    url = URL_RE.search(m.text).group(1)
    wait = await m.answer("⏳ Yuklanmoqda...")
    d = new_dir()
    async with sem:
        try:
            path = await asyncio.to_thread(download, url, d)
            if os.path.getsize(path) > MAX_UPLOAD:
                await wait.edit_text("❌ Video 50MB dan katta, Telegram yuborishga ruxsat bermaydi.")
                shutil.rmtree(d, ignore_errors=True)
                return
            last_file[m.from_user.id] = path
            await m.answer_video(FSInputFile(path), caption="✅ Tayyor", reply_markup=kb())
            await wait.delete()
        except Exception as e:
            shutil.rmtree(d, ignore_errors=True)
            await wait.edit_text(f"❌ Yuklab bo'lmadi: {str(e)[:200]}")


@dp.message(F.video)
async def on_video(m: types.Message):
    d = new_dir()
    path = f"{d}/in.mp4"
    try:
        await bot.download(m.video, destination=path)
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        await m.answer("❌ Videoni qabul qilib bo'lmadi (20MB dan katta bo'lishi mumkin).")
        return
    last_file[m.from_user.id] = path
    await m.answer("Nima qilamiz?", reply_markup=kb())


@dp.callback_query(F.data.in_({"circle", "4k"}))
async def on_action(c: types.CallbackQuery):
    src = last_file.get(c.from_user.id)
    if not src or not Path(src).exists():
        await c.answer("Fayl eskirgan, videoni qayta yuboring", show_alert=True)
        return
    await c.answer("Ishlanmoqda...")
    dst = str(Path(src).with_name(f"out_{c.data}.mp4"))
    async with sem:
        try:
            if c.data == "circle":
                await to_circle(src, dst)
                await c.message.answer_video_note(FSInputFile(dst))
            else:
                await to_4k(src, dst)
                if os.path.getsize(dst) > MAX_UPLOAD:
                    await c.message.answer("⚠️ 4K fayl 50MB dan katta bo'ldi, Telegramga yuborib bo'lmaydi.")
                else:
                    await c.message.answer_document(FSInputFile(dst), caption="🖥 4K tayyor")
        except Exception as e:
            await c.message.answer(f"❌ Xatolik: {str(e)[:200]}")
        finally:
            Path(dst).unlink(missing_ok=True)


async def main():
    asyncio.create_task(cleaner())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
