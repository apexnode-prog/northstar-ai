#!/usr/bin/env python3
"""Northstar local web server and Ollama proxy."""
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
import json
import random
import re

HOST = "127.0.0.1"
PORT = 8080
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
ROOT = Path(__file__).resolve().parent


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

        if self.path != "/api/chat":
            self._json(404, {"error": "Not found"})
            return

        # Ollama's /api/chat accepts model, messages, and stream fields. Forward
        # the JSON request and relay its NDJSON stream without buffering it.
        try:
            messages = payload.get("messages", [])
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
