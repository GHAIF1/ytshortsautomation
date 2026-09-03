#!/usr/bin/env python3
"""
YouTube Shorts - Game Recording & Upload Automation
====================================================

Adapted from the original video_automation.py (which opened the website in the
default browser and recorded a fixed-length mss/OpenCV video). This version:

  * drives the website with Playwright (visible Chromium, so the screen can
    actually be recorded),
  * enters fullscreen and selects the configured game mode through the UI,
  * records exactly the game-canvas rectangle with FFmpeg (gdigrab),
  * stops the recording the moment the website reports game over,
  * saves a 9:16 MP4 into Desktop/MyVideos and uploads it to YouTube.

Usage
-----
    python video_automation.py                 # run the full workflow
    python video_automation.py --mode team-battle
    python video_automation.py --calibrate     # print the capture rectangle
    python video_automation.py --no-upload     # record only, skip YouTube
"""

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# =========================================================================
#  CONFIGURATION  - everything you normally need to change lives here
# =========================================================================

# --- Website ---------------------------------------------------------------
# Local Live Server while developing. After deploying to GitHub Pages,
# change this to e.g. "https://yourusername.github.io/yourrepo/"
WEBSITE_URL = "http://127.0.0.1:5500/"

# Game mode to play (keys come from GAME_MODES below). You can also override
# per run:  python video_automation.py --mode team-battle
GAME_MODE = "hazard-hunt"

GAME_MODES = {
    "hazard-hunt":     {"button": "#btn-hazard-hunt",     "label": "Hazard Hunt"},
    "meteor-shower":   {"button": "#btn-meteor-shower",   "label": "Meteor Shower"},
    "rotating-field":  {"button": "#btn-rotating-field",  "label": "Rotating Field"},
    "team-battle":     {"button": "#btn-team-battle",     "label": "Team Battle"},
}
# To add a mode later: add a button to index.html and one entry in this dict.
# Nothing else needs to change.

# Stable selectors that already exist in index.html. The game-state ones are
# attributes on #msgOverlay that index.html now maintains.
FULLSCREEN_SELECTOR = "#btn-fullscreen"
GAME_AREA_SELECTOR  = "#canvas"                       # clicking this starts the game
GAME_READY_SELECTOR = '#msgOverlay[data-state="ready"]'
GAME_PLAYING_SELECTOR = '#msgOverlay[data-state="playing"]'
GAME_OVER_SELECTOR  = '#msgOverlay[data-state="game-over"]'
VIDEO_FORMAT        = "shorts"   # "shorts" (9:16) or "fullscreen" (16:9) -> the site's #select-format

# --- Screen recording -------------------------------------------------------
# "auto"   = measure the game canvas rectangle on screen before every run.
#            Recommended: it stays correct if the window/layout changes, and
#            on this site the canvas is pinned to the bottom-left corner, so
#            the video comes out 9:16 with the game exactly filling it.
# "manual" = use the CAPTURE_* values below as-is (the original script's
#            numbers). Run `python video_automation.py --calibrate` to print
#            the numbers for your screen.
CAPTURE_MODE = "auto"

CAPTURE_X = 0         # px from the left screen edge
CAPTURE_Y = 0         # px from the TOP edge. (The original script recorded
                      # from the bottom edge; for that set CAPTURE_Y =
                      # screen height - CAPTURE_HEIGHT, e.g. 1080 - 994 = 86.)
CAPTURE_WIDTH  = 559
CAPTURE_HEIGHT = 994

FPS = 30
MAX_RECORDING_SECONDS = 300   # safety limit ONLY. Normally the game-over
                              # state stops the recording right when the
                              # round ends.

FFMPEG_BINARY = "ffmpeg"      # or a full path, e.g. r"C:\ffmpeg\bin\ffmpeg.exe"

# --- Video files ------------------------------------------------------------
RECORDING_FOLDER = Path.home() / "Desktop" / "MyVideos"   # created automatically

