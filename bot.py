import os
import re
import sys
import ast
import json
import shutil
import asyncio
import logging
import zipfile
import functools
import importlib.util
from datetime import datetime, timedelta

import psutil
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.client.default import DefaultBotProperties

# ----------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------
# These are used directly. If a BOT_TOKEN / ADMIN_ID Environment Variable is
# also set on Railway from an older setup, it is ignored — whatever is
# written here always wins, so editing this file is always enough.
DEFAULT_BOT_TOKEN = "8788775336:AAG1EN9l8bDR6km6M5fYfi7auNuDEkvLoYE"
DEFAULT_ADMIN_ID = 7764565509

BOT_TOKEN = (DEFAULT_BOT_TOKEN or os.getenv("BOT_TOKEN", "")).strip()
try:
    ADMIN_ID = int(str(DEFAULT_ADMIN_ID) or os.getenv("ADMIN_ID", "0"))
except ValueError:
    ADMIN_ID = 0

DB_FILE = "running_db.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("runner")

if not BOT_TOKEN:
    logger.critical(
        "BOT_TOKEN is not set. Add it in Railway's Environment Variables as "
        "BOT_TOKEN and redeploy the service."
    )
    sys.exit(1)

if not ADMIN_ID:
    logger.critical(
        "ADMIN_ID is not set or invalid. Add it in Railway's Environment Variables as "
        "ADMIN_ID (your numeric Telegram user ID)."
    )
    sys.exit(1)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="Markdown"))
dp = Dispatcher(storage=MemoryStorage())

RUNNING_PROCESSES = {}

# ----------------------------------------------------
# DATABASE HELPERS
# ----------------------------------------------------
def save_db():
    try:
        data = {}
        for pid, info in RUNNING_PROCESSES.items():
            data[str(pid)] = {
                "file_name": info["file_name"],
                "work_dir": info["work_dir"],
                "user_id": info["user_id"],
                "end_time": info["end_time"].strftime("%Y-%m-%d %H:%M:%S"),
            }
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception:
        logger.exception("save_db failed")


def cleanup_stale_state():
    """On (re)start, any in-memory process handles from a previous run are gone.
    Reset the on-disk db and wipe leftover work directories so old, dead
    entries never confuse the bot or fill up the disk."""
    try:
        if os.path.exists(DB_FILE):
            os.remove(DB_FILE)
    except Exception:
        logger.exception("could not remove stale db file")

    try:
        for entry in os.listdir(os.getcwd()):
            if entry.startswith("run_") and os.path.isdir(entry):
                shutil.rmtree(entry, ignore_errors=True)
    except Exception:
        logger.exception("could not clean stale run_ directories")


# ----------------------------------------------------
# STATES
# ----------------------------------------------------
class UploadState(StatesGroup):
    waiting_for_file = State()
    waiting_for_entry_choice = State()
    waiting_for_duration_unit = State()
    waiting_for_duration_value = State()


class ProjectManageState(StatesGroup):
    waiting_for_project_selection = State()
    waiting_for_action = State()
    waiting_for_extend_time = State()


# ----------------------------------------------------
# KEYBOARDS
# ----------------------------------------------------
def kbtn(text, style=None):
    """Colored keyboard button (Bot API 9.4 'style' field, needs aiogram>=3.20).
    Falls back to a plain button automatically if the installed aiogram/Bot API
    combo doesn't support styling, so this can never raise an error."""
    if style:
        try:
            return KeyboardButton(text=text, style=style)
        except Exception:
            logger.debug("Colored button style unsupported, falling back to plain button")
    return KeyboardButton(text=text)


def get_main_keyboard():
    buttons = [
        [kbtn("📤 Upload & Run Project", "primary")],
        [kbtn("📁 My Projects", "primary"), kbtn("🖥 Server Status", "primary")],
        [kbtn("📖 Tutorial", "success")],
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def get_cancel_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[kbtn("❌ Cancel", "danger")]],
        resize_keyboard=True,
    )


