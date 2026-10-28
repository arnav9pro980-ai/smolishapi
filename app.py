import re
from pathlib import Path
import requests
from flask import Flask, jsonify, request, send_file
from playwright.sync_api import sync_playwright

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent

# Global state for Playwright & Browser session
playwright_instance = None
browser = None
context = None
page = None


def extract_active_video_url(current_page):
    """Deep scans the DOM to find any active video ID matching /v/ID in the viewport."""
    try:
        url = current_page.evaluate("""() => {
            // 1. Search all elements with href, data attributes, or internal links containing /v/
            const allElements = document.querySelectorAll('*');
            for (const el of allElements) {
                const href = el.href || el.getAttribute('href') || el.getAttribute('data-url') || '';
                const match = href.match(/\/v\/(\d+)/);
                if (match) {
                    const rect = el.getBoundingClientRect();
                    // Check if it's currently visible in the viewport window
                    if (rect.top >= -200 && rect.top <= window.innerHeight + 200) {
                        return 'https://smolish.com/v/' + match[1];
                    }
                }
            }
            
            // 2. Fallback: Search for any /v/ pattern anywhere in the HTML body
            const matches = [...document.body.innerHTML.matchAll(/\/v\/(\d+)/g)];
            if (matches.length > 0) {
                return 'https://smolish.com/v/' + matches[0][1];
            }
            
            return window.location.href;
        }""")
        return url
    except Exception:
        return current_page.url


def init_browser(cookies_dict=None):
    """Initializes Playwright and opens the Smolish feed page."""
    global playwright_instance, browser, context, page

    if page and not page.is_closed():
        return  # Already initialized

    if not playwright_instance:
        playwright_instance = sync_playwright().start()

    # Launch headless browser with sandbox flags & realistic mobile/desktop viewport
    browser = playwright_instance.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ]
    )
    
    context = browser.new_context(
        viewport={"width": 412, "height": 915},  # Mobile viewport mimics short-video apps perfectly
        user_agent=(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 "
            "Mobile/15E148 Safari/604.1"
        )
    )

    if cookies_dict:
        playwright_cookies = [
            {"name": name, "value": value, "domain": ".smolish.com", "path": "/"}
            for name, value in cookies_dict.items()
        ]
        context.add_cookies(playwright_cookies)

    page = context.new_page()
    page.goto("https://smolish.com/", wait_until="networkidle")
    
    try:
        page.wait_for_selector('a[href*="/v/"]', timeout=12000)
    except Exception:
        pass

    try:
        page.click("body", timeout=3000)
    except Exception:
        pass


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "status": "online",
        "endpoints": {
            "/feed": "POST - Initialize feed (optional JSON body with 'cookies' dict)",
            "/scroll": "POST - Scroll down to the next reel and get its link",
            "/download": "POST - Download video by providing 'link' (and optional 'cookies') in JSON body",
        },
    })


@app.route("/feed", methods=["POST"])
def get_feed():
    try:
        data = request.get_json() or {}
        cookies_dict = data.get("cookies", {})
        
        init_browser(cookies_dict)
        reel_url = extract_active_video_url(page)
        return jsonify({"success": True, "url": reel_url})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/scroll", methods=["POST"])
def scroll_feed():
    try:
        if not page or page.is_closed():
            return jsonify({"success": False, "error": "Feed session not initialized. Call /feed first."}), 400

        # Multi-layer scroll execution (Container scroll + Window scroll + Wheel event + PageDown)
        scrolled = page.evaluate("""() => {
            const elements = [...document.querySelectorAll("*")]
                .filter(el => {
                    const s = getComputedStyle(el);
                    return el.scrollHeight > el.clientHeight + 10 &&
                        ["auto", "scroll", "overlay"].includes(s.overflowY);
                })
                .sort((a, b) =>
                    (b.scrollHeight - b.clientHeight) -
                    (a.scrollHeight - a.clientHeight)
                );

            let didScroll = false;
            if (elements.length > 0) {
                elements[0].scrollTop += window.innerHeight;
                didScroll = true;
            }
            
            window.scrollBy({ top: window.innerHeight, behavior: 'smooth' });
            window.scrollBy(0, window.innerHeight);
            return didScroll;
        }""")

        # Also trigger native mouse wheel and keyboard actions
        page.mouse.wheel(0, 800)
        page.keyboard.press("PageDown")
        
        page.wait_for_timeout(3500)  # Allow reel transition animation to finish
        reel_url = extract_active_video_url(page)
        return jsonify({"success": True, "scrolled_container": scrolled, "url": reel_url})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/download", methods=["POST"])
def download_video():
    data = request.get_json() or {}
    user_input_link = data.get("link")
    cookies = data.get("cookies", {})

    if not user_input_link:
        return jsonify({"success": False, "error": "Missing 'link' parameter in JSON body"}), 400

    id_match = re.search(r"/v/(\d+)", user_input_link)
    if not id_match:
        return jsonify({"success": False, "error": "Could not extract Video ID from link"}), 400

    video_id = id_match.group(1)
    output_file = BASE_DIR / f"{video_id}.mp4"
    url = f"https://smolish.com/api/videos/{video_id}/download?watermark=1"
    page_url = f"https://smolish.com/v/{video_id}"

    headers = {
        "Accept": "*/*",
        "Referer": page_url,
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 "
            "Mobile/15E148 Safari/604.1"
        ),
    }

    session = requests.Session()
    session.headers.update(headers)
    if cookies:
        session.cookies.update(cookies)

    try:
        with session.get(url, stream=True, allow_redirects=True, timeout=(15, 120)) as response:
            if response.status_code != 200:
                return jsonify({"success": False, "error": f"Request rejected with status {response.status_code}"}), 400

            content_type = response.headers.get("Content-Type", "").lower()
            if "text/html" in content_type or "application/json" in content_type:
                return jsonify({"success": False, "error": "Response is not a video. Cookies might have expired."}), 400

            with output_file.open("wb") as output:
                for chunk in response.iter_content(chunk_size=128 * 1024):
                    if chunk:
                        output.write(chunk)

        return send_file(output_file, as_attachment=True)

    except requests.RequestException as error:
        return jsonify({"success": False, "error": str(error)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