# --- YouTube upload ---------------------------------------------------------
YOUTUBE_UPLOAD_ENABLED = True
# Files from the Google Cloud OAuth setup (see runbook). Keep them in the
# project folder and NEVER commit them to git.
YOUTUBE_CLIENT_SECRET = Path(__file__).resolve().parent / "client_secret.json"
YOUTUBE_TOKEN_FILE    = Path(__file__).resolve().parent / "youtube_token.json"
YOUTUBE_TITLE       = ""            # empty = auto title "<Game label> - <date>"
YOUTUBE_DESCRIPTION = "Recorded and uploaded automatically by my game automation."
YOUTUBE_PRIVACY     = "private"     # "private" | "unlisted" | "public"

# =========================================================================
#  No changes needed below this line
# =========================================================================

STAGE_TOTAL = 9


def say(number, message):
    """Print a step message like [3/9] Entering fullscreen ..."""
    print(f"\n[{number}/{STAGE_TOTAL}] {message}")


def check_ffmpeg():
    """Fail early with a helpful message if FFmpeg is missing."""
    ffmpeg = FFMPEG_BINARY
    if os.path.sep in ffmpeg or "/" in ffmpeg:      # looks like a path
        if Path(ffmpeg).exists():
            return
    elif shutil.which(ffmpeg):
        return
    sys.exit(
        "FFmpeg was not found. Install it, e.g. with:\n"
        "    winget install Gyan.FFmpeg\n"
        "or download a build from https://www.gyan.dev/ffmpeg/builds/ and add\n"
        "it to PATH (or set FFMPEG_BINARY to the full path of ffmpeg.exe)."
    )


def require_playwright():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        sys.exit(
            "Playwright is not installed. Run:\n"
            "    pip install -r requirements.txt\n"
            "    python -m playwright install chromium"
        )


# --------------------------------------------------------------------------
#  FFmpeg screen recorder (Windows gdigrab)
# --------------------------------------------------------------------------

class FFmpegRecorder:
    """Records one desktop rectangle with FFmpeg and stops it cleanly."""

    def __init__(self, rect, fps, out_path):
        width = rect["width"] - (rect["width"] % 2)     # libx264 needs even sizes
        height = rect["height"] - (rect["height"] % 2)
        cmd = [
            FFMPEG_BINARY, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "gdigrab",
            "-framerate", str(fps),
            "-offset_x", str(rect["x"]),
            "-offset_y", str(rect["y"]),
            "-video_size", f"{width}x{height}",
            "-i", "desktop",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(out_path),
        ]
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )

    def start(self):
        """Small warm-up delay, and fail early if ffmpeg died immediately."""
        time.sleep(1.0)
        if self.proc.poll() is not None:
            error = self.proc.stderr.read().decode(errors="replace").strip()
            self.proc = None
            raise RuntimeError(
                "FFmpeg exited immediately - is the capture rectangle valid?\n" + error
            )

    def stop(self):
        """Ask ffmpeg to finalize the MP4, falling back to harder stops."""
        if self.proc is None:
            return
        proc = self.proc
        self.proc = None
        # 1) Polite "q" on stdin (works when ffmpeg is listening on it).
        try:
            proc.stdin.write(b"q")
            proc.stdin.flush()
        except Exception:
            pass
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            # 2) Windows: CTRL_BREAK makes ffmpeg finalize like Ctrl+C.
            if os.name == "nt":
                try:
                    os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # 3) Last resort - file may be cut short.
                    print("  ! ffmpeg did not stop cleanly; forcing termination.")
                    proc.kill()
                    proc.wait()
            else:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        error = proc.stderr.read().decode(errors="replace").strip()
        if proc.returncode not in (0, None) and error:
            print(f"  ! ffmpeg exit code {proc.returncode}: {error}")


# --------------------------------------------------------------------------
#  Capture rectangle
# --------------------------------------------------------------------------

