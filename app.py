
import re
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_file, Response
from playwright.sync_api import sync_playwright

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
CURL_FILE = BASE_DIR / "curl.txt"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/156.0.0.0 Safari/537.36"
)

playwright_instance = None
browser = None
context = None
page = None


def extract_cookies(curl_text):
    patterns = [
        r'-b\s+\^"([\s\S]*?)\^"',
        r'--cookie\s+\^"([\s\S]*?)\^"',
        r'-b\s+"([\s\S]*?)"',
        r'--cookie\s+"([\s\S]*?)"',
        r"-b\s+'([\s\S]*?)'",
        r"--cookie\s+'([\s\S]*?)'",
    ]

    cookie_string = None

    for pattern in patterns:
        match = re.search(pattern, curl_text)
        if match:
            cookie_string = match.group(1)
            break

    if cookie_string is None:
        raise ValueError("Could not find cookies in curl.txt")

    cookies = {}

    for item in cookie_string.split(";"):
        item = item.strip()

        if "=" in item:
            name, value = item.split("=", 1)
            cookies[name.strip()] = value.strip()

    return cookies


def load_cookies():
    if not CURL_FILE.exists():
        return {}

    curl_text = CURL_FILE.read_text(encoding="utf-8")
    return extract_cookies(curl_text)


def extract_active_video_url(current_page):
    try:
        return current_page.evaluate("""() => {
            const elements = document.querySelectorAll(
                'a[href*="/v/"], [data-url*="/v/"]'
            );

            for (const el of elements) {
                const href =
                    el.href ||
                    el.getAttribute('data-url') ||
                    el.getAttribute('href') ||
                    '';

                const match = href.match(/\\/v\\/(\\d+)/);

                if (match) {
                    const rect = el.getBoundingClientRect();

                    if (
                        rect.top >= -200 &&
                        rect.top <= window.innerHeight + 200
                    ) {
                        return 'https://smolish.com/v/' + match[1];
                    }
                }
            }

            const html = document.body?.innerHTML || '';
            const match = html.match(/\\/v\\/(\\d+)/);

            if (match) {
                return 'https://smolish.com/v/' + match[1];
            }

            return window.location.href;
        }""")
    except Exception:
        return current_page.url


def init_browser(cookies_dict=None):
    global playwright_instance, browser, context, page

    if page and not page.is_closed():
        return

    if cookies_dict is None:
        cookies_dict = load_cookies()

    if playwright_instance is None:
        playwright_instance = sync_playwright().start()

    browser = playwright_instance.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
    )

    context = browser.new_context(
        viewport={"width": 412, "height": 915},
        user_agent=USER_AGENT,
        locale="en-US",
    )

    if cookies_dict:
        playwright_cookies = [
            {
                "name": name,
                "value": value,
                "domain": ".smolish.com",
                "path": "/",
                "secure": True,
            }
            for name, value in cookies_dict.items()
        ]

        context.add_cookies(playwright_cookies)

    page = context.new_page()

    page.on(
        "console",
        lambda msg: print(f"[BROWSER CONSOLE] {msg.type}: {msg.text}")
        if msg.type == "error"
        else None,
    )

    page.on(
        "pageerror",
        lambda error: print(f"[BROWSER ERROR] {error}"),
    )

    response = page.goto(
        "https://smolish.com/",
        wait_until="domcontentloaded",
        timeout=60000,
    )

    print("[BROWSER] URL:", page.url)
    print("[BROWSER] HTTP:", response.status if response else "No response")
    print("[BROWSER] Title:", page.title())
    print("[BROWSER] Cookies loaded:", len(cookies_dict))

    try:
        page.click("body", timeout=3000)
    except Exception:
        pass


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "status": "online",
        "browser_initialized": bool(page and not page.is_closed()),
        "endpoints": {
            "/feed": "POST - Initialize feed; optional JSON cookies",
            "/scroll": "POST - Scroll to the next reel",
            "/screen": "GET - Screenshot of the current browser",
            "/status": "GET - Browser and verification diagnostics",
            "/download": "POST - Download a video using its link",
        },
    })