def get_time_unit_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [kbtn("⏱ Hours", "primary"), kbtn("📅 Days", "success")],
            [kbtn("❌ Cancel", "danger")],
        ],
        resize_keyboard=True,
    )


# Admin Access Middleware
@dp.message.outer_middleware()
async def admin_only_middleware(handler, event: types.Message, data):
    try:
        if event.from_user.id != ADMIN_ID:
            await event.answer("⛔ Access Denied! This bot is private.")
            return
        return await handler(event, data)
    except Exception:
        logger.exception("middleware error")
        try:
            await event.answer("⚠️ An unexpected error occurred but was handled. Please try again.")
        except Exception:
            pass


def safe_handler(func):
    """Wrap every handler so that no exception can ever crash the bot or
    leave the user without a response."""

    @functools.wraps(func)
    async def wrapper(message: types.Message, *args, **kwargs):
        try:
            return await func(message, *args, **kwargs)
        except Exception as e:
            logger.exception("handler '%s' failed", func.__name__)
            try:
                await message.answer(
                    f"⚠️ An unexpected error occurred, but the bot did not crash:\n`{str(e)[:300]}`",
                    reply_markup=get_main_keyboard(),
                )
            except Exception:
                pass

    return wrapper


@dp.error()
async def global_error_handler(event: types.ErrorEvent):
    logger.exception("Unhandled error while processing update", exc_info=event.exception)
    try:
        await bot.send_message(
            ADMIN_ID,
            f"⚠️ Handled internal error:\n`{str(event.exception)[:500]}`",
        )
    except Exception:
        pass
    return True


# ----------------------------------------------------
# COMMANDS & HANDLERS
# ----------------------------------------------------
@dp.message(CommandStart())
@safe_handler
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 **Welcome Boss!**\n\nYour code runner bot is online and ready.",
        reply_markup=get_main_keyboard(),
    )


