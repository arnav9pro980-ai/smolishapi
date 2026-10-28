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
        raise ValueError("Could not find the cookie argument in curl text")

    cookies = {}
    for item in cookie_string.split(";"):
        item = item.strip()
        if "=" in item:
            name, value = item.split("=", 1)
            cookies[name.strip()] = value.strip()
    return cookies


def extract_headers(curl_text):
    headers = {}
    for match in re.finditer(r'-H\s+\^"([^"]*(?:\^[^"]*)*)\^"', curl_text):
        raw = match.group(1)
        raw = raw.replace('^"', '"').replace("^", "")
        if ":" in raw:
            name, value = raw.split(":", 1)
            headers[name.strip()] = value.strip()
    return headers


def init_browser_with_curl(curl_text):
    """Initializes Playwright using cookies extracted from the user's cURL string."""
    global playwright_instance, browser, context, page

    if page and not page.is_closed():
        return  # Already initialized

    cookies_dict = extract_cookies(curl_text)

    if not playwright_instance:
        playwright_instance = sync_playwright().start()

    # Launch headless browser with sandbox flags for Render/Linux container environments
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
            "Chrome/156.0.0.0 Safari/537.36"
        )
    )

    playwright_cookies = [
        {"name": name, "value": value, "domain": ".smolish.com", "path": "/"}
        for name, value in cookies_dict.items()
    ]
    context.add_cookies(playwright_cookies)

    page = context.new_page()
    page.goto("https://smolish.com/", wait_until="domcontentloaded")
    
    try:
        page.click("body", timeout=3000)
    except Exception:
        pass


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "status": "online",
        "endpoints": {
            "/feed": "POST - Initialize feed by passing JSON body with 'curl_text'",
            "/scroll": "POST - Scroll down to the next reel and get its redirected URL",
            "/download": "POST - Download video by providing 'link' and 'curl_text' in JSON body",
        },
    })


@app.route("/feed", methods=["POST"])
def get_feed():
    try:
        data = request.get_json() or {}
        curl_text = data.get("curl_text")

        if not curl_text:
            return jsonify({"success": False, "error": "Missing 'curl_text' parameter in JSON body"}), 400

        init_browser_with_curl(curl_text)
        current_url = page.url
        return jsonify({"success": True, "url": current_url})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/scroll", methods=["POST"])
def scroll_feed():
    try:
        if not page or page.is_closed():
            return jsonify({"success": False, "error": "Feed session not initialized. Call /feed first with your curl_text."}), 400

        # Simulate pressing Down Arrow to trigger the site's native redirect/snap-scroll
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(2500)

        current_url = page.url
        return jsonify({"success": True, "url": current_url})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/download", methods=["POST"])
def download_video():
    data = request.get_json() or {}
    user_input_link = data.get("link")
    curl_text = data.get("curl_text")

    if not user_input_link:
        return jsonify({"success": False, "error": "Missing 'link' parameter in JSON body"}), 400
    if not curl_text:
        return jsonify({"success": False, "error": "Missing 'curl_text' parameter in JSON body"}), 400

    id_match = re.search(r"/v/(\d+)", user_input_link)
    if not id_match:
        return jsonify({"success": False, "error": "Could not extract Video ID from link"}), 400

    video_id = id_match.group(1)
    output_file = BASE_DIR / f"{video_id}.mp4"
    url = f"https://smolish.com/api/videos/{video_id}/download?watermark=1"
    page_url = f"https://smolish.com/v/{video_id}"

    try:
        cookies = extract_cookies(curl_text)
        headers = extract_headers(curl_text)
    except ValueError as error:
        return jsonify({"success": False, "error": str(error)}), 400

    headers.setdefault("Accept", "*/*")
    headers.setdefault("Referer", page_url)
    headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/156.0.0.0 Safari/537.36",
    )

    headers.pop("Cookie", None)
    headers.pop("Host", None)
    headers.pop("Content-Length", None)

    session = requests.Session()
    session.headers.update(headers)
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
