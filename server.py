#!/usr/bin/env python3
"""Northstar local web server and Ollama proxy."""
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from html.parser import HTMLParser
import zlib
import json
import random
import re
import threading

HOST = "127.0.0.1"
PORT = 8080
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
ROOT = Path(__file__).resolve().parent
BTC_ADDRESS = "bc1qss003pkz4gyzsp7y7r0gx7t6f20wq8ngjev99qaqvpfzl2ee088qgcups5"
ADMIN_CODE = "apexnode-prog_admin"
REDEEMED_TXIDS = ROOT / "redeemed_txids.json"
REDEEM_LOCK = threading.Lock()


def btc_usd_price():
    request = Request("https://mempool.space/api/v1/prices", headers={"User-Agent": "Northstar/1.0"})
    with urlopen(request, timeout=15) as response:
        return float(json.loads(response.read())["USD"])


def load_redeemed():
    try:
        values = json.loads(REDEEMED_TXIDS.read_text("utf-8"))
        return set(values if isinstance(values, list) else [])
    except (OSError, json.JSONDecodeError):
        return set()


class SearchParser(HTMLParser):
    """Extract DuckDuckGo result blocks without external dependencies."""
    def __init__(self):
        super().__init__()
        self.results = []
        self.current = None
        self.capture = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "")
        if tag == "a" and ("result__a" in classes or "result-link" in classes):
            self.current = {"title": "", "url": attrs.get("href", ""), "snippet": ""}
            self.capture = ("title", 1)
        elif self.current and ("result__snippet" in classes or "result-snippet" in classes):
            self.capture = ("snippet", 1)
        elif self.capture:
            self.capture = (self.capture[0], self.capture[1] + 1)

    def handle_data(self, data):
        if self.current and self.capture:
            key, _ = self.capture
            self.current[key] += data

    def handle_endtag(self, tag):
        if self.capture and tag == "a" and self.capture[0] == "title":
            self.capture = None
        if self.current and self.current.get("title", "").strip() and self.current.get("snippet", "").strip():
            self.current = {k: v.strip() for k, v in self.current.items()}
            self.results.append(self.current)
            self.current = None
            self.capture = None


def web_search(query):
    url = "https://html.duckduckgo.com/html/?" + urlencode({"q": query})
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; Northstar/1.0)"})
    with urlopen(req, timeout=15) as response:
        html = response.read().decode("utf-8", "replace")
    parser = SearchParser()
    parser.feed(html)
    return parser.results[:5]


def extract_pdf(data):
    try:
        from pypdf import PdfReader
        import io
        return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)
    except ImportError:
        pass
    try:
        import fitz
        doc = fitz.open(stream=data, filetype="pdf")
        return "\n".join(page.get_text() for page in doc)
    except ImportError:
        pass
    chunks = [data]
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        raw = match.group(1)
        try:
            chunks.append(zlib.decompress(raw))
        except zlib.error:
            chunks.append(raw)
    extracted = []
    for chunk in chunks:
        for raw in re.findall(rb"\((?:\\.|[^\\)])*\)\s*(?:Tj|'|\")", chunk):
            text = raw[1:raw.rfind(b")")]
            text = re.sub(rb"\\([()\\])", rb"\1", text)
            extracted.append(text.decode("utf-8", "ignore"))
        for raw in re.findall(rb"<([0-9A-Fa-f]{4,})>\s*Tj", chunk):
            try:
                extracted.append(bytes.fromhex(raw.decode()).decode("utf-16-be", "ignore"))
            except ValueError:
                pass
    return "\n".join(extracted)


class NorthstarHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def _json(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _ollama_json(self, path):
        with urlopen(f"http://127.0.0.1:11434{path}", timeout=10) as response:
            return json.loads(response.read())

    def do_GET(self):
        if self.path == "/api/tags":
            try:
                self._json(200, self._ollama_json("/api/tags"))
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                self._json(502, {"error": f"Could not query Ollama models: {exc}"})
            return
        super().do_GET()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, ValueError):
            self._json(400, {"error": "Request body must be valid JSON."})
            return

        if self.path == "/api/verify-btc":
            submitted_txid = str(payload.get("txid", "")).strip()
            submitted_code = str(payload.get("code", "")).strip()
            if ADMIN_CODE in (submitted_txid, submitted_code):
                self._json(200, {"verified": True, "admin": True})
                return
            txid = (submitted_txid or submitted_code).lower()
            try:
                price = btc_usd_price()
                quote_btc = 2.0 / price
                if not txid:
                    self._json(200, {"price_usd": price, "btc_due": quote_btc, "address": BTC_ADDRESS})
                    return
                if not re.fullmatch(r"[0-9a-f]{64}", txid):
                    self._json(400, {"verified": False, "error": "Enter a valid 64-character Bitcoin transaction ID."})
                    return
                with REDEEM_LOCK:
                    used = load_redeemed()
                    if txid in used:
                        self._json(200, {"verified": False, "error": "This transaction ID has already been redeemed."})
                        return
                    tx_request = Request(f"https://mempool.space/api/tx/{txid}", headers={"User-Agent": "Northstar/1.0"})
                    with urlopen(tx_request, timeout=20) as response:
                        transaction = json.loads(response.read())
                    received = sum(int(output.get("value", 0)) for output in transaction.get("vout", [])
                                   if output.get("scriptpubkey_address") == BTC_ADDRESS)
                    if received * price / 100_000_000 < 1.90:
                        self._json(200, {"verified": False, "error": "This transaction does not show at least $1.90 USD sent to the access address."})
                        return
                    used.add(txid)
                    REDEEMED_TXIDS.write_text(json.dumps(sorted(used), indent=2) + "\n", encoding="utf-8")
                self._json(200, {"verified": True})
            except HTTPError as exc:
                self._json(502 if exc.code >= 500 else 400, {"verified": False, "error": "The Bitcoin explorer could not find or verify that transaction."})
            except (URLError, TimeoutError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                self._json(502, {"verified": False, "error": f"Could not verify payment right now: {exc}"})
            return

        if self.path == "/api/image":
            prompt = str(payload.get("prompt", "")).strip()
            if not prompt:
                self._json(400, {"error": "A non-empty prompt is required."})
                return
            image_url = (
                "https://pollinations.ai/" + quote(prompt, safe="")
                + f"?width=512&height=512&nologo=true&seed={random.randrange(1, 2**31)}"
            )
            try:
                image_request = Request(image_url, headers={"User-Agent": "Northstar/1.0"})
                with urlopen(image_request, timeout=180) as response:
                    image_bytes = response.read()
                    content_type = response.headers.get_content_type()
                if not image_bytes or not content_type.startswith("image/"):
                    raise ValueError("Image provider returned an empty or non-image response.")
                generated_dir = ROOT / "generated"
                generated_dir.mkdir(parents=True, exist_ok=True)
                filename = f"{random.randrange(1, 2**63):016x}.jpg"
                (generated_dir / filename).write_bytes(image_bytes)
            except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
                self._json(502, {"error": f"Could not generate image: {exc}"})
                return
            self._json(200, {"url": f"/generated/{filename}"})
            return

        if self.path == "/api/extract":
            try:
                import base64
                raw = base64.b64decode(payload.get("data", ""), validate=False)
                text = extract_pdf(raw)
                self._json(200, {"text": text[:200000]})
            except Exception as exc:
                self._json(400, {"error": f"Could not extract PDF text: {exc}"})
            return

        if self.path != "/api/chat":
            self._json(404, {"error": "Not found"})
            return

        # Ollama's /api/chat accepts model, messages, and stream fields. Forward
        # the JSON request and relay its NDJSON stream without buffering it.
        try:
            messages = payload.get("messages", [])
            if payload.get("web_search"):
                query = str(payload.get("search_query", "")).strip()
                if not query:
                    query = next((str(m.get("content", "")) for m in reversed(messages)
                                  if m.get("role") == "user"), "")
                try:
                    results = web_search(query) if query else []
                    context = "Web search results (verify details; cite the URLs when useful):\n" + "\n".join(
                        f"{i+1}. {r['title']}\nURL: {r['url']}\n{r['snippet']}" for i, r in enumerate(results)
                    )
                except (HTTPError, URLError, TimeoutError, OSError):
                    context = "Live web search was unavailable for this request."
                if messages and messages[0].get("role") == "system":
                    messages[0]["content"] = str(messages[0].get("content", "")) + "\n\n" + context
                else:
                    messages.insert(0, {"role": "system", "content": context})
            has_images = False
            data_image = re.compile(r"^data:image/[^;,]+;base64,(.*)$", re.DOTALL)
            for message in messages:
                images = list(message.get("images", []))
                content = message.get("content")
                if isinstance(content, list):
                    kept = []
                    for part in content:
                        image_url = part.get("image_url") if isinstance(part, dict) else None
                        url = image_url.get("url") if isinstance(image_url, dict) else image_url
                        match = data_image.match(url or "") if isinstance(url, str) else None
                        if match:
                            images.append(match.group(1))
                            has_images = True
                        else:
                            kept.append(part)
                    message["content"] = kept
                elif isinstance(content, str):
                    match = data_image.match(content)
                    if match:
                        images.append(match.group(1))
                        message["content"] = ""
                        has_images = True
                if images:
                    normalized = []
                    for image in images:
                        if isinstance(image, str):
                            match = data_image.match(image)
                            normalized.append(match.group(1) if match else image)
                        else:
                            normalized.append(image)
                    message["images"] = normalized
                    has_images = True

            if has_images:
                available = self._ollama_json("/api/tags").get("models", [])
                names = [m.get("name", "").lower() for m in available]
                vision = next((candidate for candidate in ("moondream", "llava", "minicpm-v", "llama3.2-vision")
                               if any(name == candidate or name.startswith(candidate + ":") for name in names)), None)
                if not vision:
                    self._json(400, {"error": "No local vision model is installed yet. Open Terminal and run: ollama pull moondream"})
                    return
                payload["model"] = next(m["name"] for m in available if m.get("name", "").lower().startswith(vision))
            payload["options"] = {**payload.get("options", {}), "num_ctx": 2048, "num_thread": 8}
            body = json.dumps(payload).encode("utf-8")
            request = Request(OLLAMA_URL, data=body, headers={"Content-Type": "application/json"}, method="POST")
            upstream = urlopen(request, timeout=600)
            self.send_response(upstream.status)
            self.send_header("Content-Type", upstream.headers.get("Content-Type", "application/x-ndjson"))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            while True:
                chunk = upstream.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
            upstream.close()
        except HTTPError as exc:
            detail = exc.read()
            self.send_response(exc.code)
            self.send_header("Content-Type", exc.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(detail)))
            self.end_headers()
            self.wfile.write(detail)
        except (URLError, TimeoutError, BrokenPipeError, ConnectionResetError) as exc:
            if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
                return
            self._json(502, {"error": f"Could not reach Ollama at {OLLAMA_URL}: {exc}"})

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))


if __name__ == "__main__":
    print(f"Northstar serving {ROOT} at http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), NorthstarHandler).serve_forever()
