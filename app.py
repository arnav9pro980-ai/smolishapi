
import re
import threading
from urllib.parse import urlparse

import requests
from flask import Flask, Response, jsonify, request, stream_with_context
from playwright.sync_api import sync_playwright

app = Flask(__name__)

API_KEY = "test123"
BASE_URL = "https://smolish.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/156.0.0.0 Safari/537.36"
)

lock = threading.RLock()
playwright = None
browser = None
context = None
page = None


def authorized():
    supplied = request.headers.get("X-API-Key", "")
    return supplied == API_KEY


@app.before_request
def check_api_key():
    if request.path == "/api/health":
        return None

    if not authorized():
        return jsonify({"error": "Unauthorized"}), 401


def ensure_browser():
    global playwright, browser, context, page

    if browser is not None:
        try:
            if browser.is_connected() and page and not page.is_closed():
                return
        except Exception:
            pass

    close_browser()

    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)

    context = browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 900},
    )

    page = context.new_page()
    page.goto(
        BASE_URL + "/",
        wait_until="domcontentloaded",
        timeout=60000,
    )
    page.wait_for_timeout(2000)


def close_browser():
    global playwright, browser, context, page

    old_page = page
    old_context = context
    old_browser = browser
    old_playwright = playwright

    page = None
    context = None
    browser = None
    playwright = None

    for obj, method in (
        (old_page, "close"),
        (old_context, "close"),
        (old_browser, "close"),
        (old_playwright, "stop"),
    ):
        if obj is not None:
            try:
                getattr(obj, method)()
            except Exception:
                pass


def get_live_cookies():
    ensure_browser()

    # Read the latest cookies directly from the running browser context.
    cookies = context.cookies([BASE_URL])

    return {
        cookie["name"]: cookie["value"]
        for cookie in cookies
    }


def video_id_from_url(url):
    if not url:
        return None

    parsed = urlparse(url)

    if parsed.hostname not in ("smolish.com", "www.smolish.com"):
        return None

    match = re.search(r"/v/(\d+)(?:/|$)", parsed.path)
    return match.group(1) if match else None


def current_reel():
    if page is None:
        return None

    video_id = video_id_from_url(page.url)

    if video_id:
        return f"{BASE_URL}/v/{video_id}"

    try:
        links = page.locator('a[href*="/v/"]').evaluate_all(
            """els => els.map(el => {
                const r = el.getBoundingClientRect();
                return {
                    href: el.href,
                    x: r.x + r.width / 2,
                    y: r.y + r.height / 2,
                    visible: r.width > 0 && r.height > 0
                };
            }).filter(el => el.visible)
            """
        )

        viewport = page.evaluate(
            "() => ({w: innerWidth, h: innerHeight})"
        )

        candidates = []

        for item in links:
            vid = video_id_from_url(item["href"])

            if vid and 0 <= item["y"] <= viewport["h"]:
                distance = (
                    abs(item["x"] - viewport["w"] / 2)
                    + abs(item["y"] - viewport["h"] / 2)
                )
                candidates.append((distance, vid))

        if candidates:
            candidates.sort()
            return f"{BASE_URL}/v/{candidates[0][1]}"

    except Exception as exc:
        app.logger.warning("Reel detection failed: %s", exc)

    return None


def live_session():
    cookies = get_live_cookies()

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Referer": BASE_URL + "/",
    })
    session.cookies.update(cookies)

    return session


@app.get("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "browser_started": browser is not None,
    })


@app.post("/api/feed/start")
def feed_start():
    with lock:
        try:
            ensure_browser()

            return jsonify({
                "status": "started",
                "current": current_reel(),
            })

        except Exception as exc:
            app.logger.exception("Feed startup failed")
            return jsonify({
                "error": "Could not open feed",
                "details": str(exc),
            }), 500


@app.get("/api/feed/current")
def feed_current():
    with lock:
        if page is None:
            return jsonify({
                "error": "Start the feed first"
            }), 409

        return jsonify({
            "current": current_reel(),
        })


@app.get("/api/feed/next")
def feed_next():
    with lock:
        if page is None:
            return jsonify({
                "error": "Start the feed first"
            }), 409

        try:
            previous = current_reel()

            page.locator("body").click(timeout=3000)
            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(2500)

            current = current_reel()

            return jsonify({
                "status": "ok" if current else "not_detected",
                "previous": previous,
                "current": current,
                "video_id": video_id_from_url(current),
            })

        except Exception as exc:
            app.logger.exception("Feed scroll failed")
            return jsonify({
                "error": "Could not scroll feed",
                "details": str(exc),
            }), 500


@app.post("/api/feed/stop")
def feed_stop():
    with lock:
        close_browser()

    return jsonify({"status": "stopped"})


@app.get("/api/videos/<video_id>/download")
def download_video(video_id):
    if not re.fullmatch(r"\d+", video_id):
        return jsonify({"error": "Invalid video ID"}), 400

    session = None
    upstream = None

    try:
        with lock:
            # Get fresh cookies from the active browser context.
            session = live_session()

        upstream_url = (
            f"{BASE_URL}/api/videos/{video_id}/download?watermark=1"
        )

        upstream = session.get(
            upstream_url,
            headers={"Referer": f"{BASE_URL}/v/{video_id}"},
            stream=True,
            allow_redirects=True,
            timeout=(15, 120),
        )

        if upstream.status_code != 200:
            status = upstream.status_code
            content_type = upstream.headers.get("Content-Type", "")
            details = ""

            if "json" in content_type.lower() or "text" in content_type.lower():
                details = upstream.text[:1000]

            upstream.close()
            session.close()

            return jsonify({
                "error": "Upstream download failed",
                "upstream_status": status,
                "details": details,
            }), 502

        content_type = upstream.headers.get("Content-Type", "").lower()

        if any(kind in content_type for kind in (
            "text/html",
            "application/json",
            "text/plain",
        )):
            upstream.close()
            session.close()

            return jsonify({
                "error": "The upstream response was not a video",
                "content_type": content_type,
            }), 502

        def generate():
            try:
                for chunk in upstream.iter_content(128 * 1024):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()
                session.close()

        response = Response(
            stream_with_context(generate()),
            content_type=upstream.headers.get(
                "Content-Type", "video/mp4"
            ),
        )

        response.headers["Content-Disposition"] = (
            f'attachment; filename="{video_id}.mp4"'
        )

        length = upstream.headers.get("Content-Length")

        if length and length.isdigit():
            response.headers["Content-Length"] = length

        response.headers["Cache-Control"] = "no-store"

        return response

    except Exception as exc:
        if upstream is not None:
            upstream.close()

        if session is not None:
            session.close()

        app.logger.exception("Video download failed")

        return jsonify({
            "error": "Download failed",
            "details": str(exc),
        }), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000, threaded=True)
