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

session_cookies = {}


def authorized():
    return request.headers.get("X-API-Key", "") == API_KEY


@app.before_request
def check_api_key():
    if request.path == "/api/health":
        return None

    if not authorized():
        return jsonify({"error": "Unauthorized"}), 401


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


def create_browser():
    global playwright, browser, context, page

    close_browser()

    playwright = sync_playwright().start()

    browser = playwright.chromium.launch(
        headless=True
    )

    context = browser.new_context(
        user_agent=USER_AGENT,
        viewport={
            "width": 1280,
            "height": 900,
        },
    )

    if session_cookies:
        cookies = []

        for name, value in session_cookies.items():
            cookies.append({
                "name": name,
                "value": value,
                "domain": ".smolish.com",
                "path": "/",
            })

        context.add_cookies(cookies)

        app.logger.info(
            "Loaded %d session cookie(s)",
            len(cookies),
        )

    page = context.new_page()

    page.set_default_timeout(10000)
    page.set_default_navigation_timeout(30000)


def ensure_browser():
    global browser, context, page

    if (
        browser is not None
        and context is not None
        and page is not None
    ):
        try:
            if browser.is_connected() and not page.is_closed():
                return
        except Exception:
            pass

    create_browser()


def open_feed():
    ensure_browser()

    app.logger.info("Opening Smolish feed")

    try:
        page.goto(
            BASE_URL + "/",
            wait_until="commit",
            timeout=30000,
        )
    except Exception as exc:
        app.logger.warning(
            "Initial navigation did not finish: %s",
            exc,
        )

    try:
        page.wait_for_timeout(5000)
    except Exception:
        pass

    try:
        page.locator("body").click(timeout=3000)
    except Exception:
        pass


def get_live_cookies():
    ensure_browser()

    cookies = context.cookies([
        BASE_URL
    ])

    return {
        cookie["name"]: cookie["value"]
        for cookie in cookies
    }


def video_id_from_url(url):
    if not url:
        return None

    try:
        parsed = urlparse(url)
    except Exception:
        return None

    if parsed.hostname not in (
        "smolish.com",
        "www.smolish.com",
    ):
        return None

    match = re.search(
        r"/v/(\d+)(?:/|$)",
        parsed.path,
    )

    return match.group(1) if match else None


def current_reel():
    if page is None:
        return None

    try:
        current_url = page.url

        video_id = video_id_from_url(
            current_url
        )

        if video_id:
            return f"{BASE_URL}/v/{video_id}"
    except Exception:
        pass

    try:
        links = page.locator(
            'a[href*="/v/"]'
        ).evaluate_all(
            """els => els.map(el => {
                const r = el.getBoundingClientRect();

                return {
                    href: el.href,
                    x: r.x + r.width / 2,
                    y: r.y + r.height / 2,
                    width: r.width,
                    height: r.height
                };
            })"""
        )

        viewport = page.evaluate(
            """() => ({
                w: window.innerWidth,
                h: window.innerHeight
            })"""
        )

        candidates = []

        for item in links:
            if item["width"] <= 0 or item["height"] <= 0:
                continue

            video_id = video_id_from_url(
                item["href"]
            )

            if not video_id:
                continue

            if not (
                0 <= item["y"] <= viewport["h"]
            ):
                continue

            distance = (
                abs(
                    item["x"]
                    - viewport["w"] / 2
                )
                +
                abs(
                    item["y"]
                    - viewport["h"] / 2
                )
            )

            candidates.append(
                (
                    distance,
                    video_id,
                )
            )

        if candidates:
            candidates.sort(
                key=lambda item: item[0]
            )

            return (
                f"{BASE_URL}/v/"
                f"{candidates[0][1]}"
            )

    except Exception as exc:
        app.logger.warning(
            "Reel detection failed: %s",
            exc,
        )

    return None


def wait_for_reel(timeout=15000):
    deadline = (
        __import__("time").time()
        + timeout / 1000
    )

    while __import__("time").time() < deadline:
        reel = current_reel()

        if reel:
            return reel

        try:
            page.wait_for_timeout(500)
        except Exception:
            break

    return current_reel()


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
    connected = False

    if browser is not None:
        try:
            connected = browser.is_connected()
        except Exception:
            connected = False

    return jsonify({
        "status": "ok",
        "browser_started": connected,
        "session_cookies": len(session_cookies),
    })


@app.post("/api/session/cookies")
def set_session_cookies():
    global session_cookies

    data = request.get_json(
        silent=True
    )

    if not isinstance(data, dict):
        return jsonify({
            "error": "JSON object required"
        }), 400

    cookies = data.get("cookies")

    if isinstance(cookies, dict):
        new_cookies = cookies

    elif isinstance(cookies, list):
        new_cookies = {}

        for cookie in cookies:
            if not isinstance(cookie, dict):
                continue

            name = cookie.get("name")
            value = cookie.get("value")

            if name and value is not None:
                new_cookies[name] = value

    else:
        return jsonify({
            "error": (
                "cookies must be an object "
                "or list"
            )
        }), 400

    if not new_cookies:
        return jsonify({
            "error": "No cookies supplied"
        }), 400

    session_cookies = {
        str(name): str(value)
        for name, value in new_cookies.items()
    }

    with lock:
        close_browser()

    return jsonify({
        "status": "session_updated",
        "cookies": len(session_cookies),
        "browser_restarted": True,
    })


@app.post("/api/session/clear")
def clear_session():
    global session_cookies

    with lock:
        session_cookies = {}
        close_browser()

    return jsonify({
        "status": "session_cleared"
    })


