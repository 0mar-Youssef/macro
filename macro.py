import cv2
import numpy as np
import pytesseract
import pyautogui
import mss
import time
import sys
import os
import re
import tempfile
from PIL import Image

# Windows ships no Tesseract on PATH — point pytesseract at the first install
# location we find so OCR works out of the box.  (No-op on macOS/Linux where
# it's already on PATH.)  The UB-Mannheim installer drops it in Program Files by
# default, but a per-user install lands in %LOCALAPPDATA%\Programs instead.
if sys.platform == "win32":
    for _p in (
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
    ):
        if os.path.exists(_p):
            pytesseract.pytesseract.tesseract_cmd = _p
            break

DEBUG = "--debug" in sys.argv

# pyautogui ships with a hidden 0.1s nap after EVERY mouse command (its safety
# brake).  Two commands per click = ~0.2s of tax per click we never asked for.
# The corner-slam emergency abort (FAILSAFE) still works.
pyautogui.PAUSE = 0

# Roblox uses raw input and silently DROPS pyautogui's standard SendInput
# clicks.  On Windows we drive the mouse/keyboard with pydirectinput instead,
# which sends the lower-level (scancode/absolute) input the game accepts.
# pydirectinput is Windows-only, so everywhere else — and if it isn't installed
# — we fall back to pyautogui.
_input = pyautogui
if sys.platform == "win32":
    try:
        import pydirectinput
        pydirectinput.PAUSE = 0
        pydirectinput.FAILSAFE = False
        _input = pydirectinput
    except ImportError:
        print("[!] pydirectinput not installed — clicks may not register in "
              "Roblox.  Run:  python -m pip install pydirectinput")

# ─── CONFIG ───────────────────────────────────────────────────────────────────

CASH_TRIGGER  = 133.0
DROP_MAX_WAIT = 9.0     # max seconds to wait for the drop modal
DROP_MIN_WAIT = 4.0     # sell never shows before ~4.7s — pure sleep until here
POLL_INTERVAL = 0.1     # fast polls in the catch window -> near-instant sell
SELL_WAIT     = 0.1
LEAVE_CHECK_AT = 6.0    # sell normally shows by ~4.8s; if it's not there by
                        # now, check whether we got kicked (Leave dialog up)
AFK_EVERY      = 5      # watch for the AFK popup every Nth round (during the
                        # spin's dead time; other rounds do zero AFK work)

# The 1% tab sits at a FIXED spot (logical px).  Set once measured; OCR keeps
# misreading it, so a hardcoded click is the reliable path.  None -> try OCR.
ONE_PERCENT_TAB = (413, 177)

# Leaf Case grid slot — fixed once the 1% tab is open (logical px).  Template
# matching it was unreliable (conf ~0.30), so we click the known slot instead.
LEAF_CASE_POS = (1033, 286)

VIP_TEMPLATE     = "templates/vip_free_case.png"
LEAF_TEMPLATE    = "templates/leaf_case.png"
CONFIRM_TEMPLATE = "templates/confirm_check.png"
LEAVE_TEMPLATE   = "templates/leave_button.png"   # Roblox "Disconnected" dialog

# Single rejoin click — when the kick dialog is detected, click this coordinate
# (the rejoin/play button that appears in that state).
KICK_REJOIN_POS  = (1052, 616)
REJOIN_LOAD_WAIT = 7.0    # click -> game fully loaded back in (max ~7s)

# ── Watchdog: break out of a stuck modal / reward overlay ──────────────────────
# If N rounds in a row find no sell button, the screen is stuck on some overlay
# that nothing else (AFK / disconnect) detects.  Press Escape + click a neutral
# spot to dismiss it, then carry on.
STUCK_LIMIT     = 4              # consecutive sell-failures before a reset
NEUTRAL_CLICK   = (960, 927)     # empty board area, safe to click (logical px)
# Cash sanity.  Between Leaf resets the cash ONLY climbs, in small steps (~$2-3
# per VIP sell).  Every OCR error seen live is a dropped/extra decimal point —
# a power-of-10 mistake ('$1152' for $115.2, '$131142' for $131.14).  So we use
# the last good value to (a) place the decimal correctly and (b) reject readings
# that fall outside the plausible climb window.
CASH_UP_TOL    = 60.0    # most a single round can add
CASH_DOWN_TOL  = 25.0    # cash never drops mid-climb; a dip == misread
CASH_BAD_RESET = 3       # this many rejects in a row => the baseline is the bad
                         # value; drop it and re-lock onto the next reading
