
import hmac
import re
import time
import threading
import requests

from pathlib import Path
from functools import wraps
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_file
from playwright.sync_api import sync_playwright

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

API_KEY = "test123"

playwright_instance = None
browser_instance = None
context_instance = None
page_instance = None

state_lock = threading.RLock()
download_lock = threading.Lock()


def require_api_key(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        supplied = request.headers.get("X-API-Key", "")
        if not hmac.compare_digest(supplied, API_KEY):
            return jsonify({"error": "Invalid or missing API key"}), 401
        return func(*args, **kwargs)
    return wrapper


def get_fresh_cookies():
    """
    Obtain fresh cookies by opening Smolish in a new browser context.
    These are anonymous cookies unless the site provides a login flow.
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/156.0.0.0 Safari/537.36"
                )
            )
            page = context.new_page()
            page.goto(
                "https://smolish.com/",
                wait_until="domcontentloaded",
                timeout=60000
            )
            page.wait_for_timeout(1500)
            return {
                item["name"]: item["value"]
                for item in context.cookies("https://smolish.com/")
            }
        finally:
            browser.close()


def close_browser():
    global playwright_instance, browser_instance
    global context_instance, page_instance

    for obj in (browser_instance, playwright_instance):
        if obj:
            try:
                if obj is browser_instance:
                    obj.close()
                else:
                    obj.stop()
            except Exception:
                pass

    browser_instance = None
    playwright_instance = None
    context_instance = None
    page_instance = None


def get_current_reel():
    if page_instance is None:
        return None

    current_url = page_instance.url

    if re.search(r"/v/\d+", urlparse(current_url).path):
        return current_url

    try:
        candidates = page_instance.evaluate("""
            () => {
                const links = [...document.querySelectorAll('a[href*="/v/"]')];
                const viewportHeight = window.innerHeight;
                const viewportWidth = window.innerWidth;

                return links.map(a => {
                    const r = a.getBoundingClientRect();
                    const visible =
                        r.width > 0 &&
                        r.height > 0 &&
                        r.bottom > 0 &&
                        r.top < viewportHeight &&
                        r.right > 0 &&
                        r.left < viewportWidth;

                    return {
                        href: a.href,
                        visible,
                        centerDistance: Math.abs(
                            (r.top + r.height / 2) - viewportHeight / 2
                        )
                    };
                }).filter(x =>
                    x.visible &&
                    /\\/v\\/\\d+/.test(new URL(x.href).pathname)
                ).sort((a, b) =>
                    a.centerDistance - b.centerDistance
                );
            }
        """)

        for candidate in candidates:
            parsed = urlparse(candidate["href"])
            if parsed.hostname == "smolish.com" or parsed.hostname == "www.smolish.com":
                return candidate["href"]

    except Exception:
        pass

    return None


@app.get("/")
def index():
    return jsonify({
        "name": "Smolish Feed API",
        "endpoints": [
            "GET /api/health",
            "POST /api/feed/start",
            "POST /api/feed/next",
            "POST /api/feed/close",
            "POST /api/download",
            "GET /api/files/<video_id>"
        ]
    })


@app.get("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "feed_started": page_instance is not None
    })


@app.post("/api/feed/start")
@require_api_key
def start_feed():
    global playwright_instance, browser_instance
    global context_instance, page_instance

    with state_lock:
        try:
            close_browser()

            playwright_instance = sync_playwright().start()
            browser_instance = playwright_instance.chromium.launch(
                headless=True
            )

            context_instance = browser_instance.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/156.0.0.0 Safari/537.36"
                )
            )

            page_instance = context_instance.new_page()
            page_instance.goto(
                "https://smolish.com/",
                wait_until="domcontentloaded",
                timeout=60000
            )
            page_instance.wait_for_timeout(2500)

            reel_url = get_current_reel()

            return jsonify({
                "status": "success",
                "feed_started": True,
                "reel_url": reel_url,
                "message": (
                    "Feed started"
                    if reel_url
                    else "Feed opened, but no reel link was detected"
                )
            })

        except Exception as exc:
            close_browser()
            return jsonify({
                "error": "Could not start feed",
                "details": str(exc)
            }), 500


@app.post("/api/feed/next")
@require_api_key
def next_feed():
    with state_lock:
        if page_instance is None:
            return jsonify({
                "error": "Feed not started. Call /api/feed/start first."
            }), 400

        try:
            previous_url = get_current_reel()

            page_instance.keyboard.press("ArrowDown")
            page_instance.mouse.wheel(
                0, max(500, page_instance.evaluate("window.innerHeight * 0.85"))
            )

            deadline = time.monotonic() + 12
            reel_url = None

            while time.monotonic() < deadline:
                page_instance.wait_for_timeout(400)
                candidate = get_current_reel()

                if candidate and candidate != previous_url:
                    reel_url = candidate
                    break

            if reel_url is None:
                reel_url = get_current_reel()

            return jsonify({
                "status": "success" if reel_url else "not_found",
                "previous_reel_url": previous_url,
                "reel_url": reel_url,
                "changed": bool(
                    reel_url and reel_url != previous_url
                )
            })

        except Exception as exc:
            return jsonify({
                "error": "Could not scroll feed",
                "details": str(exc)
            }), 500


@app.post("/api/feed/close")
@require_api_key
def close_feed_endpoint():
    with state_lock:
        close_browser()

    return jsonify({
        "status": "success",
        "message": "Feed session closed"
    })


@app.post("/api/download")
@require_api_key
def download_video():
    data = request.get_json(silent=True) or {}
    video_url = data.get("url", "")

    if not isinstance(video_url, str):
        return jsonify({"error": "url must be a string"}), 400

    parsed = urlparse(video_url)
    match = re.fullmatch(r"/v/(\\d+)/?", parsed.path)

    if (
        parsed.scheme != "https"
        or parsed.hostname not in ("smolish.com", "www.smolish.com")
        or not match
    ):
        return jsonify({
            "error": "Provide a valid https://smolish.com/v/VIDEO_ID URL"
        }), 400

    video_id = match.group(1)
    output_file = DOWNLOAD_DIR / f"{video_id}.mp4"

    if output_file.exists() and output_file.stat().st_size > 0:
        return jsonify({
            "status": "success",
            "video_id": video_id,
            "download_url": request.host_url.rstrip("/")
                + f"/api/files/{video_id}",
            "cached": True
        })

    try:
        # Prefer the current feed browser's fresh cookies, if available.
        # Otherwise create a new browser context and obtain fresh site cookies.
        with state_lock:
            if context_instance is not None:
                cookies = {
                    item["name"]: item["value"]
                    for item in context_instance.cookies("https://smolish.com/")
                }
            else:
                cookies = get_fresh_cookies()

        headers = {
            "Accept": "*/*",
            "Referer": video_url,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/156.0.0.0 Safari/537.36"
            )
        }

        api_url = (
            f"https://smolish.com/api/videos/{video_id}/download"
            "?watermark=1"
        )

        with download_lock:
            with requests.Session() as session:
                session.headers.update(headers)
                session.cookies.update(cookies)

                with session.get(
                    api_url,
                    stream=True,
                    allow_redirects=True,
                    timeout=(15, 120)
                ) as response:
                    if response.status_code != 200:
                        return jsonify({
                            "error": "Upstream download rejected",
                            "status_code": response.status_code
                        }), 502

                    content_type = response.headers.get(
                        "Content-Type", ""
                    ).lower()

                    if (
                        "text/html" in content_type
                        or "application/json" in content_type
                    ):
                        return jsonify({
                            "error": (
                                "Upstream returned a non-video response. "
                                "Fresh anonymous cookies may not be sufficient "
                                "if Smolish requires an authenticated session."
                            )
                        }), 502

                    temp_file = DOWNLOAD_DIR / f"{video_id}.part"

                    try:
                        with temp_file.open("wb") as output:
                            for chunk in response.iter_content(
                                chunk_size=128 * 1024
                            ):
                                if chunk:
                                    output.write(chunk)

                        if temp_file.stat().st_size == 0:
                            return jsonify({
                                "error": "Downloaded file is empty"
                            }), 502

                        temp_file.replace(output_file)

                    finally:
                        if temp_file.exists():
                            temp_file.unlink()

        return jsonify({
            "status": "success",
            "video_id": video_id,
            "download_url": request.host_url.rstrip("/")
                + f"/api/files/{video_id}",
            "cached": False
        })

    except requests.RequestException as exc:
        return jsonify({
            "error": "Download request failed",
            "details": str(exc)
        }), 502

    except Exception as exc:
        return jsonify({
            "error": "Download failed",
            "details": str(exc)
        }), 500


@app.get("/api/files/<video_id>")
@require_api_key
def get_downloaded_file(video_id):
    if not re.fullmatch(r"\d+", video_id):
        return jsonify({"error": "Invalid video ID"}), 400

    video_file = DOWNLOAD_DIR / f"{video_id}.mp4"

    if not video_file.is_file():
        return jsonify({
            "error": "File not found. Download the reel first."
        }), 404

    return send_file(
        video_file,
        mimetype="video/mp4",
        as_attachment=True,
        download_name=f"{video_id}.mp4",
        conditional=True
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(__import__("os").environ.get("PORT", 5000)),
        debug=False
    )
