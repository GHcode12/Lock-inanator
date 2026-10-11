"""Study Buddy - cute touch-screen version for a 480x320 TFT (ILI9486 + XPT2046).

Buttons along the top: Start/Pause, Reset, Skip, To-Do (your study plan) and Quit.
Keyboard still works too: SPACE start/pause, R reset, N skip, L to-do, ESC/Q quit.
"""
import math
import sys
import time

from picamera2 import Picamera2
from ultralytics import YOLO
import pygame

from study_plan import (
    TodoPanel, make_font, fit_text, draw_heart, draw_sparkle,
    PAPER, INK, INK_SOFT, WHITE, PINK, PINK_D, MINT, MINT_D, LAV, LAV_D,
    PEACH, PEACH_D, BUTTER, BUTTER_D,
)

pygame.init()
SCREEN_WIDTH = 480
SCREEN_HEIGHT = 320
screen = pygame.display.set_mode(
    (SCREEN_WIDTH, SCREEN_HEIGHT), pygame.FULLSCREEN
)
pygame.display.set_caption("Study Buddy - TFT Touch Version")
pygame.mouse.set_visible(False)     # no mouse pointer on a touch screen

clock = pygame.time.Clock()
# SPI screens only show roughly 10-20 frames per second, so 30 is plenty
# and leaves more CPU for the phone detection.
FPS = 30

TEXT_COLOR = (50, 50, 50)

# ==========================================
# TOUCH SETTINGS
# ==========================================
# If taps land in the wrong place (mirrored or rotated), change these:
TOUCH_SWAP_XY = False
TOUCH_INVERT_X = False
TOUCH_INVERT_Y = False
# Shows a red dot where the game thinks you are touching.
SHOW_TOUCH_DEBUG = False

# ==========================================
# POMODORO SETTINGS
# ==========================================
FOCUS_MINUTES = 25
SHORT_BREAK_MINUTES = 5
LONG_BREAK_MINUTES = 15
SESSIONS_BEFORE_LONG_BREAK = 4

# Timer circle position (right side, just under the button bar)
TIMER_CENTER = (430, 84)

# ==========================================
# CAMERA / VISION SETTINGS
# ==========================================
CAMERA_SIZE = (480, 320)
VISION_CHECK_INTERVAL = 2.0   # seconds between phone checks
DETECTION_CONFIDENCE = 0.25
PHONE_PENALTY = 15

# ==========================================
# VISION MODEL SETUP (Picamera2 & YOLO)
# ==========================================
try:
  picam2 = Picamera2()
  try:
    picam2.configure(
        picam2.create_preview_configuration(
            main={"size": CAMERA_SIZE, "format": "RGB888"}
        )
    )
  except Exception as cfg_err:
    print(f"[WARNING] Preview config failed ({cfg_err}); using still config.")
    picam2.configure(picam2.create_still_configuration())
  picam2.start()
  print("[INFO] Camera initialized successfully.")
except Exception as e:
  print(f"[WARNING] Camera failed to start: {e}. Running in simulation mode.")
  picam2 = None

try:
  model = YOLO("yolov8n.pt")
  TARGET_CLASSES = ["cell phone", "book", "laptop", "keyboard", "person"]
except Exception as e:
  print(f"[ERROR] Failed to load YOLO model: {e}")
  pygame.quit()
  sys.exit()

last_vision_check = 0
vision_status = "waiting for first check..."


# =====================================================================
# ASSETS
# =====================================================================
# Helper function for loading zero-padded animation frames (01 to 08)
def load_animation_frames(name, count=8):
  frames = []
  for i in range(1, count + 1):
    path = f"assets/{name}_{i:02d}.png"
    try:
      img = pygame.image.load(path).convert_alpha()
      # Scaled slightly smaller for 480x320 screen
      scaled = pygame.transform.scale(
          img, (int(img.get_width() * 1.2), int(img.get_height() * 1.2))
      )
      frames.append(scaled)
    except Exception as e:
      print(f"[WARNING] Could not load animation frame {path}: {e}")
  return frames


# Load Background, Table, and Chair
try:
  room_img = pygame.image.load("assets/room.png").convert()
  room_img = pygame.transform.scale(room_img, (SCREEN_WIDTH, SCREEN_HEIGHT))

  table_img = pygame.image.load("assets/table.png").convert_alpha()
  table_img = pygame.transform.scale(
      table_img,
      (int(table_img.get_width() * 1.2), int(table_img.get_height() * 1.2)),
  )

  chair_img = pygame.image.load("assets/chair.png").convert_alpha()
  chair_img = pygame.transform.scale(
      chair_img,
      (int(chair_img.get_width() * 1.2), int(chair_img.get_height() * 1.2)),
  )