# FAILSAFE: if the screen reads >= trigger this many rounds in a row, BUY — no
# matter what the baseline thinks.  A real high is sustained; a phantom isn't.
# This is the hard guarantee that "I had $160 and it didn't buy" can't happen.
HIGH_CONFIRM   = 3

# Cash is read at the TOP of the round (the bar is dimmed during the spin, so
# mid-spin OCR reads garbage).  Far from the trigger we only bother every Nth
# round; once the last reading is within CASH_NEAR of the trigger we read every
# round so the Leaf buy is never late.
CASH_EVERY = 10          # read cash every Nth round while far from the trigger
CASH_NEAR  = 6.0         # within this of the trigger -> read every round
                         # (133 - 6 = $127: at 127+ we watch every round)

# Physical screen 1920x1080 | Logical 1920x1080 | Scale 1x (Windows 100% DPI)
# REGION constants are PHYSICAL pixels.  Clicks are LOGICAL pixels.
# At 1x DPI physical == logical.
PHYS_W, PHYS_H = 1920, 1080

# Tightened to JUST the "$47.86" cash number top-right (between the green cube
# icon and the (+) button).  Measured on a real 1920x1080 capture — sits a touch
# left of the (+) so OCR never sweeps it up, and right of the cube/gems so it
# can't grab the wrong currency.
CASH_REGION_PHYS = (1695,  10, 1815,  56)
TAB_REGION_PHYS  = (   0, 138, 1347, 174)
VIP_REGION_PHYS  = (   0, 181,  786, 400)
# The "Live Feed" panel runs down the RIGHT edge (physical x > ~1660) and its
# entries have bright-green backgrounds that the sell/confirm detectors would
# otherwise lock onto.  Crop it off for both the green-blob sell search and the
# AFK-checkmark template search.
LIVE_FEED_X         = 1660
SELL_REGION_PHYS    = (0, 0, LIVE_FEED_X, 1080)
CONFIRM_REGION_PHYS = (0, 0, LIVE_FEED_X, 1080)

# Green of the "Sell for X$" pill — calibrated from a real reward-modal capture.
# The KEY discriminator is SATURATION: the pill is vivid (S~190-210) while the
# pastel card background behind it is muted (S~140-150).  An S floor of 170 drops
# the whole card out of the mask so only the button survives.  (Hue also helps:
# pill H~59-61, card H~68-71 — the H ceiling of 67 trims the card too.)
SELL_GREEN_LO = np.array([48, 170,  70])
SELL_GREEN_HI = np.array([67, 255, 255])

# ─── FAST SCREENSHOT (mss) ────────────────────────────────────────────────────
# mss grabs the framebuffer directly (no subprocess / temp file).  We normalise
# every frame to PHYS_W x PHYS_H so all the physical coordinate math is valid.

_sct      = mss.mss()
_monitor  = _sct.monitors[1]
_frame    = {'arr': None, 'ts': 0.0}

def grab(phys_region=None, max_age=0.15):
    """Return a physical-resolution (1920x1080) RGB numpy array, cached briefly."""
    now = time.time()
    if _frame['arr'] is None or now - _frame['ts'] > max_age:
        raw = np.asarray(_sct.grab(_monitor))          # BGRA
        rgb = np.ascontiguousarray(raw[:, :, 2::-1])    # BGRA -> RGB
        if rgb.shape[1] != PHYS_W or rgb.shape[0] != PHYS_H:
            rgb = cv2.resize(rgb, (PHYS_W, PHYS_H), interpolation=cv2.INTER_AREA)
        _frame['arr'] = rgb
        _frame['ts']  = now
    arr = _frame['arr']
    if phys_region:
        l, t, r, b = phys_region
        return arr[t:b, l:r]
    return arr

def _invalidate():
    _frame['arr'] = None

# ─── CLICK ────────────────────────────────────────────────────────────────────