@dp.message(F.text == "❌ Cancel")
@safe_handler
async def cancel_action(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Operation canceled.", reply_markup=get_main_keyboard())


@dp.message(F.text == "📖 Tutorial")
@safe_handler
async def show_tutorial(message: types.Message):
    tutorial_text = (
        "📚 **Code Runner Tutorial**\n\n"
        "• **Supported Languages:** Python (`.py`), Node.js (`.js`), TypeScript (`.ts`), "
        "Go (`.go`), Java (`.java`), PHP (`.php`), Rust (`.rs`)\n"
        "• **ZIP Packages:** Upload `.zip` files for multi-file projects — the bot "
        "automatically finds the right entry file.\n"
        "• **Auto Libraries:** Missing Python/Node packages are detected from your "
        "source code and installed automatically, even without a `requirements.txt` "
        "or `package.json`.\n"
        "• **Auto Runtimes:** Python is always ready to go. For Node/Go/Java/PHP/Rust, "
        "the bot installs the required runtime the first time it's needed — this can "
        "add a short delay on first use of a new language."
    )
    await message.answer(tutorial_text)


@dp.message(F.text == "🖥 Server Status")
@safe_handler
async def server_status(message: types.Message):
    cpu = psutil.cpu_percent(interval=1)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage("/")

    status_msg = (
        "🖥 **Detailed Server Status**\n\n"
        f"💻 **CPU Usage:** `{cpu}%`\n"
        f"🧠 **RAM Used:** `{ram.used // (1024**2)} MB` / `{ram.total // (1024**2)} MB` (`{ram.percent}%`)\n"
        f"🧠 **RAM Free:** `{ram.available // (1024**2)} MB`\n"
        f"💾 **Disk Free:** `{disk.free // (1024**3)} GB`\n"
        f"⚙️ **Active Projects:** `{len(RUNNING_PROCESSES)}`"
    )
    await message.answer(status_msg)


# ----------------------------------------------------
# PROJECT / DEPENDENCY ANALYSIS HELPERS
# ----------------------------------------------------
IMPORT_TO_PIP = {
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "yaml": "PyYAML",
    "bs4": "beautifulsoup4",
    "dotenv": "python-dotenv",
    "Crypto": "pycryptodome",
    "Cryptodome": "pycryptodome",
    "sklearn": "scikit-learn",
    "telebot": "pyTelegramBotAPI",
    "telegram": "python-telegram-bot",
    "jwt": "PyJWT",
    "MySQLdb": "mysqlclient",
    "psycopg2": "psycopg2-binary",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "fitz": "PyMuPDF",
    "socks": "PySocks",
    "OpenSSL": "pyOpenSSL",
    "serial": "pyserial",
    "win32api": "pywin32",
    "Xlib": "python-xlib",
    "telethon": "Telethon",
    "pyrogram": "pyrogram",
    "tgcrypto": "TgCrypto",
}

try:
    STDLIB_MODULES = set(sys.stdlib_module_names)
except AttributeError:
    STDLIB_MODULES = set(sys.builtin_module_names)

IGNORED_DIR_NAMES = {"__pycache__", "__MACOSX", ".git", "node_modules", "venv", ".venv"}

# entry-file candidates and recognized extensions per project type
ENTRY_CONFIG = {
    "python": (["main.py", "bot.py", "run.py", "app.py", "start.py", "runner.py"], (".py",)),
    "node": (
        ["index.js", "main.js", "app.js", "server.js", "bot.js",
         "index.ts", "main.ts", "app.ts", "server.ts", "bot.ts"],
        (".js", ".ts"),
    ),
    "go": (["main.go", "bot.go", "app.go"], (".go",)),
    "java": (["Main.java", "App.java", "Bot.java", "Runner.java"], (".java",)),
    "php": (["index.php", "main.php", "app.php", "bot.php"], (".php",)),
    "rust": (["main.rs", "bot.rs"], (".rs",)),
}


def flatten_single_subdir(work_dir):
    """If the zip extracted into a single wrapping folder, move everything up
    one level so entry-point detection works regardless of how it was zipped."""
    try:
        entries = [e for e in os.listdir(work_dir) if e not in IGNORED_DIR_NAMES]
        if len(entries) == 1:
            inner = os.path.join(work_dir, entries[0])
            if os.path.isdir(inner):
                for item in os.listdir(inner):
                    shutil.move(os.path.join(inner, item), os.path.join(work_dir, item))
                shutil.rmtree(inner, ignore_errors=True)
    except Exception:
        logger.exception("flatten_single_subdir failed")


def walk_files(work_dir, suffixes):
    if isinstance(suffixes, str):
        suffixes = (suffixes,)
    matches = []
    for root, dirs, files in os.walk(work_dir):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIR_NAMES]
        for f in files:
            if f.endswith(suffixes):
                matches.append(os.path.join(root, f))
    return matches


def find_entry_file(work_dir, candidates, suffixes):
    for c in candidates:
        p = os.path.join(work_dir, c)
        if os.path.isfile(p):
            return p
    for root, dirs, files in os.walk(work_dir):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIR_NAMES]
        for c in candidates:
            if c in files:
                return os.path.join(root, c)
    matches = walk_files(work_dir, suffixes)
    if len(matches) == 1:
        return matches[0]
    if matches:
        matches.sort(key=lambda p: p.count(os.sep))
        return matches
    return None


def detect_project_type(work_dir):
    if os.path.exists(os.path.join(work_dir, "requirements.txt")) or walk_files(work_dir, ".py"):
        return "python"
    if os.path.exists(os.path.join(work_dir, "package.json")) or walk_files(work_dir, ".js") or walk_files(work_dir, ".ts"):
        return "node"
    if walk_files(work_dir, ".go"):
        return "go"
    if walk_files(work_dir, ".java"):
        return "java"
    if walk_files(work_dir, ".php"):
        return "php"
    if walk_files(work_dir, ".rs"):
        return "rust"
    return None


