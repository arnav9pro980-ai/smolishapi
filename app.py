import re
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from playwright.sync_api import sync_playwright

app = FastAPI(title="Smolish Downloader API")

# ================= HARDCODED CREDENTIALS =================
# !!! This file must NEVER go into a public repo. !!!
# These cookies ARE your logged-in session.

COOKIES = {
    "smolish_vd": "30255403082186752.0VaOiyRiW-44O0k9V9U3sd32UlT115EJ",
    "sg": "GtUMBim7zr8oZSvYYL6WCA.tnz1nh.PqlwylVc6o.j1srhVSD.1.SZax_I0nGVC895d6zKVRIFsYV07oXe7N",
    "sgk": "FhZsFL7yAyIomElhY_qHD82Um8z8rKPet7yINCwJc8Q.tnz1nh",
    "cf_clearance": "AydDI_ruzZkoHq12CtywrY4zn6YBH7PlNaBiI5rYD00-1793180971-1.2.1.1-IQ9axCbSHImIvXwCcAbQyceQrFCvWuTQSF5eGMquqP8c8d8EgXhaVXjboniG3voGn8aZbHMfm_3uvyJ2OH5k8ya66iLDKU0EM2uwvTdR3FCQlmoqpdrf2newOgNJitFdNGDxy7S.yjjCmkzxU9SuHeg4ixXoq0g1Bdfjibl6h6sDYqJk7pi1Wj1tK0lyht2znoMMJa75X.36I4uF26qL1U7QBOHXxma.g.a3pXjnCWwxYK.6Frv1UJLtkIipPbOfdXq9lcRL8eDW99NZtfIl6Tk.w4ioz1QoGoAWtdUUBYZ2NPVBsG0ggih.ivOAX2fmDxQsjvn95YVmhJaUbDIu4y8B6dc0AucAVTuJMsqjOX8VrbgAg62iQBDQD2v2HpNGhn5wetdV6EW2s0xwN2rEJ.IcT9eMvPZD.vOqx1y323k",
}

HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9,hi;q=0.8,zh-CN;q=0.7,zh;q=0.6",
    "priority": "u=1, i",
    "sec-ch-ua": '"Not:A-Brand";v="8", "Chromium";v="156", "Google Chrome";v="156"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/156.0.0.0 Safari/537.36"
    ),
}
# ==========================================================


def get_session(page_url: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    session.headers["Referer"] = page_url  # per-video referer
    session.cookies.update(COOKIES)
    return session


def parse_video_id(link: str) -> str:
    m = re.search(r"/v/(\d+)", link)
    if not m:
        raise HTTPException(400, "Could not extract video ID. Expected https://smolish.com/v/<id>")
    return m.group(1)


@app.get("/")
def health():
    return {"status": "ok", "cookies_loaded": len(COOKIES)}


@app.get("/download/{video_id}")
def download(video_id: str, watermark: int = 0):
    page_url = f"https://smolish.com/v/{video_id}"
    url = f"https://smolish.com/api/videos/{video_id}/download?watermark={watermark}"
    session = get_session(page_url)

    upstream = session.get(url, stream=True, allow_redirects=True, timeout=(15, 120))
    ctype = upstream.headers.get("Content-Type", "").lower()

    if upstream.status_code != 200:
        snippet = upstream.text[:300] if "json" in ctype else ""
        upstream.close()
        raise HTTPException(502, f"Upstream returned {upstream.status_code} {snippet}")

    if "text/html" in ctype or "application/json" in ctype:
        upstream.close()
        raise HTTPException(502, "Upstream returned a challenge/HTML page — cookies are stale. Re-copy the cURL and update COOKIES in main.py.")

    def chunk_iter():
        try:
            for chunk in upstream.iter_content(chunk_size=256 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    out_headers = {"Content-Disposition": f'attachment; filename="{video_id}.mp4"'}
    if "Content-Length" in upstream.headers:
        out_headers["Content-Length"] = upstream.headers["Content-Length"]

    return StreamingResponse(
        chunk_iter(),
        media_type=upstream.headers.get("Content-Type", "video/mp4"),
        headers=out_headers,
    )


class DownloadBody(BaseModel):
    link: str
    watermark: int = 0


@app.post("/download")
def download_by_link(body: DownloadBody):
    return download(video_id=parse_video_id(body.link), watermark=body.watermark)


@app.get("/feed")
def feed(count: int = 5):
    playwright_cookies = [
        {"name": n, "value": v, "domain": ".smolish.com", "path": "/"}
        for n, v in COOKIES.items()
    ]

    urls = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)  # must be headless on Render
        context = browser.new_context(user_agent=HEADERS["user-agent"])
        context.add_cookies(playwright_cookies)
        page = context.new_page()
        page.goto("https://smolish.com/", wait_until="domcontentloaded")
        try:
            page.click("body", timeout=3000)
        except Exception:
            pass

        current = page.url
        if re.search(r"/v/\d+", current):
            urls.append(current)

        for _ in range(max(1, min(count, 25))):
            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(2500)
            if page.url != current:
                current = page.url
                urls.append(current)

        browser.close()

    return {"videos": urls}