def determine_capture_rect(page):
    """Return the physical-screen rectangle {x, y, width, height} to record.

    "auto" measures the real position/size of the game canvas after the page
    is in fullscreen, so the video matches the game box exactly (bottom-left,
    9:16). Coordinates are multiplied by devicePixelRatio because gdigrab
    counts physical pixels.
    """
    if CAPTURE_MODE == "manual":
        return {
            "x": CAPTURE_X,
            "y": CAPTURE_Y,
            "width": CAPTURE_WIDTH,
            "height": CAPTURE_HEIGHT,
        }

    box = page.locator(GAME_AREA_SELECTOR).bounding_box()
    if not box:
        raise RuntimeError("Could not measure the game canvas on the page.")
    info = page.evaluate(
        "() => ({dpr: window.devicePixelRatio, sx: window.screenX, sy: window.screenY})"
    )
    if info["sx"] != 0 or info["sy"] != 0:
        print(
            "  ! The browser window is not on the primary monitor "
            f"(screenX/Y = {info['sx']}/{info['sy']}). gdigrab records the "
            "primary monitor - drag the window to the main monitor."
        )
    x = max(0, round((info["sx"] + box["x"]) * info["dpr"]))
    y = max(0, round((info["sy"] + box["y"]) * info["dpr"]))
    width = max(2, round(box["width"] * info["dpr"]))
    height = max(2, round(box["height"] * info["dpr"]))
    rect = {
        "x": x,
        "y": y,
        "width": width - (width % 2),
        "height": height - (height % 2),
    }
    print(
        f"  Recording rectangle: x={rect['x']}  y={rect['y']}  "
        f"{rect['width']}x{rect['height']}  (dpr={info['dpr']})"
    )
    return rect


# --------------------------------------------------------------------------
#  Website / browser workflow
# --------------------------------------------------------------------------

def verify_mp4(path):
    """Check the recording exists and has a plausible size."""
    path = Path(path)
    if not path.exists():
        raise RuntimeError("The MP4 was not created.")
    size = path.stat().st_size
    if size == 0:
        raise RuntimeError("The MP4 is empty - the recording failed.")
    if size < 50_000:
        print(f"  ! Warning: the MP4 is only {size} bytes; it may be broken.")
    print(f"Video saved: {path}  ({size / 1_000_000:.1f} MB)")
    return size


def run_game_recording(args):
    """The whole browser flow. Returns a dict with the result."""
    mode_key = args.mode or GAME_MODE
    if mode_key not in GAME_MODES:
        sys.exit(
            f"Unknown game mode '{mode_key}'. Available: {', '.join(GAME_MODES)}"
        )
    label = GAME_MODES[mode_key]["label"]
    url = args.url or WEBSITE_URL

    now = datetime.now()
    recording_folder = Path(RECORDING_FOLDER)
    recording_folder.mkdir(parents=True, exist_ok=True)
    out_path = recording_folder / f"{mode_key}_{now:%Y-%m-%d_%H-%M-%S}.mp4"

    sync_playwright = require_playwright()
    recorder = None
    browser = None
    page = None
    stage = "starting"
    safety_hit = False

    try:
        say(1, f"Opening website: {url}")
        stage = "opening the website"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False)
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_selector(GAME_AREA_SELECTOR, timeout=30000)

            say(2, "Website ready.")
            stage = "entering fullscreen"
            page.click(FULLSCREEN_SELECTOR)
            page.wait_for_function(
                "document.fullscreenElement !== null", timeout=10000
            )
            time.sleep(0.6)          # let the fullscreen layout settle
            say(3, "Entered fullscreen.")

            stage = "configuring the game"
            page.select_option("#select-format", VIDEO_FORMAT)
            say(4, f"Selecting {label} ...")
            page.click(GAME_MODES[mode_key]["button"])
            page.wait_for_selector(GAME_READY_SELECTOR, timeout=10000)
            say(5, "Game ready.")

            stage = "starting the screen recording"
            rect = determine_capture_rect(page)
            recorder = FFmpegRecorder(rect, FPS, out_path)
            recorder.start()          # warms up, so the game start is captured
            say(6, "Screen recording started.")

            stage = "starting gameplay"
            page.click(GAME_AREA_SELECTOR)
            page.wait_for_selector(GAME_PLAYING_SELECTOR, timeout=15000)
            say(7, "Gameplay started.")

            say(8, "Game is running - waiting for game over ...")
            stage = "waiting for game over"
            try:
                page.wait_for_selector(
                    GAME_OVER_SELECTOR, timeout=MAX_RECORDING_SECONDS * 1000
                )
            except Exception:
                safety_hit = True
                print(
                    f"  ! Safety timeout after {MAX_RECORDING_SECONDS}s - game "
                    "over was not detected. Stopping anyway."
                )
            say(8, "Game over detected." if not safety_hit
                else "Stopping because the safety timeout fired.")

            stage = "finalizing the video"
            say(9, "Stopping recording and finalizing video ...")
            recorder.stop()
            recorder = None
            try:
                browser.close()
            except Exception:
                pass
            browser = None
            page = None

        verify_mp4(out_path)
        return {
            "mp4": str(out_path),
            "mode_key": mode_key,
            "label": label,
            "safety_hit": safety_hit,
        }

    except Exception as error:
        # Save a screenshot of the failure if the browser is still open.
        if page is not None:
            try:
                screenshot_dir = Path(__file__).resolve().parent / "screenshots"
                screenshot_dir.mkdir(parents=True, exist_ok=True)
                shot = screenshot_dir / f"failure_{datetime.now():%Y-%m-%d_%H-%M-%S}.png"
                page.screenshot(path=str(shot))
                print(f"\nScreenshot of the failure saved: {shot}")
            except Exception:
                pass
        print(f"\nAutomation FAILED while {stage}:\n  {error}")
        raise
    finally:
        if recorder is not None:
            recorder.stop()
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
#  YouTube upload (official Data API + OAuth)
# --------------------------------------------------------------------------

YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def youtube_credentials():
    """Load or create OAuth credentials. First run opens a consent browser."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    if YOUTUBE_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(
            str(YOUTUBE_TOKEN_FILE), YOUTUBE_SCOPES
        )
        if creds.valid:
            return creds
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            YOUTUBE_TOKEN_FILE.write_text(creds.to_json())
            return creds

    if not YOUTUBE_CLIENT_SECRET.exists():
        sys.exit(
            f"\nclient_secret.json not found at {YOUTUBE_CLIENT_SECRET}\n"
            "The video was recorded and saved, but it was NOT uploaded.\n"
            "To enable uploads, follow the YouTube setup in the runbook:\n"
            "  1. https://console.cloud.google.com -> create a project\n"
            "  2. Enable 'YouTube Data API v3'\n"
            "  3. OAuth consent screen -> External -> add yourself as test user\n"
            "  4. Credentials -> OAuth client ID -> Desktop app\n"
            "  5. Download the JSON and save it as client_secret.json next to\n"
            "     video_automation.py\n"
            "The next run will open a Google consent page once; afterwards the\n"
            "token is cached in youtube_token.json."
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(YOUTUBE_CLIENT_SECRET), YOUTUBE_SCOPES
    )
    creds = flow.run_local_server(port=0)
    YOUTUBE_TOKEN_FILE.write_text(creds.to_json())
    print(f"\nLogged in - token cached at {YOUTUBE_TOKEN_FILE}")
    return creds


def upload_to_youtube(mp4_path, title, description):
    """Upload one MP4 via the resumable API, printing progress every 25%."""
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    creds = youtube_credentials()
    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": "22",                 # "People & Blogs"
        },
        "status": {
            "privacyStatus": YOUTUBE_PRIVACY,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(mp4_path, mimetype="video/mp4", resumable=True)

    print("Starting YouTube upload ...")
    try:
        request = youtube.videos().insert(
            part="snippet,status", body=body, media_body=media
        )
        response = None
        last_percent = 0
        while response is None:
            status, response = request.next_chunk()
            if status:
                percent = int(status.progress() * 100)
                for milestone in (25, 50, 75, 100):
                    if last_percent < milestone <= percent:
                        print(f"Upload: {milestone}%")
                last_percent = percent
    except HttpError as error:
        raise RuntimeError(
            f"YouTube upload failed ({error.resp.status}): {error.reason}"
        ) from error

    video_id = response.get("id")
    print("Upload complete.")
    if video_id:
        print(f"Video: https://youtu.be/{video_id}  (privacy: {YOUTUBE_PRIVACY})")
    return video_id


# --------------------------------------------------------------------------
#  Calibration helper
# --------------------------------------------------------------------------

def calibrate(args):
    """Open the site fullscreen and print the capture rectangle to copy."""
    mode_key = args.mode or GAME_MODE
    if mode_key not in GAME_MODES:
        sys.exit(f"Unknown game mode '{mode_key}'.")
    label = GAME_MODES[mode_key]["label"]
    url = args.url or WEBSITE_URL

    sync_playwright = require_playwright()
    browser = None
    page = None
    try:
        print("\n=== CALIBRATION MODE ===")
        print("Opening the site, entering fullscreen and selecting a mode ...\n")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False)
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_selector(GAME_AREA_SELECTOR, timeout=30000)
            page.click(FULLSCREEN_SELECTOR)
            page.wait_for_function(
                "document.fullscreenElement !== null", timeout=10000
            )
            time.sleep(0.6)
            page.select_option("#select-format", VIDEO_FORMAT)
            page.click(GAME_MODES[mode_key]["button"])
            page.wait_for_selector(GAME_READY_SELECTOR, timeout=10000)
            print(f"Game ready in fullscreen ({label}).")

            box = page.locator(GAME_AREA_SELECTOR).bounding_box()
            info = page.evaluate(
                "() => ({dpr: window.devicePixelRatio, sx: window.screenX, "
                "sy: window.screenY, screenW: screen.width, screenH: screen.height})"
            )
            print(f"\nCanvas element on page : x={box['x']:.0f} y={box['y']:.0f} "
                  f"{box['width']:.0f}x{box['height']:.0f} (CSS px)")
            print(f"Window / monitor        : screenX={info['sx']} screenY={info['sy']} "
                  f"screen={info['screenW']}x{info['screenH']} dpr={info['dpr']}")
            rect = determine_capture_rect(page)
            print("\nIf the canvas is not being captured correctly, you can set")
            print("CAPTURE_MODE = \"manual\" in video_automation.py and use:")
            print(f"""
    CAPTURE_X = {rect['x']}
    CAPTURE_Y = {rect['y']}      # from the TOP edge of the screen
    CAPTURE_WIDTH = {rect['width']}
    CAPTURE_HEIGHT = {rect['height']}