def extract_py_imports(path):
    modules = set()
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            source = f.read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for n in node.names:
                    modules.add(n.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.level == 0:
                    modules.add(node.module.split(".")[0])
    except Exception:
        logger.debug("could not parse %s for imports", path, exc_info=True)
    return modules


def find_missing_python_packages(work_dir):
    """Scan every .py file for imports and figure out, purely from source
    analysis, which third-party packages need to be pip-installed —
    independent of whether requirements.txt even exists or is complete."""
    local_names = set()
    for root, dirs, files in os.walk(work_dir):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIR_NAMES]
        for f in files:
            if f.endswith(".py"):
                local_names.add(os.path.splitext(f)[0])
        for d in dirs:
            local_names.add(d)

    all_modules = set()
    for path in walk_files(work_dir, ".py"):
        all_modules |= extract_py_imports(path)

    to_install = []
    for mod in all_modules:
        if not mod or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", mod):
            continue
        if mod in STDLIB_MODULES or mod in local_names:
            continue
        try:
            if importlib.util.find_spec(mod) is not None:
                continue
        except Exception:
            pass
        to_install.append(IMPORT_TO_PIP.get(mod, mod))

    return sorted(set(to_install))


def find_missing_node_packages(work_dir):
    declared = set()
    pkg_json = os.path.join(work_dir, "package.json")
    if os.path.exists(pkg_json):
        try:
            with open(pkg_json, "r", encoding="utf-8", errors="ignore") as f:
                data = json.load(f)
            declared |= set(data.get("dependencies", {}).keys())
            declared |= set(data.get("devDependencies", {}).keys())
        except Exception:
            logger.debug("could not parse package.json", exc_info=True)

    required = set()
    require_re = re.compile(r"require\(\s*['\"]([^./][^'\"]*)['\"]\s*\)")
    import_re = re.compile(r"from\s+['\"]([^./][^'\"]*)['\"]")
    for path in walk_files(work_dir, ".js") + walk_files(work_dir, ".ts"):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            for match in require_re.findall(content) + import_re.findall(content):
                pkg = match.split("/")[0] if not match.startswith("@") else "/".join(match.split("/")[:2])
                required.add(pkg)
        except Exception:
            continue

    return sorted(required - declared)


async def run_and_log(cmd, cwd):
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        return proc.returncode, (out or b"").decode(errors="ignore")
    except Exception as e:
        return -1, str(e)


# Railway's default builder only provisions Python (since that's the only
# language it detects from this project at build time). Any other language
# runtime has to be installed inside the running container on demand.
SYSTEM_RUNTIME_REQUIREMENTS = {
    "node": ("node", ["nodejs", "npm"]),
    "go": ("go", ["golang-go"]),
    "java": ("java", ["default-jdk"]),
    "php": ("php", ["php-cli"]),
    "rust": ("rustc", ["rustc"]),
}


async def ensure_system_runtime(project_type, status_message: types.Message):
    """Make sure the interpreter/compiler needed for project_type exists,
    installing it automatically via apt if it's missing. Never raises —
    on failure it reports clearly and lets the caller decide what to do,
    instead of crashing the bot."""
    if project_type not in SYSTEM_RUNTIME_REQUIREMENTS:
        return True  # python needs nothing extra

    check_cmd, apt_packages = SYSTEM_RUNTIME_REQUIREMENTS[project_type]
    if shutil.which(check_cmd):
        return True

    await status_message.answer(
        f"⚙️ The {project_type} runtime isn't installed in this container yet — "
        f"attempting to install it automatically..."
    )

    for prefix in ([], ["sudo"]):
        code, out = await run_and_log([*prefix, "apt-get", "update"], "/")
        if code == 0:
            code, out = await run_and_log([*prefix, "apt-get", "install", "-y", *apt_packages], "/")
            if code == 0 and shutil.which(check_cmd):
                await status_message.answer(f"✅ {project_type} runtime installed successfully.")
                return True

    await status_message.answer(
        f"❌ Couldn't install the {project_type} runtime automatically in this container "
        f"(it may not allow package installation). Python projects are unaffected and will "
        f"still run normally."
    )
    return False


