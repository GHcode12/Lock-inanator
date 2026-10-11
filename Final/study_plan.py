"""study_plan.py - Gemini study plan + cute to-do list for Study Buddy.

What it does
  1. Looks for  syllabus.pdf  (next to the game, in ./assets, or the folder you run from).
  2. Uploads it to Gemini and asks for a step-by-step study guide, a to-do list
     and the hours you should spend on each chapter.
  3. Shows the result as a pastel to-do list inside the game (press L).

Setup
  pip install google-genai
  Put your key in the GEMINI_API_KEY environment variable, or in a text file called
  gemini_api_key.txt next to this file. (Never share or upload your key!)

The plan is saved to study_plan.json (together with your checkmarks), so Gemini is only
asked again when syllabus.pdf changes. Delete study_plan.json to force a fresh plan.
"""
import json
import math
import os
import re
import threading
import time
from pathlib import Path

import pygame

# =====================================================================
# SETTINGS (edit these!)
# =====================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SYLLABUS_FILE = "syllabus.pdf"
PLAN_FILE = os.path.join(BASE_DIR, "study_plan.json")   # saved plan + checkmarks
API_KEY_FILE = "gemini_api_key.txt"                     # optional alternative to the env var
GEMINI_MODEL = "gemini-3.8-flash"

# Anything you want Gemini to know, e.g.
# "My final exam is in 4 weeks and I can study about 2 hours per day."
EXTRA_NOTES = ""

# Optional: drop a cute .ttf font here and it will be used for the to-do list.
TODO_FONT_FILE = os.path.join(BASE_DIR, "assets", "todo_font.ttf")

# =====================================================================
# PASTEL PALETTE
# =====================================================================
PAPER = (255, 249, 242)
INK = (92, 70, 92)
INK_SOFT = (160, 138, 154)
WHITE = (255, 255, 255)
PINK = (255, 190, 207)
PINK_D = (240, 140, 172)
MINT = (183, 232, 208)
MINT_D = (112, 196, 160)
LAV = (226, 215, 249)
LAV_D = (162, 142, 220)
PEACH = (255, 218, 190)
PEACH_D = (245, 170, 130)
BUTTER = (255, 240, 180)
BUTTER_D = (235, 200, 90)
CHAPTER_COLORS = [(PINK, PINK_D), (MINT, MINT_D), (LAV, LAV_D), (PEACH, PEACH_D), (BUTTER, BUTTER_D)]

# Planner states
IDLE, LOADING, READY, ERROR, NO_SYLLABUS = "IDLE", "LOADING", "READY", "ERROR", "NO_SYLLABUS"


# =====================================================================
# GEMINI: prompt, schema, parsing
# =====================================================================
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "course_title": {"type": "string", "description": "Name of the course."},
        "overview": {"type": "string", "description": "1-2 friendly sentences on how to approach this course."},
        "chapters": {
            "type": "array",
            "description": "One entry per chapter / unit / topic of the syllabus, in order.",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Chapter name."},
                    "hours": {"type": "number", "description": "Recommended total study hours for this chapter."},
                    "why": {"type": "string", "description": "One short sentence on why it needs this much time."},
                    "study_steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "3-6 concrete step-by-step actions to master the chapter, in order.",
                    },
                    "todos": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "2-5 short checkable tasks for this chapter.",
                    },
                },
                "required": ["title", "hours", "study_steps", "todos"],
            },
        },
        "general_todos": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Tasks not tied to one chapter: assignments, deadlines, exam prep.",
        },
    },
    "required": ["course_title", "chapters"],
}


class PlannerError(Exception):
    """An error with a message that is safe to show to the player."""


def build_prompt():
    notes = ""
    if EXTRA_NOTES.strip():
        notes = f"\nExtra notes from the student: {EXTRA_NOTES.strip()}\n"
    return f"""You are a warm, organized study coach. I'm attaching my course syllabus as a PDF.
Read it carefully and make me a study plan.

For EVERY chapter / unit / topic in the syllabus (in order) give me:
- title: the chapter name.
- hours: how many hours I should spend studying it in total. Base this on the difficulty,
  the amount of material, and how heavily it counts in the exams/assignments. Use multiples of 0.5.
- why: one short sentence explaining the time estimate.
- study_steps: a step-by-step study guide of 3-6 concrete actions, in the order I should do them.
- todos: 2-5 short tasks I can tick off (e.g. "Do practice problems 1-10").

Also give general_todos: things not tied to one chapter (assignments, deadlines with dates, exam prep).
Also give a short course_title and a friendly 1-2 sentence overview.

Rules: keep every step/task under 90 characters, plain text only (no markdown, no emojis).
{notes}
Respond with ONLY a JSON object that follows this JSON schema:
{json.dumps(PLAN_SCHEMA)}"""


def _clean_text(value, limit=140):
    s = re.sub(r"\s+", " ", str(value if value is not None else "")).strip()
    s = re.sub(r"^(?:[-*\u2022]+|\d+[.)])\s+", "", s)   # strip leading bullets / "1." numbering
    if len(s) > limit:
        s = s[: limit - 3].rstrip() + "..."
    return s


def _clean_todos(items, keep_done=False, limit=140):
    out = []
    for item in items or []:
        done = False
        if isinstance(item, dict):
            done = bool(item.get("done")) if keep_done else False
            item = item.get("text") or item.get("task") or item.get("title") or ""
        text = _clean_text(item, limit)
        if text:
            out.append({"text": text, "done": done})
    return out


