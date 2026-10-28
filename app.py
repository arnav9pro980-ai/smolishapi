import re
import requests
from pathlib import Path
from flask import Flask, jsonify, request
from playwright.sync_api import sync_playwright

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
CURL_FILE = BASE_DIR / "curl.txt"

# Global state for Playwright Feed session
playwright_instance = None
browser_instance = None
context_instance = None
page_instance = None


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
        raise ValueError("Could not find the cookie argument in curl.txt")

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


@app.route("/api/feed/start", methods=["POST"])
def start_feed():
    global playwright_instance, browser_instance, context_instance, page_instance

    if not CURL_FILE.exists():
        return jsonify({"error": "curl.txt file not found."}), 400

    curl_text = CURL_FILE.read_text(encoding="utf-8")
    try:
        cookies_dict = extract_cookies(curl_text)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # Clean up any existing session
    if browser_instance:
        try:
            browser_instance.close()
        except:
            pass

    playwright_instance = sync_playwright().start()
    browser_instance = playwright_instance.chromium.launch(headless=True) # Set to False if you want to watch
    context_instance = browser_instance.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
    )

    playwright_cookies = [
        {"name": name, "value": value, "domain": ".smolish.com", "path": "/"}
        for name, value in cookies_dict.items()
    ]
    context_instance.add_cookies(playwright_cookies)

    page_instance = context_instance.new_page()
    page_instance.goto("https://smolish.com/", wait_until="domcontentloaded")
    page_instance.wait_for_timeout(2000)

    current_url = page_instance.url
    return jsonify({"status": "success", "url": current_url})


@app.route("/api/feed/next", methods=["POST"])
def next_feed():
    global page_instance
    if not page_instance:
        return jsonify({"error": "Feed session not started. Call /api/feed/start first."}), 400

    try:
        page_instance.keyboard.press("ArrowDown")
        page_instance.wait_for_timeout(2500)
        current_url = page_instance.url
        return jsonify({"status": "success", "url": current_url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/feed/close", methods=["POST"])
def close_feed():
    global playwright_instance, browser_instance, context_instance, page_instance
    try:
        if browser_instance:
            browser_instance.close()
        if playwright_instance:
            playwright_instance.stop()
        browser_instance = None
        playwright_instance = None
        page_instance = None
        return jsonify({"status": "success", "message": "Feed session closed."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/download", methods=["POST"])
def download_video():
    data = request.get_json() or {}
    video_url = data.get("url")

    if not video_url:
        return jsonify({"error": "Missing 'url' parameter in JSON payload."}), 400

    id_match = re.search(r"/v/(\d+)", video_url)
    if not id_match:
        return jsonify({"error": "Invalid Smolish video URL format."}), 400

    video_id = id_match.group(1)
    output_file = BASE_DIR / f"{video_id}.mp4"
    api_url = f"https://smolish.com/api/videos/{video_id}/download?watermark=1"
    page_url = f"https://smolish.com/v/{video_id}"

    if not CURL_FILE.exists():
        return jsonify({"error": "curl.txt file not found."}), 400

    curl_text = CURL_FILE.read_text(encoding="utf-8")
    try:
        cookies = extract_cookies(curl_text)
        headers = extract_headers(curl_text)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    headers.setdefault("Accept", "*/*")
    headers.setdefault("Referer", page_url)
    headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
    )
    headers.pop("Cookie", None)
    headers.pop("Host", None)
    headers.pop("Content-Length", None)

    session = requests.Session()
    session.headers.update(headers)
    session.cookies.update(cookies)

    try:
        with session.get(api_url, stream=True, allow_redirects=True, timeout=(15, 120)) as response:
            if response.status_code != 200:
                return jsonify({"error": f"Request rejected with status {response.status_code}"}), 400

            content_type = response.headers.get("Content-Type", "").lower()
            if "text/html" in content_type or "application/json" in content_type:
                return jsonify({"error": "Response is not a video. Cookies might have expired."}), 400

            with output_file.open("wb") as output:
                for chunk in response.iter_content(chunk_size=128 * 1024):
                    if chunk:
                        output.write(chunk)

        return jsonify({"status": "success", "file": str(output_file)})

    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