""")
            if info["sx"] != 0 or info["sy"] != 0:
                print("NOTE: the window is not on the primary monitor. Move it there.")
            print("Press Enter to close the browser ...")
            input()
    except Exception as error:
        print(f"\nCalibration FAILED:\n  {error}")
        if page is not None:
            try:
                page.screenshot(
                    path=str(
                        Path(__file__).resolve().parent
                        / f"calibrate_error_{datetime.now():%Y-%m-%d_%H-%M-%S}.png"
                    )
                )
                print("Screenshot saved next to video_automation.py.")
            except Exception:
                pass
        raise
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
#  Entry point
# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Record a game from the website and upload it to YouTube."
    )
    parser.add_argument("--mode", default=None, help="game mode key, e.g. team-battle")
    parser.add_argument("--url", default=None, help="override WEBSITE_URL")
    parser.add_argument("--calibrate", action="store_true",
                        help="print the screen rectangle instead of recording")
    parser.add_argument("--no-upload", action="store_true",
                        help="skip the YouTube upload")
    args = parser.parse_args(argv)

    check_ffmpeg()

    if args.calibrate:
        calibrate(args)
        return

    result = run_game_recording(args)

    if args.no_upload or not YOUTUBE_UPLOAD_ENABLED:
        print("\nSkipping YouTube upload (--no-upload or YOUTUBE_UPLOAD_ENABLED = False).")
        return

    now = datetime.now()
    title = YOUTUBE_TITLE or f"{result['label']} - {now:%Y-%m-%d}"
    description = YOUTUBE_DESCRIPTION
    print(f"\nStarting YouTube upload ...\n  Title   : {title}\n  Privacy : {YOUTUBE_PRIVACY}")
    upload_to_youtube(result["mp4"], title, description)

    if result["safety_hit"]:
        print("\nNOTE: the recording was stopped by the safety timeout - the game may "
              "not have ended normally. Check the video before publishing.")


if __name__ == "__main__":
    main()