def click(lx, ly):
    # Roblox ignores an instantaneous (0 ms) synthetic click — press and release
    # in the same instant never registers.  Move into place, let the cursor
    # settle, then HOLD the button down briefly before releasing so the game
    # sees a real press.
    # Roblox GUI buttons only "arm" once they see the cursor MOVING over them,
    # and pydirectinput.moveTo teleports (no intermediate move events) — so a
    # straight move-then-click can land on a button that never registered the
    # hover.  Wiggle a few px on the target first to emit real move events, then
    # hold-click.
    _input.moveTo(lx, ly)
    _input.moveTo(lx - 4, ly - 4)
    _input.moveTo(lx + 4, ly + 4)
    _input.moveTo(lx, ly)
    time.sleep(0.05)
    _input.mouseDown()
    time.sleep(0.09)
    _input.mouseUp()
    _invalidate()
    time.sleep(0.08)

# ─── TEMPLATE MATCH (few scales) ──────────────────────────────────────────────

def find_template(template_path, phys_region=None, confidence=0.60,
                  scales=(1.0, 0.9, 1.1), label=None):
    screen = grab(phys_region)
    screen_bgr = cv2.cvtColor(screen, cv2.COLOR_RGB2BGR)
    sh, sw = screen_bgr.shape[:2]

    base = cv2.cvtColor(np.array(Image.open(template_path).convert("RGB")),
                        cv2.COLOR_RGB2BGR)
    bh, bw = base.shape[:2]
    best_val, best_loc, best_tw, best_th = -1, None, bw, bh

    for s in scales:
        tw, th = int(bw * s), int(bh * s)
        if tw > sw or th > sh or tw < 8 or th < 8:
            continue
        tmpl = cv2.resize(base, (tw, th)) if s != 1.0 else base
        res  = cv2.matchTemplate(screen_bgr, tmpl, cv2.TM_CCOEFF_NORMED)
        _, val, _, loc = cv2.minMaxLoc(res)
        if val > best_val:
            best_val, best_loc, best_tw, best_th = val, loc, tw, th

    name = label or template_path.split('/')[-1]
    if DEBUG:
        print(f"  [debug] {name}: conf={best_val:.3f}")

    if best_val >= confidence and best_loc is not None:
        px = best_loc[0] + best_tw // 2
        py = best_loc[1] + best_th // 2
        if phys_region:
            px += phys_region[0]
            py += phys_region[1]
        return (px, py)
    return None

def find_and_click(template_path, label, phys_region=None,
                   retries=6, wait=0.35, conf=0.55, scales=(1.0, 0.9, 1.1)):
    for i in range(retries):
        pos = find_template(template_path, phys_region, conf, scales, label)
        if pos:
            print(f"  [✓] {label} at {pos}")
            click(*pos)
            return True
        check_for_confirm()          # target missing? a popup may be covering it
        time.sleep(wait)
    print(f"  [✗] {label} not found after {retries} tries")
    return False

# ─── SELL BUTTON (bright-green blob) ──────────────────────────────────────────

def find_sell_button(exclude=None):
    """
    Isolate the Sell button by its bright saturated green and pick the
    button-shaped blob in the centre of the screen.  Price text is irrelevant.
    exclude: logical (x, y) to stay away from — used while the AFK popup is up
    so its green checkmark can't be mistaken for the Sell button.
    """
    screen = grab(SELL_REGION_PHYS)          # crops the right-edge Live Feed
    sh, sw = screen.shape[:2]
    hsv  = cv2.cvtColor(screen, cv2.COLOR_RGB2HSV)
    mask = cv2.inRange(hsv, SELL_GREEN_LO, SELL_GREEN_HI)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))

    if DEBUG:
        cv2.imwrite(os.path.join(tempfile.gettempdir(), "debug_sell_mask.png"), mask)

    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best, best_area = None, 0
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        if h == 0:
            continue
        area, aspect = w * h, w / h
        cx, cy = x + w // 2, y + h // 2
        if area < 3_500:                  continue   # too small
        if not (1.8 < aspect < 4.3):      continue   # button is wide
        if cy < sh * 0.30:                continue   # skip top cash/gems bar
        if not (sw * 0.08 < cx < sw*0.92):continue
        if exclude and abs(cx - exclude[0]) < 130 \
                   and abs(cy - exclude[1]) < 130:
            continue                                 # that's the AFK checkmark
        if area > best_area:
            best_area, best = area, (cx, cy)

    if best:
        return (best[0], best[1])
    return None

# ─── CASH OCR ─────────────────────────────────────────────────────────────────