async def install_dependencies(work_dir, project_type, status_message: types.Message):
    """Best-effort dependency installation. Never raises — failures are
    logged and reported, but execution still proceeds afterwards."""
    notes = []

    if project_type == "python":
        req_txt = os.path.join(work_dir, "requirements.txt")
        if os.path.exists(req_txt):
            code, out = await run_and_log(
                [sys.executable, "-m", "pip", "install", "--no-input",
                 "--disable-pip-version-check", "-r", "requirements.txt"],
                work_dir,
            )
            if code != 0:
                notes.append(f"⚠️ Installing requirements.txt failed:\n`{out[-300:]}`")

        missing = find_missing_python_packages(work_dir)
        if missing:
            await status_message.answer(
                "🔎 Detected Python packages used in the source but not installed:\n"
                f"`{', '.join(missing)}`\nInstalling automatically..."
            )
            code, out = await run_and_log(
                [sys.executable, "-m", "pip", "install", "--no-input",
                 "--disable-pip-version-check", *missing],
                work_dir,
            )
            if code != 0:
                notes.append(f"⚠️ Installing some detected packages failed:\n`{out[-300:]}`")

    elif project_type == "node":
        pkg_json = os.path.join(work_dir, "package.json")
        if os.path.exists(pkg_json):
            code, out = await run_and_log(["npm", "install"], work_dir)
            if code != 0:
                notes.append(f"⚠️ npm install failed:\n`{out[-300:]}`")

        missing = find_missing_node_packages(work_dir)
        if missing:
            await status_message.answer(
                "🔎 Detected Node.js packages used in the source but not installed:\n"
                f"`{', '.join(missing)}`\nInstalling automatically..."
            )
            code, out = await run_and_log(["npm", "install", *missing], work_dir)
            if code != 0:
                notes.append(f"⚠️ Installing some detected Node.js packages failed:\n`{out[-300:]}`")

    for note in notes:
        try:
            await status_message.answer(note)
        except Exception:
            pass


def build_run_command(entry_path, project_type):
    run_dir = os.path.dirname(entry_path)
    name = os.path.basename(entry_path)

    if project_type == "python":
        cmd = [sys.executable, name]

    elif project_type == "node":
        cmd = (["npx", "ts-node", name] if name.endswith(".ts") else ["node", name])

    elif project_type == "go":
        # List every .go file in the directory as an ad-hoc package so this
        # works for both single-file scripts and multi-file projects, with
        # or without a go.mod.
        go_files = sorted(f for f in os.listdir(run_dir) if f.endswith(".go"))
        cmd = ["go", "run", *go_files] if go_files else ["go", "run", name]

    elif project_type == "php":
        cmd = ["php", name]

    elif project_type == "java":
        # Compile every .java file in the directory together, then run the
        # class matching the chosen entry file (handles multi-file projects
        # without a package declaration).
        class_name = os.path.splitext(name)[0]
        cmd = ["sh", "-c", f"javac *.java && java {class_name}"]

    elif project_type == "rust":
        # rustc follows `mod` declarations from the entry file automatically,
        # so this also covers multi-file rust projects.
        cmd = ["sh", "-c", f"rustc {name} -o app && ./app"]

    else:
        cmd = None

    return cmd, run_dir


# ----------------------------------------------------
# UPLOAD & EXECUTION
# ----------------------------------------------------
@dp.message(F.text == "📤 Upload & Run Project")
@safe_handler
async def start_upload(message: types.Message, state: FSMContext):
    await state.set_state(UploadState.waiting_for_file)
    await message.answer("📥 Send your project file or ZIP archive:", reply_markup=get_cancel_keyboard())


