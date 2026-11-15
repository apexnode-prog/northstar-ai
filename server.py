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
import hmac
import ipaddress
import subprocess

HOST = "127.0.0.1"
PORT = 8080
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
ROOT = Path(__file__).resolve().parent
BTC_ADDRESS = "bc1qss003pkz4gyzsp7y7r0gx7t6f20wq8ngjev99qaqvpfzl2ee088qgcups5"
ADMIN_CODE = "apexnode-prog_admin"
REDEEMED_TXIDS = ROOT / "redeemed_txids.json"
REDEEM_LOCK = threading.Lock()
IP_USAGE = ROOT / "ip_usage.json"
IP_USAGE_LOCK = threading.Lock()
FREE_TRIAL_LIMIT = 5


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


def load_ip_usage():
    try:
        values = json.loads(IP_USAGE.read_text("utf-8"))
        if not isinstance(values, dict):
            return {}
        return {str(ip): max(0, int(count)) for ip, count in values.items()}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def save_ip_usage(values):
    temporary = IP_USAGE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")
    temporary.replace(IP_USAGE)


def curl_download(url, output_path, headers=None, post_data_path=None):
    command = ["curl", "-fsSL", "--max-time", "45", "-A", "Mozilla/5.0"]
    for header in headers or []:
        command.extend(["-H", header])
    if post_data_path:
        command.extend(["--data-binary", f"@{post_data_path}"])
    command.extend(["-o", str(output_path), url])
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"curl exited with status {result.returncode}")


def clean_image_prompt(value):
    prompt = re.sub(r"\s+", " ", str(value or "")).strip().strip(".!? ")
    prefixes = (
        r"^(?:please\s+)?i\s+(?:would\s+like|want|need)(?:\s+you\s+to)?\s+",
        r"^(?:can|could|would)\s+you(?:\s+please)?\s+",
        r"^(?:show\s+me|give\s+me|make(?:\s+me)?|create(?:\s+me)?|generate|draw|illustrate|paint|sketch|visuali[sz]e)\s+",
        r"^to\s+see\s+",
    )
    for _ in range(4):
        previous = prompt
        for prefix in prefixes:
            prompt = re.sub(prefix, "", prompt, flags=re.IGNORECASE)
        if prompt == previous:
            break
    prompt = re.sub(
        r"^(?:an?\s+)?(?:image|picture|photo|photograph)\s+(?:of|showing|depicting)\s+",
        "",
        prompt,
        flags=re.IGNORECASE,
    )
    prompt = re.sub(r"^(?:an?|some)\s+", "", prompt, flags=re.IGNORECASE).strip()
    return prompt