def _snap_decimal(digits, hint):
    """OCR dropped the decimal point ('1152').  Re-insert it at the position
    that lands closest to `hint` (the last good cash).  The true value is always
    digits / 10^k for some k, and because cash moves slowly the right reading
    sits right next to the previous one (1152 -> 115.2, next to 112.58)."""
    n = float(int(digits))
    best, bestdiff = n, abs(n - hint)
    f = n
    for _ in range(len(digits)):                  # try /10, /100, /1000 ...
        f /= 10.0
        d = abs(f - hint)
        if d < bestdiff:
            best, bestdiff = f, d
    return round(best, 2)

def _parse_cash(txt, hint=None):
    """Extract the real dollar value from noisy OCR text.

    The cash display ALWAYS shows cents (e.g. $12.94).  Failure modes seen live:
      '7 $12.94' -> junk prefix     -> trust the token that has a decimal
      '$23.038'  -> extra digit     -> clamp to 2 decimal places
      '$1152'    -> dropped decimal -> place it using `hint` (-> 115.2, not 11.52)
    When no hint is available (startup / just after a Leaf reset) we fall back to
    assuming the last two digits are the cents.
    """
    if '$' in txt:                                # the $ anchors the real number;
        txt = txt.rsplit('$', 1)[-1]              # drop any icon-junk before it
    toks = re.findall(r'\d+(?:\.\d+)?', txt)
    if not toks:
        return None
    dotted = [t for t in toks if '.' in t]
    if dotted:                                    # a decimal point survived -> trust it
        best = max(dotted, key=len)
        intp, frac = best.split('.')
        return round(float(f"{intp}.{frac[:2]}"), 2)
    best = max(toks, key=len)                     # no decimal at all -> OCR dropped it
    if hint is not None:                          # place it next to the last value
        return _snap_decimal(best, hint)
    if len(best) >= 3:                            # no hint -> assume last 2 = cents
        return float(f"{best[:-2]}.{best[-2:]}")
    return float(best)

def read_cash(hint=None):
    img  = grab(CASH_REGION_PHYS)
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    gray = cv2.resize(gray, None, fx=2.0, fy=2.0,        # upscale: recovers the
                      interpolation=cv2.INTER_CUBIC)      # small decimal point
    _, thr = cv2.threshold(gray, 140, 255, cv2.THRESH_BINARY)
    pil = Image.fromarray(thr)
    if DEBUG:
        pil.save(os.path.join(tempfile.gettempdir(), "debug_cash_thresh.png"))
    txt = pytesseract.image_to_string(
        pil, config="--psm 7 -c tessedit_char_whitelist=0123456789.$").strip()
    if DEBUG:
        print(f"  [debug] cash OCR raw: {txt!r}")
    return _parse_cash(txt, hint)

# ─── CONFIRM POPUP ────────────────────────────────────────────────────────────

def find_confirm():
    """Detect the AFK popup's green checkmark WITHOUT clicking it.  Clicks on
    the popup are IGNORED by the game while a case animation is playing, so
    mid-spin we only want to know it's there — the click comes after the sell."""
    return find_template(CONFIRM_TEMPLATE, phys_region=CONFIRM_REGION_PHYS,
                         confidence=0.72, scales=(0.7, 0.85, 1.0, 1.15),
                         label="confirm")

def check_for_confirm():
    pos = find_confirm()
    if pos:
        print(f"  [!] AFK confirm — clicking {pos}")
        click(*pos)
        time.sleep(0.4)
        check_for_confirm()              # popups can stack — clear any second one
        return True
    return False

# ─── DISCONNECT / REJOIN ──────────────────────────────────────────────────────

def check_for_disconnect():
    """If the Roblox kick dialog is up, click the rejoin button directly."""
    try:
        pos = find_template(LEAVE_TEMPLATE, confidence=0.90,
                            scales=(1.0, 0.92, 1.08), label="leave")
    except Exception:
        return False                     # template not captured yet
    if pos:
        print(f"  [⚠] DISCONNECTED — clicking rejoin at {KICK_REJOIN_POS}")
        click(*KICK_REJOIN_POS)
        print(f"      loading… ({REJOIN_LOAD_WAIT:.0f}s)")
        time.sleep(REJOIN_LOAD_WAIT)
        return True
    return False

# ─── 1% TAB ───────────────────────────────────────────────────────────────────