@dp.message(UploadState.waiting_for_file, F.document)
@safe_handler
async def handle_file(message: types.Message, state: FSMContext):
    doc = message.document
    file_name = doc.file_name
    ext = os.path.splitext(file_name)[1].lower()
    allowed_exts = [".py", ".js", ".ts", ".go", ".java", ".php", ".rs", ".zip"]

    if ext not in allowed_exts:
        await message.answer("❌ Unsupported file extension!")
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    work_dir = os.path.join(os.getcwd(), f"run_{message.from_user.id}_{timestamp}")
    os.makedirs(work_dir, exist_ok=True)

    file_path = os.path.join(work_dir, file_name)
    try:
        await bot.download(doc, destination=file_path)
    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        await message.answer(f"❌ File download failed: `{str(e)[:200]}`", reply_markup=get_main_keyboard())
        await state.clear()
        return

    entry_path = None
    project_type = None

    if ext == ".zip":
        try:
            with zipfile.ZipFile(file_path, "r") as zip_ref:
                zip_ref.extractall(work_dir)
            os.remove(file_path)
        except Exception as e:
            shutil.rmtree(work_dir, ignore_errors=True)
            await message.answer(f"❌ Zip extraction failed: `{str(e)}`")
            await state.clear()
            return

        flatten_single_subdir(work_dir)
        project_type = detect_project_type(work_dir)

        if project_type is None:
            shutil.rmtree(work_dir, ignore_errors=True)
            await message.answer("❌ Could not detect the project type inside the ZIP.", reply_markup=get_main_keyboard())
            await state.clear()
            return

        candidates, suffixes = ENTRY_CONFIG[project_type]
        found = find_entry_file(work_dir, candidates, suffixes)

        if isinstance(found, list):
            # Multiple files, no clear single entry point — let the user choose.
            rel_choices = [os.path.relpath(p, work_dir) for p in found[:20]]
            await state.update_data(
                work_dir=work_dir, project_type=project_type, entry_choices=rel_choices
            )
            await state.set_state(UploadState.waiting_for_entry_choice)
            buttons = [[kbtn(c, "primary")] for c in rel_choices]
            buttons.append([kbtn("❌ Cancel", "danger")])
            await message.answer(
                "📂 Multiple runnable files were found in the ZIP. Which one should I run?",
                reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True),
            )
            return
        entry_path = found

        if not entry_path:
            shutil.rmtree(work_dir, ignore_errors=True)
            await message.answer("❌ No runnable file was found inside the ZIP.", reply_markup=get_main_keyboard())
            await state.clear()
            return
    else:
        entry_path = file_path
        project_type = {
            ".py": "python", ".js": "node", ".ts": "node",
            ".go": "go", ".php": "php", ".java": "java", ".rs": "rust",
        }[ext]

    await state.update_data(
        work_dir=work_dir, project_type=project_type,
        entry_path=entry_path, file_name=os.path.basename(entry_path),
    )
    await state.set_state(UploadState.waiting_for_duration_unit)
    await message.answer("⏰ Select runtime unit:", reply_markup=get_time_unit_keyboard())


@dp.message(UploadState.waiting_for_entry_choice)
@safe_handler
async def entry_choice_selected(message: types.Message, state: FSMContext):
    data = await state.get_data()
    choices = data.get("entry_choices", [])
    if message.text not in choices:
        await message.answer("❌ Please choose one of the options shown.")
        return

    work_dir = data["work_dir"]
    entry_path = os.path.join(work_dir, message.text)
    await state.update_data(entry_path=entry_path, file_name=os.path.basename(entry_path))
    await state.set_state(UploadState.waiting_for_duration_unit)
    await message.answer("⏰ Select runtime unit:", reply_markup=get_time_unit_keyboard())


@dp.message(UploadState.waiting_for_duration_unit, F.text.in_(["⏱ Hours", "📅 Days"]))
@safe_handler
async def select_duration_unit(message: types.Message, state: FSMContext):
    unit = "hours" if "Hours" in message.text else "days"
    await state.update_data(time_unit=unit)
    await state.set_state(UploadState.waiting_for_duration_value)
    await message.answer(f"⌛ Enter runtime duration in **{unit.capitalize()}**:", reply_markup=get_cancel_keyboard())