@app.post("/api/feed/start")
def feed_start():
    with lock:
        try:
            open_feed()

            current = wait_for_reel(
                timeout=15000
            )

            if current:
                return jsonify({
                    "status": "started",
                    "current": current,
                    "video_id": video_id_from_url(
                        current
                    ),
                })

            return jsonify({
                "status": "started",
                "current": None,
                "message": (
                    "Feed opened, but no "
                    "reel was detected."
                ),
            })

        except Exception as exc:
            app.logger.exception(
                "Feed startup failed"
            )

            return jsonify({
                "error": "Could not open feed",
                "details": str(exc),
            }), 500


@app.get("/api/feed/current")
def feed_current():
    with lock:
        if (
            page is None
            or page.is_closed()
        ):
            return jsonify({
                "error": (
                    "Start the feed first"
                )
            }), 409

        current = current_reel()

        return jsonify({
            "current": current,
            "video_id": video_id_from_url(
                current
            ),
        })


@app.post("/api/feed/next")
def feed_next():
    with lock:
        if (
            page is None
            or page.is_closed()
        ):
            return jsonify({
                "error": (
                    "Start the feed first"
                )
            }), 409

        try:
            previous = current_reel()

            try:
                page.locator(
                    "body"
                ).click(timeout=3000)
            except Exception:
                pass

            page.keyboard.press(
                "ArrowDown"
            )

            page.wait_for_timeout(
                2500
            )

            current = current_reel()

            if current == previous:
                deadline = (
                    __import__("time").time()
                    + 5000 / 1000
                )

                while (
                    __import__("time").time()
                    < deadline
                ):
                    page.wait_for_timeout(
                        500
                    )

                    current = current_reel()

                    if (
                        current
                        and current != previous
                    ):
                        break

            return jsonify({
                "status": (
                    "ok"
                    if current
                    else "not_detected"
                ),
                "previous": previous,
                "current": current,
                "video_id": video_id_from_url(
                    current
                ),
            })

        except Exception as exc:
            app.logger.exception(
                "Feed scroll failed"
            )

            return jsonify({
                "error": (
                    "Could not scroll feed"
                ),
                "details": str(exc),
            }), 500


@app.get("/api/feed/next")
def feed_next_get():
    return feed_next()


@app.post("/api/feed/stop")
def feed_stop():
    with lock:
        close_browser()

    return jsonify({
        "status": "stopped"
    })


@app.get(
    "/api/videos/<video_id>/download"
)
def download_video(video_id):
    if not re.fullmatch(
        r"\d+",
        video_id
    ):
        return jsonify({
            "error": "Invalid video ID"
        }), 400

    session = None
    upstream = None

    try:
        with lock:
            if (
                browser is None
                or context is None
                or page is None
                or page.is_closed()
            ):
                return jsonify({
                    "error": (
                        "Start the feed first"
                    )
                }), 409

            session = live_session()

        upstream_url = (
            f"{BASE_URL}/api/videos/"
            f"{video_id}/download?watermark=1"
        )

        upstream = session.get(
            upstream_url,
            headers={
                "Referer": (
                    f"{BASE_URL}/v/"
                    f"{video_id}"
                )
            },
            stream=True,
            allow_redirects=True,
            timeout=(15, 120),
        )

        content_type = (
            upstream.headers
            .get("Content-Type", "")
            .lower()
        )

        if upstream.status_code != 200:
            status = upstream.status_code
            details = ""

            if (
                "json" in content_type
                or "text" in content_type
            ):
                try:
                    details = upstream.text[
                        :1000
                    ]
                except Exception:
                    pass

            upstream.close()
            session.close()

            return jsonify({
                "error": (
                    "Upstream download failed"
                ),
                "upstream_status": status,
                "content_type": content_type,
                "details": details,
            }), 502

        if any(
            kind in content_type
            for kind in (
                "text/html",
                "application/json",
                "text/plain",
            )
        ):
            upstream.close()
            session.close()

            return jsonify({
                "error": (
                    "The upstream response "
                    "was not a video"
                ),
                "content_type": content_type,
            }), 502

        def generate():
            try:
                for chunk in upstream.iter_content(
                    128 * 1024
                ):
                    if chunk:
                        yield chunk
            finally:
                try:
                    upstream.close()
                except Exception:
                    pass

                try:
                    session.close()
                except Exception:
                    pass

        response = Response(
            stream_with_context(
                generate()
            ),
            content_type=(
                upstream.headers.get(
                    "Content-Type",
                    "video/mp4",
                )
            ),
        )

        response.headers[
            "Content-Disposition"
        ] = (
            f'attachment; filename="{video_id}.mp4"'
        )

        length = upstream.headers.get(
            "Content-Length"
        )

        if length and length.isdigit():
            response.headers[
                "Content-Length"
            ] = length

        response.headers[
            "Cache-Control"
        ] = "no-store"

        return response

    except requests.RequestException as exc:
        if upstream is not None:
            upstream.close()

        if session is not None:
            session.close()

        app.logger.exception(
            "Upstream request failed"
        )

        return jsonify({
            "error": "Download failed",
            "details": str(exc),
        }), 502

    except Exception as exc:
        if upstream is not None:
            upstream.close()

        if session is not None:
            session.close()

        app.logger.exception(
            "Video download failed"
        )

        return jsonify({
            "error": "Download failed",
            "details": str(exc),
        }), 502


@app.get("/api/session/cookies")
def session_status():
    return jsonify({
        "cookies_loaded": len(
            session_cookies
        ),
        "browser_started": (
            browser is not None
        ),
    })


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=10000,
        threaded=True,
    )