def normalize_plan(data, keep_done=False):
    """Turn Gemini's JSON (or a saved plan) into a clean, predictable dict."""
    if not isinstance(data, dict):
        raise PlannerError("Gemini's answer wasn't the format I expected.")

    chapters = []
    for ch in data.get("chapters") or []:
        if not isinstance(ch, dict):
            continue
        title = _clean_text(ch.get("title"), 80)
        if not title:
            continue
        try:
            hours = float(ch.get("hours") or 0)
        except (TypeError, ValueError):
            hours = 0.0
        hours = round(max(0.0, min(hours, 200.0)) * 4) / 4
        steps = [t["text"] for t in _clean_todos(ch.get("study_steps"), limit=160)]
        todos = _clean_todos(ch.get("todos"), keep_done=keep_done)
        if not todos:   # fall back to the steps so every chapter has something to tick off
            todos = [{"text": s, "done": False} for s in steps[:4]]
        chapters.append({
            "title": title,
            "hours": hours,
            "why": _clean_text(ch.get("why"), 160),
            "study_steps": steps,
            "todos": todos,
        })

    if not chapters:
        raise PlannerError("Gemini didn't find any chapters in the syllabus.")

    plan = {
        "course_title": _clean_text(data.get("course_title"), 60) or "Study Plan",
        "overview": _clean_text(data.get("overview"), 300),
        "total_hours": round(sum(c["hours"] for c in chapters), 2),
        "chapters": chapters,
        "general_todos": _clean_todos(data.get("general_todos"), keep_done=keep_done),
    }
    for key in ("syllabus_mtime", "model", "generated_at"):
        if key in data:
            plan[key] = data[key]
    return plan


def parse_json_text(text):
    if not text or not str(text).strip():
        raise PlannerError("Gemini sent back an empty answer.")
    t = str(text).strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.I)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except ValueError:
        pass
    a, b = t.find("{"), t.rfind("}")
    if a != -1 and b > a:
        try:
            return json.loads(t[a:b + 1])
        except ValueError:
            pass
    raise PlannerError("Couldn't understand Gemini's answer (it wasn't valid JSON).")


# Words in an error message that mean "this config style isn't supported, try the next one"
_CONFIG_ERROR_HINTS = (
    "response_format", "responseformat", "response_schema", "responseschema",
    "response_mime_type", "responsemimetype", "extra inputs", "unknown name",
    "invalid json payload", "validation error", "unexpected keyword",
)


def generate_plan_text(client, uploaded_file, prompt):
    """Ask Gemini for JSON. Tries the newest structured-output style first, then older ones,
    so it keeps working across google-genai versions."""
    attempts = [
        {"response_format": {"text": {"mime_type": "application/json", "schema": PLAN_SCHEMA}}},
        {"response_mime_type": "application/json", "response_schema": PLAN_SCHEMA},
        {"response_mime_type": "application/json"},
        None,
    ]
    for i, cfg in enumerate(attempts):
        try:
            kwargs = {"model": GEMINI_MODEL, "contents": [uploaded_file, prompt]}
            if cfg is not None:
                kwargs["config"] = cfg
            return client.models.generate_content(**kwargs).text
        except Exception as e:   # noqa: BLE001 - we re-raise anything that isn't a config problem
            msg = str(e).lower()
            is_config_problem = any(h in msg for h in _CONFIG_ERROR_HINTS)
            if not is_config_problem or i == len(attempts) - 1:
                raise


def friendly_error(e):
    if isinstance(e, PlannerError):
        return str(e)
    msg = str(e)
    low = msg.lower()
    if "api key" in low or "api_key" in low or "401" in low or "403" in low or "permission" in low:
        return "Gemini didn't accept the API key. Double-check GEMINI_API_KEY."
    if "429" in low or "quota" in low or "resource_exhausted" in low or "rate limit" in low:
        return "Gemini says the usage limit was reached. Try again in a little while."
    if ("404" in low or "not found" in low) and "model" in low:
        return f"Gemini doesn't know the model '{GEMINI_MODEL}'. Change GEMINI_MODEL at the top of study_plan.py."
    if any(k in low for k in ("getaddrinfo", "connection", "timed out", "timeout", "network", "name resolution")):
        return "Couldn't reach Gemini. Check the internet connection."
    return f"{type(e).__name__}: {msg[:160]}"


def find_syllabus():
    for folder in (os.getcwd(), BASE_DIR, os.path.join(BASE_DIR, "assets")):
        path = os.path.join(folder, SYLLABUS_FILE)
        if os.path.isfile(path):
            return path
    return None


def find_api_key():
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    for folder in (os.getcwd(), BASE_DIR):
        path = os.path.join(folder, API_KEY_FILE)
        if os.path.isfile(path):
            try:
                value = Path(path).read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if value:
                return value
    return None