@dp.message(UploadState.waiting_for_duration_value)
@safe_handler
async def run_project_final(message: types.Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        await message.answer("❌ Send a valid positive integer.")
        return

    duration_val = int(message.text)
    data = await state.get_data()
    work_dir = data["work_dir"]
    project_type = data["project_type"]
    entry_path = data["entry_path"]
    file_name = data["file_name"]
    unit = data["time_unit"]

    duration_seconds = duration_val * 3600 if unit == "hours" else duration_val * 86400
    end_time = datetime.now() + timedelta(seconds=duration_seconds)

    status_message = await message.answer("⚙️ Installing packages and preparing runtime...")

    runtime_ok = await ensure_system_runtime(project_type, status_message)
    if not runtime_ok:
        shutil.rmtree(work_dir, ignore_errors=True)
        await message.answer("❌ Could not run this project type in the current environment.", reply_markup=get_main_keyboard())
        await state.clear()
        return

    await install_dependencies(work_dir, project_type, status_message)

    cmd, run_dir = build_run_command(entry_path, project_type)
    if not cmd:
        shutil.rmtree(work_dir, ignore_errors=True)
        await message.answer("❌ This project type is not supported.", reply_markup=get_main_keyboard())
        await state.clear()
        return

    try:
        process = await asyncio.create_subprocess_exec(*cmd, cwd=run_dir)
        pid = process.pid

        task = asyncio.create_task(auto_stop_project(pid, duration_seconds, file_name, work_dir))

        RUNNING_PROCESSES[pid] = {
            "process": process,
            "file_name": file_name,
            "work_dir": work_dir,
            "user_id": message.from_user.id,
            "end_time": end_time,
            "task": task,
        }
        save_db()

        await message.answer(
            f"🚀 **Project is now Running!**\n\n"
            f"📄 **Name:** `{file_name}`\n"
            f"🆔 **PID:** `{pid}`\n"
            f"📅 **Expires At:** `{end_time.strftime('%Y-%m-%d %H:%M:%S')}`",
            reply_markup=get_main_keyboard(),
        )
    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        await message.answer(f"❌ Execution Error: `{str(e)[:300]}`", reply_markup=get_main_keyboard())

    await state.clear()


async def auto_stop_project(pid, delay, file_name, work_dir):
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    if pid in RUNNING_PROCESSES:
        p_info = RUNNING_PROCESSES[pid]
        try:
            p_info["process"].terminate()
            await p_info["process"].wait()
        except Exception:
            pass

        shutil.rmtree(work_dir, ignore_errors=True)
        del RUNNING_PROCESSES[pid]
        save_db()

        try:
            await bot.send_message(
                ADMIN_ID, f"⏰ **Time Expired!** Project `{file_name}` (PID: `{pid}`) was automatically stopped."
            )
        except Exception:
            pass


# ----------------------------------------------------
# PROJECT MANAGEMENT
# ----------------------------------------------------
@dp.message(F.text == "📁 My Projects")
@safe_handler
async def my_projects(message: types.Message, state: FSMContext):
    if not RUNNING_PROCESSES:
        await message.answer("ℹ️ No active projects currently running.")
        return

    text = "📁 **Active Projects:**\n\n"
    buttons = []
    for pid, info in RUNNING_PROCESSES.items():
        rem = info["end_time"] - datetime.now()
        mins = max(0, int(rem.total_seconds() // 60))
        text += f"🔹 **PID:** `{pid}` | `{info['file_name']}` | Remaining: `{mins}m`\n"
        buttons.append([kbtn(f"Manage PID {pid}", "primary")])

    buttons.append([kbtn("❌ Cancel", "danger")])
    await state.set_state(ProjectManageState.waiting_for_project_selection)
    await message.answer(text, reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True))


@dp.message(ProjectManageState.waiting_for_project_selection, F.text.startswith("Manage PID "))
@safe_handler
async def select_project(message: types.Message, state: FSMContext):
    try:
        pid = int(message.text.replace("Manage PID ", ""))
    except ValueError:
        await message.answer("❌ Invalid ID.")
        return

    if pid not in RUNNING_PROCESSES:
        await message.answer("❌ Project not found.")
        await state.clear()
        return

    await state.update_data(selected_pid=pid)
    await state.set_state(ProjectManageState.waiting_for_action)

    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [kbtn("🛑 Stop & Delete", "danger")],
            [kbtn("➕ Extend Time", "success")],
            [kbtn("❌ Cancel", "primary")],
        ],
        resize_keyboard=True,
    )
    await message.answer(f"⚙️ Action for PID `{pid}`:", reply_markup=keyboard)