@app.route("/feed", methods=["POST"])
def get_feed():
    try:
        data = request.get_json(silent=True) or {}
        cookies_dict = data.get("cookies")

        init_browser(cookies_dict)

        return jsonify({
            "success": True,
            "url": extract_active_video_url(page),
            "title": page.title(),
            "current_page": page.url,
        })

    except Exception as error:
        return jsonify({
            "success": False,
            "error": str(error),
        }), 500


@app.route("/status", methods=["GET"])
def status():
    if not page or page.is_closed():
        return jsonify({
            "initialized": False,
            "message": "Call POST /feed first.",
        })

    try:
        details = page.evaluate("""() => ({
            url: location.href,
            title: document.title,
            readyState: document.readyState,
            bodyText: (document.body?.innerText || '').slice(0, 3000),
            videoCount: document.querySelectorAll('video').length,
            linkCount: document.querySelectorAll('a[href*="/v/"]').length,
            challengeIndicators: [
                'captcha',
                'verify you are human',
                'checking your browser',
                'request could not be verified'
            ].filter(term =>
                (document.body?.innerText || '').toLowerCase().includes(term)
            )
        })""")

        details["initialized"] = True
        details["cookies"] = [
            cookie["name"]
            for cookie in context.cookies("https://smolish.com")
        ]

        return jsonify(details)

    except Exception as error:
        return jsonify({
            "initialized": False,
            "error": str(error),
        }), 500


@app.route("/screen", methods=["GET"])
def screen():
    try:
        if not page or page.is_closed():
            return jsonify({
                "success": False,
                "error": "Call POST /feed first.",
            }), 400

        screenshot = page.screenshot(
            type="png",
            full_page=False,
            animations="disabled",
        )

        return Response(
            screenshot,
            mimetype="image/png",
            headers={"Cache-Control": "no-store"},
        )

    except Exception as error:
        return jsonify({
            "success": False,
            "error": str(error),
        }), 500


@app.route("/scroll", methods=["POST"])
def scroll_feed():
    try:
        if not page or page.is_closed():
            return jsonify({
                "success": False,
                "error": "Call POST /feed first.",
            }), 400

        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(2500)

        return jsonify({
            "success": True,
            "url": extract_active_video_url(page),
            "current_page": page.url,
        })

    except Exception as error:
        return jsonify({
            "success": False,
            "error": str(error),
        }), 500


@app.route("/download", methods=["POST"])
def download_video():
    data = request.get_json(silent=True) or {}
    link = data.get("link")

    if not link:
        return jsonify({
            "success": False,
            "error": "Missing 'link' parameter.",
        }), 400

    match = re.search(r"/v/(\d+)", link)

    if not match:
        return jsonify({
            "success": False,
            "error": "Could not extract video ID.",
        }), 400

    video_id = match.group(1)
    output_file = BASE_DIR / f"{video_id}.mp4"

    try:
        cookies = data.get("cookies")

        if cookies is None:
            cookies = load_cookies()

        session = requests.Session()
        session.headers.update({
            "Accept": "*/*",
            "Referer": f"https://smolish.com/v/{video_id}",
            "User-Agent": USER_AGENT,
        })
        session.cookies.update(cookies)

        url = (
            f"https://smolish.com/api/videos/"
            f"{video_id}/download?watermark=1"
        )

        with session.get(
            url,
            stream=True,
            allow_redirects=True,
            timeout=(15, 120),
        ) as response:
            content_type = response.headers.get(
                "Content-Type", ""
            ).lower()

            if response.status_code != 200:
                return jsonify({
                    "success": False,
                    "error": f"HTTP {response.status_code}",
                    "content_type": content_type,
                    "details": (
                        response.text[:1000]
                        if "json" in content_type or "html" in content_type
                        else None
                    ),
                }), 400

            if (
                "text/html" in content_type
                or "application/json" in content_type
            ):
                return jsonify({
                    "success": False,
                    "error": (
                        "The server returned a verification or error "
                        "response instead of a video."
                    ),
                    "content_type": content_type,
                }), 400

            with output_file.open("wb") as output:
                for chunk in response.iter_content(
                    chunk_size=128 * 1024
                ):
                    if chunk:
                        output.write(chunk)

        return send_file(output_file, as_attachment=True)

    except requests.RequestException as error:
        return jsonify({
            "success": False,
            "error": str(error),
        }), 500

    except Exception as error:
        return jsonify({
            "success": False,
            "error": str(error),
        }), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