# =====================================================================
# PLANNER: finds the PDF, talks to Gemini in a background thread, saves the plan
# =====================================================================
class StudyPlanner:
    def __init__(self):
        self.state = IDLE
        self.status = ""
        self.error = ""
        self.plan = None
        self._thread = None
        self._lock = threading.Lock()

    def start(self):
        """Find syllabus.pdf, then load the saved plan or ask Gemini (in the background)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self.error = ""
        cached = self._load_cached()
        path = find_syllabus()

        if path is None:
            if cached:
                self.plan, self.state = cached, READY
            else:
                self.state = NO_SYLLABUS
            return

        mtime = int(os.path.getmtime(path))
        if cached and cached.get("syllabus_mtime") == mtime:
            self.plan, self.state = cached, READY       # same syllabus as last time
            return

        self.state = LOADING
        self.status = "Reading syllabus.pdf"
        self._thread = threading.Thread(target=self._worker, args=(path, mtime), daemon=True)
        self._thread.start()

    # ---- background work ----
    def _worker(self, path, mtime):
        client, uploaded = None, None
        try:
            key = find_api_key()
            if not key:
                raise PlannerError("No Gemini API key found. Set GEMINI_API_KEY or create gemini_api_key.txt.")
            try:
                from google import genai
            except ImportError:
                raise PlannerError("The Gemini library is missing. Run:  pip install google-genai")

            client = genai.Client(api_key=key)

            self.status = "Uploading syllabus.pdf to Gemini"
            uploaded = client.files.upload(file=Path(path))
            uploaded = self._wait_until_ready(client, uploaded)

            self.status = "Gemini is writing your study plan"
            text = generate_plan_text(client, uploaded, build_prompt())
            plan = normalize_plan(parse_json_text(text))
            plan["syllabus_mtime"] = mtime
            plan["model"] = GEMINI_MODEL
            plan["generated_at"] = time.strftime("%Y-%m-%d %H:%M")

            self.plan = plan
            self.save()
            self.state = READY
        except Exception as e:   # noqa: BLE001
            print(f"[STUDY PLAN ERROR] {type(e).__name__}: {e}")
            self.error = friendly_error(e)
            self.state = ERROR
        finally:
            if client is not None and uploaded is not None:
                try:
                    client.files.delete(name=uploaded.name)   # tidy up the uploaded copy
                except Exception:   # noqa: BLE001
                    pass

    @staticmethod
    def _wait_until_ready(client, f, timeout=90):
        started = time.time()
        while True:
            state = getattr(getattr(f, "state", None), "name", None) or str(getattr(f, "state", "") or "")
            state = state.upper()
            if "FAILED" in state:
                raise PlannerError("Gemini couldn't read that PDF.")
            if "PROCESSING" not in state:
                return f
            if time.time() - started > timeout:
                raise PlannerError("Gemini took too long to process the PDF.")
            time.sleep(2)
            f = client.files.get(name=f.name)

    # ---- saving / loading ----
    def _load_cached(self):
        try:
            with open(PLAN_FILE, "r", encoding="utf-8") as fh:
                return normalize_plan(json.load(fh), keep_done=True)
        except (OSError, ValueError, PlannerError):
            return None

    def save(self):
        if not self.plan:
            return
        with self._lock:
            try:
                tmp = PLAN_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(self.plan, fh, indent=2, ensure_ascii=False)
                os.replace(tmp, PLAN_FILE)
            except OSError as e:
                print(f"[STUDY PLAN] Could not save progress: {e}")

    # ---- helpers for the UI ----
    def all_todos(self):
        if not self.plan:
            return []
        todos = []
        for ch in self.plan["chapters"]:
            todos.extend(ch["todos"])
        todos.extend(self.plan["general_todos"])
        return todos

    def progress(self):
        todos = self.all_todos()
        return sum(1 for t in todos if t["done"]), len(todos)


# =====================================================================
# DRAWING HELPERS
# =====================================================================
def wrap_text(font, text, max_w):
    lines, cur = [], ""
    for word in text.split():
        test = word if not cur else cur + " " + word
        if font.size(test)[0] <= max_w:
            cur = test
            continue
        if cur:
            lines.append(cur)
            cur = ""
        while font.size(word)[0] > max_w and len(word) > 1:   # a single very long word
            n = len(word)
            while n > 1 and font.size(word[:n])[0] > max_w:
                n -= 1
            lines.append(word[:n])
            word = word[n:]
        cur = word
    if cur:
        lines.append(cur)
    return lines or [""]


def fit_text(font, text, max_w):
    if font.size(text)[0] <= max_w:
        return text
    while len(text) > 1 and font.size(text + "...")[0] > max_w:
        text = text[:-1]
    return text.rstrip() + "..."


def draw_heart(surface, cx, cy, size, color):
    r = int(size * 0.27)
    pygame.draw.circle(surface, color, (int(cx - size * 0.23), int(cy - size * 0.08)), r)
    pygame.draw.circle(surface, color, (int(cx + size * 0.23), int(cy - size * 0.08)), r)
    pygame.draw.polygon(surface, color, [
        (cx - size * 0.48, cy + size * 0.02),
        (cx + size * 0.48, cy + size * 0.02),
        (cx, cy + size * 0.52),
    ])


def draw_sparkle(surface, cx, cy, r, color):
    k = r * 0.28
    pygame.draw.polygon(surface, color, [
        (cx, cy - r), (cx + k, cy - k), (cx + r, cy), (cx + k, cy + k),
        (cx, cy + r), (cx - k, cy + k), (cx - r, cy), (cx - k, cy - k),
    ])


def make_font(size, bold=False):
    if os.path.isfile(TODO_FONT_FILE):
        try:
            return pygame.font.Font(TODO_FONT_FILE, size)
        except Exception:   # noqa: BLE001
            pass
    names = "comicsansms,chalkboard,markerfelt,quicksand,nunito,varelaround,segoeui,arial,dejavusans"
    try:
        return pygame.font.SysFont(names, size, bold=bold)
    except Exception:   # noqa: BLE001
        return pygame.font.Font(None, size + 4)


# =====================================================================
# THE CUTE TO-DO PANEL
# =====================================================================
class _Metrics:
    """Pixel sizes for the panel. 'compact' is used on small screens such as a 480x320 TFT."""

    def __init__(self, compact):
        self.compact = compact
        if compact:
            self.fonts = (18, 14, 13, 11, 11)        # title, heading, body, small, tiny
            self.radius, self.pad_x = 18, 10
            self.header_h, self.title_y, self.sub_y, self.title_margin = 40, 20, None, 190
            self.scallops = False
            self.close = (24, 20, 15, 44)             # from right edge, y, radius, touch size
            self.close_label = "Close"                # compact screens get a labelled button
            self.heart = (26, 21, 18)
            self.sparkles = ((50, 11, 5),)
            self.tabs_y, self.tab_h = 46, 26
            self.tabs = (("todo", "To-Do", 12, 64), ("guide", "Guide", 82, 64))
            self.bar, self.bar_right_pad, self.bar_gap, self.done_label = (156, 52, 12), 62, 8, "done!"
            self.content_top, self.bottom_pad = 78, 22
            self.gap, self.gap_section = 8, 10
            self.chapter_h, self.circle_r, self.circle_x = 30, 10, 17
            self.pill_h, self.pill_pad_w, self.title_dx = 18, 14, 34
            self.line_h, self.row_min, self.row_pad = 15, 34, 10
            self.cb, self.text_dx = 20, 36
            self.step_r, self.step_x, self.step_dx = 9, 18, 34
            self.why_line, self.why_extra, self.why_dx = 14, 4, 10
            self.ov_extra, self.ov_pad, self.ov_ty = 14, 10, 7
            self.plan_top, self.plan_row, self.plan_label_w = 28, 20, 110
            self.plan_bar_h, self.plan_hours_w = 10, 44
            self.section_h, self.section_r, self.section_dx = 28, 8, 32
            self.tap_slop = 10
            self.st_icon_y, self.st_icon, self.st_text_y, self.st_head_gap, self.st_line_gap = 30, 34, 66, 22, 15
            self.tape = False
        else:
            self.fonts = (26, 19, 17, 15, 14)
            self.radius, self.pad_x = 26, 16
            self.header_h, self.title_y, self.sub_y, self.title_margin = 74, 27, 54, 170
            self.scallops = True
            self.close = (30, 28, 14, 36)
            self.close_label = None
            self.heart = (38, 32, 24)
            self.sparkles = ((68, 18, 7), (-100, 58, 6))   # negative x = from the right edge
            self.tabs_y, self.tab_h = 114, 30
            self.tabs = (("todo", "To-Do", 24, 110), ("guide", "Study Guide", 142, 150))
            self.bar, self.bar_right_pad, self.bar_gap, self.done_label = (24, 88, 14), 88, 10, "all done!"
            self.content_top, self.bottom_pad = 154, 34
            self.gap, self.gap_section = 10, 14
            self.chapter_h, self.circle_r, self.circle_x = 40, 13, 22
            self.pill_h, self.pill_pad_w, self.title_dx = 22, 20, 44
            self.line_h, self.row_min, self.row_pad = 20, 32, 12
            self.cb, self.text_dx = 22, 44
            self.step_r, self.step_x, self.step_dx = 11, 22, 42
            self.why_line, self.why_extra, self.why_dx = 18, 6, 14
            self.ov_extra, self.ov_pad, self.ov_ty = 22, 14, 11
            self.plan_top, self.plan_row, self.plan_label_w = 40, 26, 150
            self.plan_bar_h, self.plan_hours_w = 12, 52
            self.section_h, self.section_r, self.section_dx = 36, 10, 44
            self.tap_slop = 6
            self.st_icon_y, self.st_icon, self.st_text_y, self.st_head_gap, self.st_line_gap = 70, 54, 140, 28, 20
            self.tape = True


class TodoPanel:
    def __init__(self, screen_w, screen_h, show_pill=True):
        self.sw, self.sh = screen_w, screen_h
        self.compact = screen_w < 520 or screen_h < 560
        self.m = m = _Metrics(self.compact)
        if self.compact:
            self.pw, self.ph = min(screen_w - 8, 560), min(screen_h - 8, 400)
        else:
            self.pw, self.ph = 470, 548
        self.show_pill = show_pill
        self.planner = StudyPlanner()
        self.open = False
        self.tab = "todo"            # "todo" or "guide"
        self.scroll = 0.0

        self.rect = pygame.Rect((screen_w - self.pw) // 2, (screen_h - self.ph) // 2, self.pw, self.ph)
        self.content_rect = pygame.Rect(self.rect.x + m.pad_x, self.rect.y + m.content_top,
                                        self.pw - 2 * m.pad_x, self.ph - m.content_top - m.bottom_pad)
        self.pill_right = screen_w - 16
        self.pill_y = 100

        self.f_title = make_font(m.fonts[0], True)
        self.f_head = make_font(m.fonts[1], True)
        self.f_body = make_font(m.fonts[2])
        self.f_small = make_font(m.fonts[3])
        self.f_tiny = make_font(m.fonts[4], True)

        self._items = []
        self._content_h = 0
        self._layout_key = None
        self._hits = []
        self._press = None
        self._pill_rect = None
        self._text_cache = {}

        self._dim = pygame.Surface((screen_w, screen_h), pygame.SRCALPHA)
        self._dim.fill((60, 30, 70, 120))
        self._shadow = pygame.Surface((self.pw + 24, self.ph + 24), pygame.SRCALPHA)
        pygame.draw.rect(self._shadow, (0, 0, 0, 70), pygame.Rect(0, 8, self.pw + 24, self.ph + 12),
                         border_radius=m.radius + 8)

    # ---------- public API ----------
    def start(self):
        self.planner.start()

    def toggle(self):
        """Open/close the panel (for an on-screen button)."""
        self.open = not self.open
        self._press = None

    def button_text(self):
        """Short label for an on-screen 'To-Do' button that also shows the plan status."""
        state = self.planner.state
        if state == LOADING:
            return "Plan" + "." * (int(time.time() * 2) % 4)
        if state == READY:
            done, total = self.planner.progress()
            return f"To-Do {done}/{total}"
        if state == ERROR:
            return "Plan error"
        if state == NO_SYLLABUS:
            return "No PDF"
        return "To-Do"

    def handle_event(self, event):
        """Returns True when the panel used the event (so the game should ignore it)."""
        t = event.type
        if t == pygame.KEYDOWN:
            if event.key == pygame.K_l:
                self.toggle()
                return True
            if event.key == pygame.K_g and self.planner.state in (ERROR, NO_SYLLABUS):
                self.planner.start()          # try again
                return True
            if self.open and event.key in (pygame.K_PAGEUP, pygame.K_PAGEDOWN):
                step = self.content_rect.h - 40
                self.scroll += step if event.key == pygame.K_PAGEDOWN else -step
                self._clamp_scroll()
                return True
            return False

        wheel = getattr(pygame, "MOUSEWHEEL", None)
        if wheel is not None and t == wheel:
            if self.open:
                self.scroll -= event.y * 48
                self._clamp_scroll()
                return True
            return False

        if t == pygame.MOUSEBUTTONDOWN:
            if event.button != 1:
                return self.open
            if self.open:
                self._press = {"pos": event.pos, "scroll0": self.scroll, "moved": False}
                return True
            if self._pill_rect is not None and self._pill_rect.collidepoint(event.pos):
                self.open = True
                return True
            return False

        if t == pygame.MOUSEMOTION:
            if self.open and self._press is not None:
                dy = event.pos[1] - self._press["pos"][1]
                if abs(dy) > self.m.tap_slop:
                    self._press["moved"] = True
                if self._press["moved"]:
                    self.scroll = self._press["scroll0"] - dy      # drag to scroll (touchscreens)
                    self._clamp_scroll()
                return True
            return False

        if t == pygame.MOUSEBUTTONUP:
            if event.button != 1 or not self.open:
                return self.open
            press, self._press = self._press, None
            if press is not None and not press["moved"]:
                self._click(event.pos)
            return True

        return False

    def draw(self, surface):
        self._hits = []
        self._pill_rect = None
        if self.planner.state == IDLE:
            return
        if not self.open:
            if self.show_pill:
                self._draw_pill(surface)
            return

        m = self.m
        surface.blit(self._dim, (0, 0))
        surface.blit(self._shadow, (self.rect.x - 12, self.rect.y - 8))
        pygame.draw.rect(surface, PAPER, self.rect, border_radius=m.radius)

        self._draw_header(surface)
        plan = self.planner.plan if self.planner.state == READY else None
        if plan is not None:
            self._draw_progress_and_tabs(surface, plan)
            self._ensure_layout(plan)
            self._clamp_scroll()
            self._draw_items(surface)
        else:
            self._draw_status_card(surface)

        pygame.draw.rect(surface, PINK_D, self.rect, width=3, border_radius=m.radius)
        if m.tape:       # little washi tape on top
            tape = pygame.Rect(self.rect.x + self.pw // 2 - 48, self.rect.y - 11, 96, 22)
            pygame.draw.rect(surface, BUTTER, tape, border_radius=4)
            pygame.draw.rect(surface, BUTTER_D, tape, width=2, border_radius=4)

        hint = "drag to scroll" if self.compact else "scroll with the wheel or drag   -   L closes"
        footer = self._text(self.f_tiny, hint, INK_SOFT)
        self._blit_center(surface, footer, self.rect.x + self.pw // 2, self.rect.y + self.ph - m.bottom_pad // 2 - 2)

    # ---------- internals ----------
    def _text(self, font, text, color):
        key = (id(font), text, color)
        surf = self._text_cache.get(key)
        if surf is None:
            if len(self._text_cache) > 700:
                self._text_cache.clear()
            surf = font.render(text, True, color)
            self._text_cache[key] = surf
        return surf

    @staticmethod
    def _blit_center(surface, surf, cx, cy):
        surface.blit(surf, (int(cx - surf.get_width() / 2), int(cy - surf.get_height() / 2)))

    def _clamp_scroll(self):
        max_scroll = max(0, self._content_h - self.content_rect.h)
        self.scroll = max(0.0, min(float(self.scroll), float(max_scroll)))

    def _click(self, pos):
        for rect, action, payload in reversed(self._hits):
            if rect.collidepoint(pos):
                if action == "close":
                    self.open = False
                elif action == "tab":
                    self.tab = payload
                    self.scroll = 0.0
                elif action == "toggle":
                    payload["done"] = not payload["done"]
                    self.planner.save()
                elif action == "retry":
                    self.planner.start()
                return
        if not self.rect.collidepoint(pos):
            self.open = False        # tapped outside the panel

    # ---------- layout (what goes in the scrolling area) ----------
    def _ensure_layout(self, plan):
        key = (self.tab, id(plan))
        if key == self._layout_key:
            return
        self._layout_key = key
        m = self.m
        cw = self.content_rect.w - 8
        items, y = [], 0

        def add(kind, h, gap=0, **data):
            nonlocal y
            y += gap
            items.append(dict(kind=kind, y=y, h=h, **data))
            y += h

        def row_height(n):
            return max(m.row_min, n * m.line_h + m.row_pad)

        if self.tab == "todo":
            for ci, ch in enumerate(plan["chapters"]):
                add("chapter", m.chapter_h, gap=m.gap, idx=ci, ch=ch)
                if ch["why"]:
                    lines = wrap_text(self.f_small, ch["why"], cw - 2 * m.why_dx)
                    add("why", len(lines) * m.why_line + m.why_extra, lines=lines)
                for todo in ch["todos"]:
                    lines = wrap_text(self.f_body, todo["text"], cw - m.text_dx - 12)
                    add("todo", row_height(len(lines)), lines=lines, todo=todo)
            if plan["general_todos"]:
                add("section", m.section_h, gap=m.gap_section, title="Extra to-dos")
                for todo in plan["general_todos"]:
                    lines = wrap_text(self.f_body, todo["text"], cw - m.text_dx - 12)
                    add("todo", row_height(len(lines)), lines=lines, todo=todo)
        else:
            if plan["overview"]:
                lines = wrap_text(self.f_body, plan["overview"], cw - 2 * m.ov_pad)
                add("overview", len(lines) * m.line_h + m.ov_extra, gap=6, lines=lines)
            add("timeplan", m.plan_top + len(plan["chapters"]) * m.plan_row + 6, gap=m.gap)
            for ci, ch in enumerate(plan["chapters"]):
                add("chapter", m.chapter_h, gap=m.gap, idx=ci, ch=ch)
                for si, step in enumerate(ch["study_steps"]):
                    lines = wrap_text(self.f_body, step, cw - m.step_dx - 10)
                    add("step", row_height(len(lines)), lines=lines, n=si + 1)
        y += 12
        self._items, self._content_h = items, y
        self.scroll = min(self.scroll, max(0, y - self.content_rect.h))

    # ---------- drawing pieces ----------
    def _draw_header(self, surface):
        r, m = self.rect, self.m
        band = pygame.Rect(r.x, r.y, r.w, m.header_h)
        pygame.draw.rect(surface, PINK, band, border_top_left_radius=m.radius, border_top_right_radius=m.radius)
        if m.scallops:
            count = (r.w - 32) // 30
            step = (r.w - 32) / count
            for i in range(count + 1):                       # scalloped bottom edge
                pygame.draw.circle(surface, PINK, (int(r.x + 16 + i * step), r.y + m.header_h), 14)

        plan = self.planner.plan if self.planner.state == READY else None
        title = plan["course_title"] if plan else "Study To-Do"
        title = fit_text(self.f_title, title, r.w - m.title_margin)
        self._blit_center(surface, self._text(self.f_title, title, INK), r.x + r.w // 2, r.y + m.title_y)
        if m.sub_y is not None:
            if plan:
                sub = f"{len(plan['chapters'])} chapters  -  {plan['total_hours']:g} h of studying"
            else:
                sub = "your cozy study list"
            self._blit_center(surface, self._text(self.f_small, sub, INK), r.x + r.w // 2, r.y + m.sub_y)

        hx, hy, hs = m.heart
        draw_heart(surface, r.x + hx, r.y + hy, hs, WHITE)
        for sx, sy, sr in m.sparkles:
            draw_sparkle(surface, (r.x + sx) if sx >= 0 else (r.x + r.w + sx), r.y + sy, sr, WHITE)

        # close button (big touch target)
        off, cy_off, rad, hit = m.close
        if m.close_label:
            pill = pygame.Rect(r.x + r.w - 10 - 76, r.y + 6, 76, 28)
            pygame.draw.rect(surface, WHITE, pill, border_radius=14)
            pygame.draw.rect(surface, PINK_D, pill, width=2, border_radius=14)
            lab = self._text(self.f_tiny, m.close_label, PINK_D)
            xc, yc = pill.x + 18, pill.y + 14
            pygame.draw.line(surface, PINK_D, (xc - 4, yc - 4), (xc + 4, yc + 4), 2)
            pygame.draw.line(surface, PINK_D, (xc - 4, yc + 4), (xc + 4, yc - 4), 2)
            surface.blit(lab, (pill.x + 30, pill.y + 14 - lab.get_height() // 2))
            self._hits.append((pill.inflate(10, 16), "close", None))
        else:
            cx, cy = r.x + r.w - off, r.y + cy_off
            pygame.draw.circle(surface, WHITE, (cx, cy), rad)
            d = max(4, rad // 3)
            pygame.draw.line(surface, PINK_D, (cx - d, cy - d), (cx + d, cy + d), 3)
            pygame.draw.line(surface, PINK_D, (cx - d, cy + d), (cx + d, cy - d), 3)
            self._hits.append((pygame.Rect(cx - hit // 2, cy - hit // 2, hit, hit), "close", None))

    def _draw_progress_and_tabs(self, surface, plan):
        r, m = self.rect, self.m
        done, total = self.planner.progress()
        bx, by, bh = m.bar
        bar = pygame.Rect(r.x + bx, r.y + by, self.pw - bx - m.bar_right_pad, bh)
        pygame.draw.rect(surface, LAV, bar, border_radius=bh // 2)
        if total and done:
            fill = pygame.Rect(bar.x, bar.y, max(bh, int(bar.w * done / total)), bar.h)
            pygame.draw.rect(surface, MINT_D if done == total else PINK_D, fill, border_radius=bh // 2)
        label = m.done_label if total and done == total else f"{done}/{total}"
        lab = self._text(self.f_tiny, label, INK)
        surface.blit(lab, (bar.right + m.bar_gap, bar.y + bh // 2 - lab.get_height() // 2))

        for name, text, x, w in m.tabs:
            tab = pygame.Rect(r.x + x, r.y + m.tabs_y, w, m.tab_h)
            selected = self.tab == name
            pygame.draw.rect(surface, LAV_D if selected else LAV, tab, border_radius=m.tab_h // 2)
            self._blit_center(surface, self._text(self.f_tiny, text, WHITE if selected else INK),
                              tab.x + tab.w // 2, tab.y + tab.h // 2)
            self._hits.append((tab.inflate(6, 10), "tab", name))

    def _draw_items(self, surface):
        cr = self.content_rect
        surface.set_clip(cr)
        for it in self._items:
            top = cr.y + it["y"] - int(self.scroll)
            if top + it["h"] < cr.y or top > cr.bottom:
                continue
            getattr(self, "_draw_" + it["kind"])(surface, it, top)
        surface.set_clip(None)

        if self._content_h > cr.h:                            # slim scroll bar
            track = pygame.Rect(cr.right - 5, cr.y, 5, cr.h)
            pygame.draw.rect(surface, LAV, track, border_radius=3)
            thumb_h = max(24, int(cr.h * cr.h / self._content_h))
            max_scroll = self._content_h - cr.h
            thumb_y = cr.y + int((cr.h - thumb_h) * (self.scroll / max_scroll))
            pygame.draw.rect(surface, LAV_D, pygame.Rect(cr.right - 5, thumb_y, 5, thumb_h), border_radius=3)

    def _draw_chapter(self, surface, it, top):
        cr, m = self.content_rect, self.m
        col, dark = CHAPTER_COLORS[it["idx"] % len(CHAPTER_COLORS)]
        box = pygame.Rect(cr.x, top, cr.w - 8, m.chapter_h)
        pygame.draw.rect(surface, col, box, border_radius=min(14, m.chapter_h // 2))
        pygame.draw.circle(surface, dark, (box.x + m.circle_x, box.y + m.chapter_h // 2), m.circle_r)
        self._blit_center(surface, self._text(self.f_tiny, str(it["idx"] + 1), WHITE),
                          box.x + m.circle_x, box.y + m.chapter_h // 2)

        hours = self._text(self.f_tiny, f"{it['ch']['hours']:g} h", INK)
        pw = hours.get_width() + m.pill_pad_w
        pill = pygame.Rect(box.right - pw - 8, box.y + (m.chapter_h - m.pill_h) // 2, pw, m.pill_h)
        pygame.draw.rect(surface, WHITE, pill, border_radius=m.pill_h // 2)
        self._blit_center(surface, hours, pill.x + pw // 2, pill.y + m.pill_h // 2)

        title = fit_text(self.f_head, it["ch"]["title"], pill.x - (box.x + m.title_dx) - 8)
        surface.blit(self._text(self.f_head, title, INK),
                     (box.x + m.title_dx, box.y + m.chapter_h // 2 - self.f_head.get_height() // 2))

    def _draw_section(self, surface, it, top):
        cr, m = self.content_rect, self.m
        box = pygame.Rect(cr.x, top, cr.w - 8, m.section_h)
        pygame.draw.rect(surface, BUTTER, box, border_radius=min(14, m.section_h // 2))
        draw_sparkle(surface, box.x + m.section_dx // 2, box.y + m.section_h // 2, m.section_r, BUTTER_D)
        surface.blit(self._text(self.f_head, it["title"], INK),
                     (box.x + m.section_dx, box.y + m.section_h // 2 - self.f_head.get_height() // 2))

    def _draw_why(self, surface, it, top):
        m = self.m
        for i, line in enumerate(it["lines"]):
            surface.blit(self._text(self.f_small, line, INK_SOFT),
                         (self.content_rect.x + m.why_dx, top + 3 + i * m.why_line))

    def _draw_todo(self, surface, it, top):
        cr, m = self.content_rect, self.m
        todo = it["todo"]
        done = todo["done"]
        row = pygame.Rect(cr.x, top, cr.w - 8, it["h"])

        box = pygame.Rect(row.x + 8, top + (m.row_min - m.cb) // 2, m.cb, m.cb)
        pygame.draw.rect(surface, MINT_D if done else WHITE, box, border_radius=7)
        pygame.draw.rect(surface, MINT_D if done else LAV_D, box, width=2, border_radius=7)
        if done:
            k = m.cb / 22.0
            pygame.draw.lines(surface, WHITE, False,
                              [(box.x + 5 * k, box.y + 12 * k), (box.x + 9 * k, box.y + 16 * k),
                               (box.x + 17 * k, box.y + 6 * k)], 3)

        color = INK_SOFT if done else INK
        n = len(it["lines"])
        ty0 = top + (it["h"] - n * m.line_h) // 2
        for i, line in enumerate(it["lines"]):
            surf = self._text(self.f_body, line, color)
            ty = ty0 + i * m.line_h
            surface.blit(surf, (row.x + m.text_dx, ty))
            if done:
                mid = ty + surf.get_height() // 2
                pygame.draw.line(surface, INK_SOFT, (row.x + m.text_dx, mid),
                                 (row.x + m.text_dx + surf.get_width(), mid), 2)

        for dx in range(row.x + m.text_dx, row.right - 8, 8):   # dotted divider
            pygame.draw.line(surface, LAV, (dx, row.bottom - 1), (dx + 3, row.bottom - 1))

        clipped = row.clip(self.content_rect)
        if clipped.h > 0 and clipped.w > 0:
            self._hits.append((clipped, "toggle", todo))

    def _draw_step(self, surface, it, top):
        cr, m = self.content_rect, self.m
        cy = top + m.row_min // 2
        pygame.draw.circle(surface, LAV_D, (cr.x + m.step_x, cy), m.step_r)
        self._blit_center(surface, self._text(self.f_tiny, str(it["n"]), WHITE), cr.x + m.step_x, cy)
        n = len(it["lines"])
        ty0 = top + (it["h"] - n * m.line_h) // 2
        for i, line in enumerate(it["lines"]):
            surface.blit(self._text(self.f_body, line, INK), (cr.x + m.step_dx, ty0 + i * m.line_h))

    def _draw_overview(self, surface, it, top):
        cr, m = self.content_rect, self.m
        box = pygame.Rect(cr.x, top, cr.w - 8, it["h"])
        pygame.draw.rect(surface, BUTTER, box, border_radius=14)
        draw_sparkle(surface, box.right - 16, box.y + 14, 7 if not m.compact else 5, BUTTER_D)
        for i, line in enumerate(it["lines"]):
            surface.blit(self._text(self.f_body, line, INK), (box.x + m.ov_pad, box.y + m.ov_ty + i * m.line_h))

    def _draw_timeplan(self, surface, it, top):
        cr, m = self.content_rect, self.m
        plan = self.planner.plan
        box = pygame.Rect(cr.x, top, cr.w - 8, it["h"])
        pygame.draw.rect(surface, WHITE, box, border_radius=14)
        pygame.draw.rect(surface, LAV, box, width=2, border_radius=14)
        surface.blit(self._text(self.f_head, "Hours plan", INK), (box.x + m.ov_pad, box.y + m.ov_ty - 3))
        total = self._text(self.f_tiny, f"{plan['total_hours']:g} h total", INK_SOFT)
        surface.blit(total, (box.right - total.get_width() - m.ov_pad, box.y + m.ov_ty + 1))

        bar_x = box.x + m.ov_pad + m.plan_label_w + 8
        bar_max = box.right - m.ov_pad - m.plan_hours_w - bar_x
        peak = max((c["hours"] for c in plan["chapters"]), default=1) or 1
        for i, ch in enumerate(plan["chapters"]):
            y = box.y + m.plan_top + i * m.plan_row
            label = fit_text(self.f_small, ch["title"], m.plan_label_w)
            surface.blit(self._text(self.f_small, label, INK), (box.x + m.ov_pad, y))
            by = y + (self.f_small.get_height() - m.plan_bar_h) // 2
            pygame.draw.rect(surface, LAV, pygame.Rect(bar_x, by, bar_max, m.plan_bar_h), border_radius=m.plan_bar_h // 2)
            fill_w = max(m.plan_bar_h, int(bar_max * ch["hours"] / peak)) if ch["hours"] > 0 else 0
            if fill_w:
                pygame.draw.rect(surface, CHAPTER_COLORS[i % len(CHAPTER_COLORS)][1],
                                 pygame.Rect(bar_x, by, fill_w, m.plan_bar_h), border_radius=m.plan_bar_h // 2)
            hrs = self._text(self.f_tiny, f"{ch['hours']:g} h", INK)
            surface.blit(hrs, (box.right - m.ov_pad - hrs.get_width(), y))

    def _draw_status_card(self, surface):
        """Shown instead of the list while loading, on errors, or when there's no syllabus."""
        cr, m = self.content_rect, self.m
        state = self.planner.state
        cx = cr.x + cr.w // 2
        iy = cr.y + m.st_icon_y
        if state == LOADING:
            bob = math.sin(time.time() * 5) * (8 if not m.compact else 5)
            draw_heart(surface, cx, iy + bob, m.st_icon, PINK_D)
            dots = "." * (int(time.time() * 2) % 4)
            lines = [self.planner.status + dots, "this can take a little while"]
        elif state == NO_SYLLABUS:
            draw_sparkle(surface, cx, iy, m.st_icon // 2, BUTTER_D)
            lines = ["No syllabus.pdf found", "Put a file called syllabus.pdf next to the game,",
                     "then tap Try again."]
        else:
            draw_heart(surface, cx, iy, m.st_icon, LAV_D)
            err = wrap_text(self.f_small, self.planner.error, cr.w - 30)[:3]
            lines = ["Oops, something went wrong"] + err
        y = cr.y + m.st_text_y
        for i, line in enumerate(lines):
            font = self.f_head if i == 0 else self.f_small
            self._blit_center(surface, self._text(font, line, INK if i == 0 else INK_SOFT), cx, y)
            y += m.st_head_gap if i == 0 else m.st_line_gap

        if state in (ERROR, NO_SYLLABUS):                    # touch-friendly retry button
            btn = pygame.Rect(cx - 70, cr.bottom - 42, 140, 34)
            pygame.draw.rect(surface, PINK, btn, border_radius=17)
            pygame.draw.rect(surface, PINK_D, btn, width=2, border_radius=17)
            self._blit_center(surface, self._text(self.f_head, "Try again", INK), btn.x + btn.w // 2, btn.y + 17)
            self._hits.append((btn, "retry", None))

    def _draw_pill(self, surface):
        """Small status badge shown while the panel is closed."""
        state = self.planner.state
        if state == LOADING:
            text, color = "Making your study plan" + "." * (int(time.time() * 2) % 4), LAV
        elif state == READY:
            done, total = self.planner.progress()
            text, color = f"To-Do {done}/{total}  (press L)", MINT
        elif state == ERROR:
            text, color = "Study plan problem (press L)", PINK
        else:
            text, color = "No syllabus.pdf (press L)", BUTTER
        surf = self._text(self.f_tiny, text, INK)
        w = surf.get_width() + 28
        if state == LOADING:
            w = max(w, 230)       # fixed width so the pill doesn't wobble while the dots animate
        rect = pygame.Rect(self.pill_right - w, self.pill_y, w, 28)
        pygame.draw.rect(surface, color, rect, border_radius=14)
        pygame.draw.rect(surface, WHITE, rect, width=2, border_radius=14)
        surface.blit(surf, (rect.x + 14, rect.y + 14 - surf.get_height() // 2))
        self._pill_rect = rect