@dp.message(ProjectManageState.waiting_for_action, F.text == "🛑 Stop & Delete")
@safe_handler
async def stop_project_action(message: types.Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("selected_pid")

    if pid in RUNNING_PROCESSES:
        p_info = RUNNING_PROCESSES[pid]
        p_info["task"].cancel()
        try:
            p_info["process"].terminate()
        except Exception:
            pass
        shutil.rmtree(p_info["work_dir"], ignore_errors=True)
        del RUNNING_PROCESSES[pid]
        save_db()
        await message.answer(f"✅ PID `{pid}` stopped.", reply_markup=get_main_keyboard())

    await state.clear()


@dp.message(ProjectManageState.waiting_for_action, F.text == "➕ Extend Time")
@safe_handler
async def extend_time_prompt(message: types.Message, state: FSMContext):
    await state.set_state(ProjectManageState.waiting_for_extend_time)
    await message.answer("⌛ Enter extra hours to add:", reply_markup=get_cancel_keyboard())


@dp.message(ProjectManageState.waiting_for_extend_time)
@safe_handler
async def extend_time_action(message: types.Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        await message.answer("❌ Enter a valid number.")
        return

    extra_hours = int(message.text)
    data = await state.get_data()
    pid = data.get("selected_pid")

    if pid in RUNNING_PROCESSES:
        p_info = RUNNING_PROCESSES[pid]
        p_info["end_time"] += timedelta(hours=extra_hours)
        p_info["task"].cancel()
        rem_seconds = (p_info["end_time"] - datetime.now()).total_seconds()
        p_info["task"] = asyncio.create_task(
            auto_stop_project(pid, rem_seconds, p_info["file_name"], p_info["work_dir"])
        )
        save_db()
        await message.answer(
            f"✅ Extended! Expiry: `{p_info['end_time'].strftime('%Y-%m-%d %H:%M:%S')}`",
            reply_markup=get_main_keyboard(),
        )

    await state.clear()


# ----------------------------------------------------
# FALLBACK (must stay last — only catches messages no other handler matched)
# ----------------------------------------------------
@dp.message()
@safe_handler
async def fallback_handler(message: types.Message, state: FSMContext):
    await message.answer(
        "🤔 I didn't recognize that. Tap /start or use the menu buttons below.",
        reply_markup=get_main_keyboard(),
    )


# ----------------------------------------------------
# MAIN EXECUTION
# ----------------------------------------------------
async def main():
    cleanup_stale_state()
    logger.info("Runner bot starting...")

    try:
        me = await bot.get_me()
        logger.info("Authenticated as @%s (id=%s)", me.username, me.id)
    except Exception:
        logger.critical(
            "Could not authenticate with Telegram using the configured BOT_TOKEN. "
            "Double-check that the token in the source file is correct and was not "
            "regenerated/revoked in BotFather."
        )
        return

    try:
        # A webhook left over from an earlier setup silently blocks getUpdates
        # (polling) with no visible error — always clear it before polling.
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        logger.exception("delete_webhook failed (continuing anyway)")

    while True:
        try:
            await dp.start_polling(bot)
            break
        except Exception:
            logger.exception("Polling crashed — restarting in 5 seconds")
            await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