def valid_image_file(path):
    try:
        if path.stat().st_size <= 2048:
            return None
        with path.open("rb") as image_file:
            signature = image_file.read(8)
        if signature.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if signature.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
    except OSError:
        pass
    return None


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
        self.send_header("Access-Control-Allow-Headers", "Content-Type, ngrok-skip-browser-warning, X-Access-Token")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _ollama_json(self, path):
        with urlopen(f"http://127.0.0.1:11434{path}", timeout=10) as response:
            return json.loads(response.read())

    def _visitor_ip(self):
        forwarded = self.headers.get("X-Forwarded-For", "")
        candidate = forwarded.split(",", 1)[0].strip() if forwarded else ""
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            return str(self.client_address[0])

    def _has_access_token(self):
        token = self.headers.get("X-Access-Token", "").strip()
        if token and hmac.compare_digest(token, ADMIN_CODE):
            return True
        if not token:
            return False
        with REDEEM_LOCK:
            redeemed = {str(value).lower() for value in load_redeemed()}
        return token.lower() in redeemed

    def _trial_snapshot(self, ip):
        with IP_USAGE_LOCK:
            used = load_ip_usage().get(ip, 0)
        return {"used": used, "remaining": max(0, FREE_TRIAL_LIMIT - used)}

    def do_GET(self):
        route = self.path.split("?", 1)[0]
        if route == "/api/trial-status":
            ip = self._visitor_ip()
            snapshot = self._trial_snapshot(ip)
            snapshot["unlocked"] = self._has_access_token()
            self._json(200, snapshot)
            return
        if self.path == "/api/tags":
            try:
                self._json(200, self._ollama_json("/api/tags"))
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                self._json(502, {"error": f"Could not query Ollama models: {exc}"})
            return
        super().do_GET()

    def do_POST(self):
        route = self.path.split("?", 1)[0]
        if route in ("/api/chat", "/api/image") and not self._has_access_token():
            ip = self._visitor_ip()
            with IP_USAGE_LOCK:
                usage = load_ip_usage()
                used = usage.get(ip, 0)
                if used >= FREE_TRIAL_LIMIT:
                    self._json(402, {"error": "Free trial limit reached", "remaining": 0})
                    return
                usage[ip] = used + 1
                try:
                    save_ip_usage(usage)
                except OSError as exc:
                    self._json(500, {"error": f"Could not record free-trial usage: {exc}"})
                    return

        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, ValueError):
            self._json(400, {"error": "Request body must be valid JSON."})
            return

        if route == "/api/verify-btc":
            submitted_txid = str(payload.get("txid", "")).strip()
            submitted_code = str(payload.get("code", "")).strip()
            if any(hmac.compare_digest(candidate, ADMIN_CODE)
                   for candidate in (submitted_txid, submitted_code) if candidate):
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

        if route == "/api/image":
            original_prompt = str(payload.get("prompt", "")).strip()
            if not original_prompt:
                self._json(400, {"error": "A non-empty prompt is required."})
                return
            prompt = clean_image_prompt(original_prompt)
            if not prompt:
                prompt = "colorful educational landscape illustration"
            if not re.search(r"\b(diagram|cartoon|anime|sketch|drawing)\b", original_prompt, re.I):
                prompt += ", photorealistic, realistic photograph, natural lighting, sharp focus, high detail"

            hf_token = str(payload.get("hf_token", "")).strip()
            generated_dir = ROOT / "generated"
            try:
                generated_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self._json(500, {"error": f"Could not create generated image folder: {exc}"})
                return

            output_path = generated_dir / f"{random.randrange(1, 2**63):016x}.jpg"
            content_type = None
            failures = []
            hf_request_path = None
            try:
                if hf_token:
                    hf_request_path = generated_dir / f"{random.randrange(1, 2**63):016x}.request.json"
                    hf_request_path.write_text(json.dumps({"inputs": prompt}), encoding="utf-8")
                    hf_url = "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-dev"
                    curl_download(
                        hf_url,
                        output_path,
                        headers=[
                            f"Authorization: Bearer {hf_token}",
                            "Content-Type: application/json",
                        ],
                        post_data_path=hf_request_path,
                    )
                    content_type = valid_image_file(output_path)
                    if not content_type:
                        raise RuntimeError("Hugging Face did not return a valid JPEG or PNG image.")
                else:
                    encoded_prompt = quote(prompt, safe="")
                    first_seed = random.randrange(1, 2**31)
                    retry_seed = random.randrange(1, 2**31)
                    image_urls = (
                        f"https://pollinations.ai/{encoded_prompt}?model=flux&width=1024&height=1024&nologo=true&enhance=true&seed={first_seed}",
                        f"https://pollinations.ai/{encoded_prompt}?model=flux&width=1024&height=1024&nologo=true&seed={retry_seed}",
                    )
                    for image_url in image_urls:
                        try:
                            curl_download(image_url, output_path)
                            content_type = valid_image_file(output_path)
                            if content_type:
                                break
                            failures.append("Pollinations returned a file that is not a valid JPEG or PNG image.")
                        except (OSError, RuntimeError) as exc:
                            failures.append(str(exc))
                        try:
                            output_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                    if not content_type:
                        raise RuntimeError("Could not generate image: " + "; ".join(failures))
            except (OSError, RuntimeError) as exc:
                try:
                    output_path.unlink(missing_ok=True)
                except OSError:
                    pass
                self._json(502, {"error": str(exc)})
                return
            finally:
                if hf_request_path:
                    try:
                        hf_request_path.unlink(missing_ok=True)
                    except OSError:
                        pass

            extension = ".png" if content_type == "image/png" else ".jpg"
            final_path = output_path.with_suffix(extension)
            try:
                if final_path != output_path:
                    output_path.replace(final_path)
            except OSError as exc:
                self._json(500, {"error": f"Could not save generated image: {exc}"})
                return
            self._json(200, {"url": f"/generated/{final_path.name}"})
            return

        if route == "/api/extract":
            try:
                import base64
                raw = base64.b64decode(payload.get("data", ""), validate=False)
                text = extract_pdf(raw)
                self._json(200, {"text": text[:200000]})
            except Exception as exc:
                self._json(400, {"error": f"Could not extract PDF text: {exc}"})
            return

        if route != "/api/chat":
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