def click_one_percent_tab():
    if ONE_PERCENT_TAB:
        print(f"  [✓] 1% tab (fixed) at {ONE_PERCENT_TAB}")
        click(*ONE_PERCENT_TAB)
        return True
    img  = grab(TAB_REGION_PHYS)
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    _, thr = cv2.threshold(gray, 80, 255, cv2.THRESH_BINARY_INV)
    data = pytesseract.image_to_data(Image.fromarray(thr),
                                     output_type=pytesseract.Output.DICT)
    for i, t in enumerate(data['text']):
        if '1%' in t and int(data['conf'][i]) > 20:
            px = data['left'][i] + data['width'][i]  // 2
            py = data['top'][i]  + data['height'][i] // 2
            lx = TAB_REGION_PHYS[0] + px
            ly = TAB_REGION_PHYS[1] + py
            print(f"  [✓] 1% tab at ({lx}, {ly})")
            click(lx, ly)
            return True
    print("  [!] 1% tab OCR failed, using fallback")
    click(194, 174)
    return False

# ─── WAIT FOR DROP + SELL ─────────────────────────────────────────────────────

def wait_for_drop_and_sell(check_afk=False):
    """Poll for the sell button.  LEAN: most rounds do nothing but sleep
    through the spin's dead time (sell never shows before ~4.7s), then poll
    fast so the sell is clicked the instant it appears.

    check_afk: on scheduled rounds, watch for the AFK popup during the dead
    time — DETECT only (the game ignores clicks on it mid-animation); the
    checkmark gets clicked right after the sell.  Returns True when sold."""
    start, poll = time.time(), 0
    afk_pos = None
    leave_checked = False
    while time.time() - start < DROP_MAX_WAIT:
        time.sleep(POLL_INTERVAL)
        poll += 1
        elapsed = time.time() - start
        if elapsed < DROP_MIN_WAIT:
            # dead time — only the scheduled AFK watch does anything here
            if check_afk and afk_pos is None and poll % 12 == 1:
                afk_pos = find_confirm()
                if afk_pos:
                    print(f"  [!] AFK popup at {afk_pos} — clearing AFTER sell")
            continue
        if not leave_checked and elapsed >= LEAVE_CHECK_AT:
            leave_checked = True                  # sell is late — were we kicked?
            if check_for_disconnect():
                return False                      # rejoin already done inside
        pos = find_sell_button(exclude=afk_pos)
        if pos:
            # The button pops in with an animation; the first detection can
            # catch it half-rendered (center reads a hair high -> the click
            # grazes the top edge).  Give it a beat to settle, re-center, click.
            time.sleep(0.15)
            _invalidate()
            pos = find_sell_button(exclude=afk_pos) or pos
            print(f"  [$] Sell at {pos}  (after {elapsed:.1f}s)")
            click(*pos)
            time.sleep(SELL_WAIT)
            if afk_pos:                  # sale done — NOW the checkmark works
                check_for_confirm()
            return True
    print(f"  [✗] No sell button after {DROP_MAX_WAIT:.0f}s")
    if afk_pos:                          # clear it even when the sell missed
        check_for_confirm()
    return False

# ─── MAIN ACTIONS ─────────────────────────────────────────────────────────────

def recover():
    """Last-resort unstick: an overlay is up that no detector recognises.
    Escape closes most Roblox modals; a neutral click + tab re-click resets
    focus.  Cheap and safe to run on a clean screen too."""
    print("  [⟳] WATCHDOG — screen looks stuck, forcing a reset")
    for _ in range(2):
        _input.press("esc")
        time.sleep(0.25)
    _invalidate()
    click(*NEUTRAL_CLICK)
    time.sleep(0.4)

def open_and_sell_vip(check_afk=False):
    print("[→] VIP Free Case...")
    if not find_and_click(VIP_TEMPLATE, "VIP Free Case",
                          phys_region=VIP_REGION_PHYS, conf=0.55):
        check_for_disconnect()       # kicked?  the dialog hides the VIP button
        return False
    return wait_for_drop_and_sell(check_afk)

def open_leaf_case_once():
    print("\n[★] $133 — Leaf Case")
    click_one_percent_tab()
    time.sleep(1.0)
    print(f"  [✓] Leaf Case (fixed) at {LEAF_CASE_POS}")
    click(*LEAF_CASE_POS)
    sold = wait_for_drop_and_sell()
    print("[→] Back to VIP tab...")
    click_one_percent_tab()
    time.sleep(0.6)
    return sold