except Exception as e:
  print(f"[ERROR] Failed to load environment assets: {e}")
  pygame.quit()
  sys.exit()

# Scaled positions for 480x320 layout
TABLE_X, TABLE_Y = 20, 190
CHAIR_X, CHAIR_Y = 60, 200

# Load All Character Animations
animations = {
    "STANDING": load_animation_frames("standing", 8),
    "READING": load_animation_frames("reading", 8),
    "FLIPPING": load_animation_frames("flipping_book", 8),
    "WALK_LEFT": load_animation_frames("walk_left", 8),
    "CONFUSED": load_animation_frames("confused", 8),
}


# =====================================================================
# POMODORO TIMER
# =====================================================================
FOCUS = "FOCUS"
SHORT_BREAK = "SHORT BREAK"
LONG_BREAK = "LONG BREAK"

MODE_COLORS = {
    FOCUS: (240, 98, 84),
    SHORT_BREAK: (96, 205, 148),
    LONG_BREAK: (98, 160, 240),
}

DISC_BG = (33, 37, 60, 255)
TRACK_COLOR = (58, 64, 92, 255)
TEXT_MAIN = (240, 243, 252)


class PomodoroTimer:
  RING_OUTER = 32  # Smaller radius for the 480x320 display
  RING_THICK = 5
  FADE_TIME = 0.25

  def __init__(self, center):
    self.center = center
    self.durations = {
        FOCUS: FOCUS_MINUTES * 60,
        SHORT_BREAK: SHORT_BREAK_MINUTES * 60,
        LONG_BREAK: LONG_BREAK_MINUTES * 60,
    }
    self.mode = FOCUS
    self.remaining = self.durations[FOCUS]
    self.running = False
    self.sessions_done = 0
    self.font_time = make_font(16, True)
    self.visibility = 0.0
    self._ring_cache_key = None
    self._ring_cache_surf = None

    # soft shadow under the circle
    size = (self.RING_OUTER + 6) * 2
    self._shadow = pygame.Surface((size, size), pygame.SRCALPHA)
    pygame.draw.circle(self._shadow, (60, 30, 70, 70), (size // 2, size // 2), self.RING_OUTER + 2)

  @property
  def total(self):
    return self.durations[self.mode]

  def toggle(self):
    self.running = not self.running

  def reset(self):
    self.running = False
    self.remaining = self.total

  def skip(self):
    self._go_to_next_phase(completed=False)

  def _go_to_next_phase(self, completed):
    if self.mode == FOCUS:
      if completed:
        self.sessions_done += 1
        if self.sessions_done % SESSIONS_BEFORE_LONG_BREAK == 0:
          self.mode = LONG_BREAK
        else:
          self.mode = SHORT_BREAK
      else:
        self.mode = SHORT_BREAK
    else:
      self.mode = FOCUS
    self.running = False
    self.remaining = self.total

  def update(self, dt):
    step = dt / self.FADE_TIME
    if self.running:
      self.visibility = min(1.0, self.visibility + step)
    else:
      self.visibility = max(0.0, self.visibility - step)

    if not self.running:
      return None
    self.remaining -= dt
    if self.remaining <= 0:
      finished = self.mode
      self._go_to_next_phase(completed=True)
      return finished
    return None

  def time_string(self):
    secs = max(0, int(math.ceil(self.remaining)))
    return f"{secs // 60:02d}:{secs % 60:02d}"

  def _ring_surface(self, fraction, accent):
    key = (self.mode, int(math.ceil(self.remaining)))
    if key == self._ring_cache_key and self._ring_cache_surf is not None:
      return self._ring_cache_surf

    ss = 2
    size = (self.RING_OUTER + 2) * 2
    big = pygame.Surface((size * ss, size * ss), pygame.SRCALPHA)
    c = size * ss // 2
    r_out = self.RING_OUTER * ss
    thick = self.RING_THICK * ss
    r_in = r_out - thick

    pygame.draw.circle(big, DISC_BG, (c, c), r_in)
    pygame.draw.circle(big, TRACK_COLOR, (c, c), r_out, thick)

    color = (*accent, 255)
    if fraction >= 0.9995:
      pygame.draw.circle(big, color, (c, c), r_out, thick)
    elif fraction > 0:
      a0 = -math.pi / 2
      a1 = a0 + fraction * 2 * math.pi
      steps = max(3, int(fraction * 180))
      outer, inner = [], []
      for i in range(steps + 1):
        a = a0 + (a1 - a0) * i / steps
        outer.append((c + r_out * math.cos(a), c + r_out * math.sin(a)))
        inner.append((c + r_in * math.cos(a), c + r_in * math.sin(a)))
      pygame.draw.polygon(big, color, outer + inner[::-1])

    ring = pygame.transform.smoothscale(big, (size, size))
    self._ring_cache_key = key
    self._ring_cache_surf = ring
    return ring

  def draw(self, surface):
    if self.visibility <= 0:
      return
    accent = MODE_COLORS[self.mode]
    fraction = max(0.0, min(1.0, self.remaining / self.total))
    ring = self._ring_surface(fraction, accent)

    layer = pygame.Surface(ring.get_size(), pygame.SRCALPHA)
    layer.blit(ring, (0, 0))
    time_surf = self.font_time.render(self.time_string(), True, TEXT_MAIN)
    layer.blit(
        time_surf,
        (
            ring.get_width() // 2 - time_surf.get_width() // 2,
            ring.get_height() // 2 - time_surf.get_height() // 2,
        ),
    )
    alpha = int(255 * self.visibility)
    layer.set_alpha(alpha)
    self._shadow.set_alpha(alpha)
    surface.blit(self._shadow, (self.center[0] - self._shadow.get_width() // 2,
                                self.center[1] - self._shadow.get_height() // 2 + 3))
    surface.blit(layer, (self.center[0] - layer.get_width() // 2,
                         self.center[1] - layer.get_height() // 2))


# =====================================================================
# CHARACTER (time-based, so he moves at the same speed at any frame rate)
# =====================================================================
class Character:

  def __init__(self, x, y):
    self.x = x
    self.y = y
    self.target_x = 35
    self.target_y = 130
    self.walk_speed = 120.0      # pixels per second

    self.is_timer_on = False     # True only while a FOCUS session is running
    self.state = "STANDING"
    self.points = 100

    self.anim_index = 0
    self.anim_timer = 0.0
    self.anim_interval = 0.13    # seconds per animation frame
    self.confused_timer = 0.0

  def start_focus(self):
    self.is_timer_on = True
    self.state = "WALK_LEFT"
    self.anim_index = 0

  def stop_focus(self):
    self.is_timer_on = False
    self.state = "STANDING"
    self.x, self.y = 200, 140
    self.anim_index = 0

  def lose_points(self, amount):
    self.points -= amount
    if self.points < 0:
      self.points = 0
    self.state = "CONFUSED"
    self.anim_index = 0
    self.confused_timer = 1.5

  def update(self, dt):
    if self.state == "CONFUSED":
      self.confused_timer -= dt
      if self.confused_timer <= 0:
        if self.is_timer_on:
          at_desk = (
              math.hypot(self.target_x - self.x, self.target_y - self.y) <= 2
          )
          self.state = "FLIPPING" if at_desk else "WALK_LEFT"
        else:
          self.state = "STANDING"
        self.anim_index = 0

    elif self.state == "WALK_LEFT":
      dx = self.target_x - self.x
      dy = self.target_y - self.y
      distance = math.hypot(dx, dy)
      if distance > 2:
        move = min(self.walk_speed * dt, distance)
        self.x += (dx / distance) * move
        self.y += (dy / distance) * move
      else:
        self.x = self.target_x
        self.y = self.target_y
        self.state = "FLIPPING"
        self.anim_index = 0

    self.anim_timer += dt
    if self.anim_timer >= self.anim_interval:
      self.anim_timer = 0.0
      current_anim = self.state
      if current_anim not in animations or len(animations[current_anim]) == 0:
        current_anim = "STANDING"
      max_frames = len(animations[current_anim])
      self.anim_index = (self.anim_index + 1) % max_frames

  def draw(self, surface):
    anim_key = self.state
    if anim_key not in animations or len(animations[anim_key]) == 0:
      anim_key = "STANDING"
    current_frames = animations[anim_key]
    current_image = current_frames[self.anim_index % len(current_frames)]
    surface.blit(current_image, (self.x, self.y))


character = Character(200, 140)
timer = PomodoroTimer(TIMER_CENTER)

# Gemini study plan + cute to-do list (reads syllabus.pdf; opened with the To-Do button)
todo_panel = TodoPanel(SCREEN_WIDTH, SCREEN_HEIGHT, show_pill=False)
todo_panel.start()


# =====================================================================
# TOUCH INPUT: turn touches / mouse into plain mouse events in screen pixels
# =====================================================================
def fix_pos(x, y):
  """Applies the TOUCH_SWAP/INVERT settings to a screen position."""
  nx = x / max(1, SCREEN_WIDTH - 1)
  ny = y / max(1, SCREEN_HEIGHT - 1)
  if TOUCH_SWAP_XY:
    nx, ny = ny, nx
  if TOUCH_INVERT_X:
    nx = 1.0 - nx
  if TOUCH_INVERT_Y:
    ny = 1.0 - ny
  nx, ny = min(max(nx, 0.0), 1.0), min(max(ny, 0.0), 1.0)
  return int(nx * (SCREEN_WIDTH - 1)), int(ny * (SCREEN_HEIGHT - 1))


def mouse_event(kind, pos):
  if kind == "down":
    return pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=pos, button=1)
  if kind == "up":
    return pygame.event.Event(pygame.MOUSEBUTTONUP, pos=pos, button=1)
  return pygame.event.Event(pygame.MOUSEMOTION, pos=pos, rel=(0, 0), buttons=(1, 0, 0))


def normalize_event(event):
  """Returns a mouse-style event (or the original event); None means 'ignore this one'."""
  t = event.type
  if t in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP, pygame.MOUSEMOTION):
    if getattr(event, "touch", False):
      return None          # the same touch also arrives as a finger event; use that one
    if t != pygame.MOUSEMOTION and getattr(event, "button", 1) != 1:
      return event         # mouse wheel etc.
    kind = {pygame.MOUSEBUTTONDOWN: "down", pygame.MOUSEBUTTONUP: "up"}.get(t, "move")
    return mouse_event(kind, fix_pos(*event.pos))
  if t in (pygame.FINGERDOWN, pygame.FINGERUP, pygame.FINGERMOTION):
    kind = {pygame.FINGERDOWN: "down", pygame.FINGERUP: "up"}.get(t, "move")
    return mouse_event(kind, fix_pos(event.x * (SCREEN_WIDTH - 1), event.y * (SCREEN_HEIGHT - 1)))
  return event


# =====================================================================
# CUTE UI
# =====================================================================
f_button = make_font(14, True)
f_ui = make_font(14, True)
f_small = make_font(12, False)
f_tiny = make_font(12, True)
f_dialog = make_font(19, True)

GREY_FACE, GREY_EDGE = (234, 230, 242), (176, 168, 202)


def icon_play(surface, cx, cy, color):
  pygame.draw.polygon(surface, color, [(cx - 4, cy - 7), (cx - 4, cy + 7), (cx + 7, cy)])


def icon_pause(surface, cx, cy, color):
  pygame.draw.rect(surface, color, pygame.Rect(cx - 6, cy - 7, 4, 14), border_radius=2)
  pygame.draw.rect(surface, color, pygame.Rect(cx + 2, cy - 7, 4, 14), border_radius=2)


def icon_heart(surface, cx, cy, color):
  draw_heart(surface, cx, cy - 1, 16, color)


def icon_cross(surface, cx, cy, color):
  pygame.draw.line(surface, color, (cx - 6, cy - 6), (cx + 6, cy + 6), 3)
  pygame.draw.line(surface, color, (cx - 6, cy + 6), (cx + 6, cy - 6), 3)


def draw_button(surface, rect, label, colors, icon=None, icon_color=INK, pressed=False, font=None):
  """A chunky pastel button with a little 3D edge (sinks down when pressed)."""
  font = font or f_button
  face, edge = colors
  pygame.draw.rect(surface, edge, rect.move(0, 3), border_radius=12)
  top = rect.move(0, 2 if pressed else 0)
  pygame.draw.rect(surface, edge if pressed else face, top, border_radius=12)
  pygame.draw.rect(surface, edge, top, width=2, border_radius=12)
  if not pressed:
    pygame.draw.line(surface, WHITE, (top.x + 12, top.y + 5), (top.right - 12, top.y + 5), 2)  # shine

  left = top.x
  if icon is not None:
    icon(surface, top.x + 18, top.centery, icon_color)
    left = top.x + 26
  if label:
    text = font.render(fit_text(font, label, top.right - left - 8), True, INK)
    cx = left + (top.right - left) // 2
    surface.blit(text, (cx - text.get_width() // 2, top.centery - text.get_height() // 2))


BUTTON_Y, BUTTON_H = 4, 36
BUTTON_RECTS = {
    "start": pygame.Rect(6, BUTTON_Y, 104, BUTTON_H),
    "reset": pygame.Rect(116, BUTTON_Y, 64, BUTTON_H),
    "skip": pygame.Rect(186, BUTTON_Y, 56, BUTTON_H),
    "todo": pygame.Rect(248, BUTTON_Y, 118, BUTTON_H),
    "quit": pygame.Rect(432, BUTTON_Y, 42, BUTTON_H),
}

# Quit dialog (asks first, so a stray tap can't close the game)
DIALOG_RECT = pygame.Rect(0, 0, 320, 150)
DIALOG_RECT.center = (SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2)
DIALOG_BUTTONS = {
    "stay": pygame.Rect(DIALOG_RECT.x + 24, DIALOG_RECT.bottom - 62, 120, 46),
    "quit": pygame.Rect(DIALOG_RECT.right - 144, DIALOG_RECT.bottom - 62, 120, 46),
}

STATE_LOOK = {   # friendly state name, pill color
    "STANDING": ("Chilling", MINT),
    "WALK_LEFT": ("Off to the desk", LAV),
    "FLIPPING": ("Studying", BUTTER),
    "READING": ("Reading", BUTTER),
    "CONFUSED": ("Oops!", PINK),
}

HEART_COUNT = 5
dim_overlay = pygame.Surface((SCREEN_WIDTH, SCREEN_HEIGHT), pygame.SRCALPHA)
dim_overlay.fill((60, 30, 70, 120))
status_card = pygame.Rect(6, 46, 206, 68)

pressed_button = None        # name of the top-bar button being pressed
pressed_dialog = None        # "stay" / "quit" while pressing a dialog button
quit_dialog = False
banner_text, banner_color, banner_until = "", PINK, 0.0
last_touch_pos = None
running = True


def button_at(pos):
  for name, rect in BUTTON_RECTS.items():
    if rect.inflate(6, 8).collidepoint(pos):    # a little extra room for fingers
      return name
  return None


def dialog_button_at(pos):
  for name, rect in DIALOG_BUTTONS.items():
    if rect.inflate(8, 8).collidepoint(pos):
      return name
  return None


def show_banner(text, color, seconds=2.5):
  global banner_text, banner_color, banner_until
  banner_text, banner_color, banner_until = text, color, time.time() + seconds


def toggle_timer():
  timer.toggle()
  if timer.running and timer.mode == FOCUS:
    character.start_focus()
  else:
    character.stop_focus()


def run_action(name):
  global quit_dialog
  if name == "start":
    toggle_timer()
  elif name == "reset":
    timer.reset()
    character.stop_focus()
  elif name == "skip":
    timer.skip()
    character.stop_focus()
  elif name == "todo":
    todo_panel.toggle()
  elif name == "quit":
    quit_dialog = True


def draw_top_bar(surface):
  # Start / Pause / Resume
  if timer.running:
    label, icon, colors = "Pause", icon_pause, (BUTTER, BUTTER_D)
  else:
    label = "Resume" if timer.remaining < timer.total - 0.5 else "Start"
    icon, colors = icon_play, (MINT, MINT_D)
  draw_button(surface, BUTTON_RECTS["start"], label, colors, icon, pressed=pressed_button == "start")
  draw_button(surface, BUTTON_RECTS["reset"], "Reset", (PEACH, PEACH_D), pressed=pressed_button == "reset")
  draw_button(surface, BUTTON_RECTS["skip"], "Skip", (LAV, LAV_D), pressed=pressed_button == "skip")
  draw_button(surface, BUTTON_RECTS["todo"], todo_panel.button_text(), (PINK, PINK_D),
              icon_heart, WHITE, pressed=pressed_button == "todo")
  draw_button(surface, BUTTON_RECTS["quit"], "", (GREY_FACE, GREY_EDGE), icon_cross,
              (140, 130, 165), pressed=pressed_button == "quit")


def draw_status_card(surface):
  card = pygame.Surface(status_card.size, pygame.SRCALPHA)
  pygame.draw.rect(card, (255, 255, 255, 185), card.get_rect(), border_radius=14)
  pygame.draw.rect(card, (*PINK_D, 220), card.get_rect(), width=2, border_radius=14)
  surface.blit(card, status_card.topleft)

  # hearts = points (each penalty takes some away)
  full = int(character.points / 20 + 0.5)
  for i in range(HEART_COUNT):
    draw_heart(surface, status_card.x + 22 + i * 19, status_card.y + 17, 16, PINK_D if i < full else LAV)
  pts = f_ui.render(str(character.points), True, INK)
  surface.blit(pts, (status_card.x + 22 + HEART_COUNT * 19, status_card.y + 17 - pts.get_height() // 2))

  # state pill
  name, color = STATE_LOOK.get(character.state, (character.state.title(), LAV))
  label = f_tiny.render(name, True, INK)
  pill = pygame.Rect(status_card.x + 8, status_card.y + 31, label.get_width() + 18, 18)
  pygame.draw.rect(surface, color, pill, border_radius=9)
  surface.blit(label, (pill.x + 9, pill.centery - label.get_height() // 2))

  # camera line
  if not picam2:
    vision_line, dot = "camera off", INK_SOFT
  elif not character.is_timer_on:
    vision_line, dot = "camera resting", INK_SOFT
  else:
    vision_line, dot = f"watching - {vision_status}", MINT_D
  pygame.draw.circle(surface, dot, (status_card.x + 14, status_card.y + 57), 4)
  text = f_small.render(fit_text(f_small, vision_line, status_card.w - 34), True, INK_SOFT)
  surface.blit(text, (status_card.x + 24, status_card.y + 57 - text.get_height() // 2))


def draw_banner(surface):
  if time.time() >= banner_until:
    return
  msg = f_ui.render(banner_text, True, INK)
  box = pygame.Rect(0, 0, msg.get_width() + 56, 32)
  box.midbottom = (SCREEN_WIDTH // 2, SCREEN_HEIGHT - 8)
  pygame.draw.rect(surface, banner_color, box.move(0, 3), border_radius=16)
  pygame.draw.rect(surface, WHITE, box, border_radius=16)
  pygame.draw.rect(surface, banner_color, box, width=3, border_radius=16)
  draw_sparkle(surface, box.x + 18, box.centery, 7, banner_color)
  draw_sparkle(surface, box.right - 18, box.centery, 7, banner_color)
  surface.blit(msg, (box.centerx - msg.get_width() // 2, box.centery - msg.get_height() // 2))


def draw_quit_dialog(surface):
  if not quit_dialog:
    return
  surface.blit(dim_overlay, (0, 0))
  r = DIALOG_RECT
  pygame.draw.rect(surface, (0, 0, 0), r.move(0, 5), border_radius=22)   # shadow
  pygame.draw.rect(surface, PAPER, r, border_radius=22)
  pygame.draw.rect(surface, PINK_D, r, width=3, border_radius=22)
  draw_heart(surface, r.x + 34, r.y + 34, 26, PINK_D)
  draw_sparkle(surface, r.right - 30, r.y + 24, 8, BUTTER_D)
  draw_sparkle(surface, r.right - 52, r.y + 40, 5, PINK)
  title = f_dialog.render("Leave Study Buddy?", True, INK)
  surface.blit(title, (r.centerx - title.get_width() // 2 + 10, r.y + 24))
  sub = f_small.render("Your study plan and checkmarks are saved.", True, INK_SOFT)
  surface.blit(sub, (r.centerx - sub.get_width() // 2, r.y + 58))
  draw_button(surface, DIALOG_BUTTONS["stay"], "Stay", (MINT, MINT_D), pressed=pressed_dialog == "stay", font=f_dialog)
  draw_button(surface, DIALOG_BUTTONS["quit"], "Quit", (PINK, PINK_D), pressed=pressed_dialog == "quit", font=f_dialog)


# =====================================================================
# MAIN LOOP
# =====================================================================
last_frame_time = time.monotonic()
while running:
  current_time = time.time()

  # Real seconds since the last frame. Not clamped on purpose: a YOLO check can stall a
  # frame for a while on the Pi and the Pomodoro clock must keep true time.
  now = time.monotonic()
  dt = now - last_frame_time
  last_frame_time = now

  for raw_event in pygame.event.get():
    event = normalize_event(raw_event)
    if event is None:
      continue

    if event.type == pygame.QUIT:
      running = False
      continue

    if event.type == pygame.KEYDOWN and event.key in (pygame.K_ESCAPE, pygame.K_q):
      running = False                       # keyboard shortcut: quit right away
      continue

    # ---- "Leave Study Buddy?" dialog takes over while it is open ----
    if quit_dialog:
      if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
        pressed_dialog = dialog_button_at(event.pos)
      elif event.type == pygame.MOUSEMOTION:
        if pressed_dialog is not None and dialog_button_at(event.pos) != pressed_dialog:
          pressed_dialog = None
      elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
        hit = dialog_button_at(event.pos)
        if pressed_dialog is not None and hit == pressed_dialog:
          if hit == "quit":
            running = False
          else:
            quit_dialog = False
        pressed_dialog = None
      continue

    # ---- to-do list (it has its own Close button) ----
    if todo_panel.handle_event(event):
      continue

    if event.type == pygame.KEYDOWN:        # keyboard still works (e.g. over SSH / USB keyboard)
      if event.key == pygame.K_SPACE:
        toggle_timer()
      elif event.key == pygame.K_r:
        run_action("reset")
      elif event.key == pygame.K_n:
        run_action("skip")
      elif event.key == pygame.K_MINUS or event.key == pygame.K_DOWN:
        character.lose_points(10)

    elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
      last_touch_pos = event.pos
      pressed_button = button_at(event.pos)

    elif event.type == pygame.MOUSEMOTION:
      last_touch_pos = event.pos
      if pressed_button is not None and button_at(event.pos) != pressed_button:
        pressed_button = None               # finger slid off the button: cancel the tap

    elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
      if pressed_button is not None and button_at(event.pos) == pressed_button:
        run_action(pressed_button)          # a tap only counts when you lift your finger on it
      pressed_button = None

  # ==========================================
  # AUTOMATED YOLO PHONE DETECTION CHECK
  # (only runs while a focus session is running)
  # ==========================================
  if (
      character.is_timer_on
      and picam2
      and (current_time - last_vision_check > VISION_CHECK_INTERVAL)
  ):
    last_vision_check = current_time
    try:
      frame = picam2.capture_array()
      if frame.ndim == 3 and frame.shape[2] == 4:
        frame = frame[:, :, :3].copy()       # drop the alpha channel
      results = model(frame, conf=DETECTION_CONFIDENCE, verbose=False)

      detected_items = []
      for r in results:
        for c, conf in zip(r.boxes.cls, r.boxes.conf):
          class_name = model.names[int(c)]
          if class_name in TARGET_CLASSES:
            detected_items.append((class_name, float(conf)))

      vision_status = "saw " + (
          ", ".join(f"{n}" for n, _ in detected_items) or "nothing"
      )

      if any(n == "cell phone" for n, _ in detected_items):
        print("[ALERT] Phone detected! Penalty triggered.")
        character.lose_points(PHONE_PENALTY)
        show_banner(f"Phone spotted!  -{PHONE_PENALTY} points", PINK)
    except Exception as e:
      vision_status = f"error: {e}"
      print(f"[VISION ERROR] {e}")

  finished_phase = timer.update(dt)
  if finished_phase is not None:
    character.stop_focus()
    if finished_phase == FOCUS:
      show_banner("Focus done! Time for a break", MINT, 4.0)
    else:
      show_banner("Break over - tap Start", LAV, 4.0)

  character.update(dt)

  # --- DRAWING LAYERS ---
  screen.blit(room_img, (0, 0))
  timer.draw(screen)
  screen.blit(chair_img, (CHAIR_X, CHAIR_Y))
  character.draw(screen)
  screen.blit(table_img, (TABLE_X, TABLE_Y))

  draw_top_bar(screen)
  draw_status_card(screen)
  draw_banner(screen)

  if SHOW_TOUCH_DEBUG and last_touch_pos is not None:
    pygame.draw.circle(screen, (255, 0, 0), last_touch_pos, 6, 2)

  todo_panel.draw(screen)       # the to-do list sits on top of everything
  draw_quit_dialog(screen)      # ...and the quit question on top of that

  pygame.display.flip()
  clock.tick(FPS)

if picam2:
  picam2.stop()
pygame.quit()
sys.exit()
