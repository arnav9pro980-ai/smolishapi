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


def get_live_session_data():
    """Dynamically boots Playwright, navigates to Smolish, and harvests live cookies/headers."""
    global playwright_instance, browser, context, page

    if not playwright_instance:
        playwright_instance = sync_playwright().start()

    if not browser or not browser.is_connected():
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
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

    # Hit the home page to trigger any automatic redirects and set valid cookies
    page.goto("https://smolish.com/", wait_until="networkidle")
    
    try:
        page.wait_for_selector('a[href*="/v/"]', timeout=10000)
    except Exception:
        pass

    # Extract cookies from Playwright context to pass to requests session
    pw_cookies = context.cookies("https://smolish.com/")
    cookies_dict = {c["name"]: c["value"] for c in pw_cookies}
    
    current_url = page.url
    user_agent = context.pages[0].evaluate("navigator.userAgent")

    return cookies_dict, user_agent, current_url, page


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "status": "online",
        "endpoints": {
            "/feed": "GET - Fetches the current live feed/redirect URL using auto-harvested cookies",
            "/scroll": "POST - Scrolls down to the next reel and gets its URL",
            "/download": "POST - Downloads video by providing 'link' in JSON body",
        },
    })


@app.route("/feed", methods=["GET"])
def get_feed():
    try:
        _, _, current_url, _ = get_live_session_data()
        return jsonify({"success": True, "url": current_url})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/scroll", methods=["POST"])
def scroll_feed():
    try:
        _, _, _, current_page = get_live_session_data()
        if not current_page or current_page.is_closed():
            return jsonify({"success": False, "error": "Browser session not active."}), 400

        # Scroll down to load the next reel
        current_page.evaluate("""() => {
            window.scrollBy({ top: window.innerHeight, behavior: 'smooth' });
        }""")
        current_page.keyboard.press("PageDown")
        current_page.wait_for_timeout(3000)

        new_url = current_page.url
        return jsonify({"success": True, "url": new_url})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/download", methods=["POST"])
def download_video():
    data = request.get_json() or {}
    user_input_link = data.get("link")

    if not user_input_link:
        return jsonify({"success": False, "error": "Missing 'link' parameter in JSON body"}), 400

    id_match = re.search(r"/v/(\d+)", user_input_link)
    if not id_match:
        return jsonify({"success": False, "error": "Could not extract Video ID from link"}), 400

    video_id = id_match.group(1)
    output_file = BASE_DIR / f"{video_id}.mp4"
    url = f"https://smolish.com/api/videos/{video_id}/download?watermark=1"
    page_url = f"https://smolish.com/v/{video_id}"

    try:
        cookies_dict, user_agent, _, _ = get_live_session_data()
    except Exception as e:
        return jsonify({"success": False, "error": f"Failed to get session cookies: {str(e)}"}), 500

    headers = {
        "Accept": "*/*",
        "Referer": page_url,
        "User-Agent": user_agent,
    }

    session = requests.Session()
    session.headers.update(headers)
    session.cookies.update(cookies_dict)

    try:
        with session.get(url, stream=True, allow_redirects=True, timeout=(15, 120)) as response:
            if response.status_code != 200:
                return jsonify({"success": False, "error": f"Request rejected with status {response.status_code}"}), 400

            content_type = response.headers.get("Content-Type", "").lower()
            if "text/html" in content_type or "application/json" in content_type:
                return jsonify({"success": False, "error": "Response is not a video. Verification challenge failed."}), 400

            with output_file.open("wb") as output:
                for chunk in response.iter_content(chunk_size=128 * 1024):
                    if chunk:
                        output.write(chunk)

        return send_file(output_file, as_attachment=True)

    except requests.RequestException as error:
        return jsonify({"success": False, "error": str(error)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