# ─── ENTRY ────────────────────────────────────────────────────────────────────

def main():
    print("=" * 50)
    print("  Starting in 5s — switch to Roblox")
    print("  Ctrl+C to stop" + ("   [DEBUG]" if DEBUG else ""))
    print("=" * 50)
    time.sleep(5)

    leaf_opened, rnd = False, 0
    fails    = 0          # consecutive rounds with no sell button (stuck detector)
    last_cash = None      # last sane reading; anchors decimal placement + window
    bad_streak = 0        # consecutive out-of-window cash reads
    high_streak = 0       # consecutive reads that showed >= trigger
    since_read = 999      # rounds since last accepted cash read (999 = read now)
    while True:
        rnd += 1
        print(f"\n[Round {rnd}]")

        # (Lean loop: no per-round scans.  AFK is watched during the spin every
        #  AFK_EVERY-th round + on VIP-click misses; disconnect is checked only
        #  when the sell is late or the VIP button is missing.)

        # ── WATCHDOG: too many sell-misses in a row -> overlay is stuck. ──
        if fails >= STUCK_LIMIT:
            recover()
            fails = 0
            continue

        # ── Scheduled cash read (round top — the bar is dimmed during the
        #    spin, so mid-spin reads don't work).  Every CASH_EVERY rounds
        #    while far from the trigger; EVERY round once we're at $127+. ──
        near = last_cash is not None and last_cash >= CASH_TRIGGER - CASH_NEAR
        if last_cash is None or since_read >= CASH_EVERY or near:
            cash = read_cash(last_cash)
            if cash is not None:
                print(f"  [💰] ${cash:.2f}")
                # Cash only climbs (small steps) between Leaf resets.  A read
                # outside that window is an OCR glitch: don't move the baseline.
                # CASH_BAD_RESET rejects in a row -> the baseline IS the bad
                # value; drop it and re-lock onto the next reading.
                in_window = last_cash is None or (
                    last_cash - CASH_DOWN_TOL <= cash <= last_cash + CASH_UP_TOL)
                if in_window:
                    last_cash, bad_streak, since_read = cash, 0, 0
                else:
                    print(f"  [skip] ${cash:.2f} out of range vs last ${last_cash:.2f}")
                    bad_streak += 1
                    if bad_streak >= CASH_BAD_RESET:
                        print("  [cash] baseline looks wrong — re-locking")
                        last_cash, bad_streak = None, 0
                # Failsafe streak: consecutive reads showing enough money,
                # INDEPENDENT of the baseline — the "must buy" guarantee.
                high_streak = high_streak + 1 if cash >= CASH_TRIGGER else 0
                if cash < CASH_TRIGGER:
                    leaf_opened = False           # dropped below -> re-arm
            else:
                print("  [?] cash unreadable")
        since_read += 1

        # ── LEAF: buy when the baseline says we're there (fresh re-read or a
        #    sustained high streak confirms before spending). ──
        if last_cash is not None and last_cash >= CASH_TRIGGER and not leaf_opened:
            _invalidate()
            now = read_cash(last_cash)            # fresh confirm before spending
            if (now is not None and now >= CASH_TRIGGER) \
                    or high_streak >= HIGH_CONFIRM:
                print(f"  [★] ${last_cash:.2f} ≥ ${CASH_TRIGGER:.0f} — buying Leaf")
                fails = 0 if open_leaf_case_once() else fails + 1
                leaf_opened = True
                last_cash, bad_streak, high_streak, since_read = None, 0, 0, 999
                continue
            reread = f"${now:.2f}" if now is not None else "failed"
            print(f"  [skip] leaf not confirmed (re-read {reread})")
            bad_streak += 1
            if bad_streak >= CASH_BAD_RESET:
                print("  [cash] baseline looks wrong — re-locking")
                last_cash, bad_streak = None, 0

        # ── VIP round.  AFK popup watched during the spin every Nth round. ──
        if open_and_sell_vip(check_afk=(rnd % AFK_EVERY == 0)):
            fails = 0
        else:
            fails += 1
            print(f"  [watchdog] sell-fail streak: {fails}/{STUCK_LIMIT}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[✋] Stopped")
        sys.exit(0)
