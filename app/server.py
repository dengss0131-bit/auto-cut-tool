import cgi
import json
import shutil
import socketserver
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
import mimetypes

from autocut import autocut, MODES

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "app" / "web"
OUTPUTS = ROOT / "outputs"
UPLOADS = ROOT / "uploads"
PREVIEWS = ROOT / "previews"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024 * 1024


class LargeUploadThreadingHTTPServer(ThreadingHTTPServer):
    request_queue_size = 16

    def server_bind(self):
        self.socket.setsockopt(socketserver.socket.SOL_SOCKET, socketserver.socket.SO_REUSEADDR, 1)
        self.socket.settimeout(None)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    def _send(self, status=200, content_type="application/json; charset=utf-8", body=b""):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ["/", "/index.html"]:
            html = (WEB / "index.html").read_bytes()
            self._send(200, "text/html; charset=utf-8", html)
            return
        if parsed.path == "/api/modes":
            self._send(200, body=json.dumps(MODES, ensure_ascii=False).encode("utf-8"))
            return
        if parsed.path.startswith("/preview/"):
            name = Path(parsed.path.replace("/preview/", "", 1)).name
            video_path = PREVIEWS / name
            if not video_path.exists():
                self._send(404, body=json.dumps({"error": "preview not found"}, ensure_ascii=False).encode("utf-8"))
                return
            self.serve_file_with_range(video_path, "video/mp4")
            return
        self._send(404, body=json.dumps({"error": "not found"}, ensure_ascii=False).encode("utf-8"))

    def serve_file_with_range(self, file_path: Path, content_type: str = None):
        total = file_path.stat().st_size
        content_type = content_type or mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        range_header = self.headers.get("Range")
        start = 0
        end = total - 1
        status = 200
        if range_header:
            try:
                unit, value = range_header.split("=", 1)
                if unit.strip().lower() == "bytes":
                    start_text, end_text = value.split("-", 1)
                    start = int(start_text) if start_text else 0
                    end = int(end_text) if end_text else total - 1
                    end = min(end, total - 1)
                    status = 206
            except Exception:
                start = 0
                end = total - 1
                status = 200
        length = max(0, end - start + 1)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()
        with file_path.open("rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/export":
            self.handle_export()
            return
        if parsed.path != "/api/autocut":
            self._send(404, body=json.dumps({"error": "not found"}, ensure_ascii=False).encode("utf-8"))
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                raise ValueError("没有收到上传内容，请重新选择视频")
            if length > MAX_UPLOAD_BYTES:
                raise ValueError("视频文件太大，当前限制为 20GB")

            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
                },
            )

            file_item = form["video"] if "video" in form else None
            if file_item is None or not getattr(file_item, "filename", ""):
                raise ValueError("请点击选择一个视频文件")

            original_name = Path(file_item.filename).name
            safe_name = "".join(ch if ch.isalnum() or ch in ".-_ " else "_" for ch in original_name).strip() or "input.mp4"
            UPLOADS.mkdir(parents=True, exist_ok=True)
            source = UPLOADS / safe_name
            counter = 1
            while source.exists():
                source = UPLOADS / f"{Path(safe_name).stem}_{counter}{Path(safe_name).suffix}"
                counter += 1

            with source.open("wb") as f:
                shutil.copyfileobj(file_item.file, f, length=1024 * 1024)

            if source.stat().st_size == 0:
                source.unlink(missing_ok=True)
                raise ValueError("上传后的视频文件为空，请重新选择视频")

            mode = form.getfirst("mode", "standard")
            preview_output = PREVIEWS / f"{source.stem}_preview_{mode}.mp4"

            def optional_float(name):
                value = form.getfirst(name, "")
                return float(value) if str(value).strip() else None

            report = autocut(
                str(source),
                str(preview_output),
                mode=mode,
                min_silence=optional_float("min_silence"),
                keep_silence=optional_float("keep_silence"),
                threshold_db=optional_float("threshold_db"),
                padding=optional_float("padding"),
            )
            report["uploaded_source"] = str(source)
            report["preview_file"] = str(preview_output)
            report["preview_url"] = f"/preview/{preview_output.name}"
            report["export_suggested_name"] = f"{source.stem}_final_{mode}.mp4"
            self._send(200, body=json.dumps({"ok": True, "report": report}, ensure_ascii=False, indent=2).encode("utf-8"))
        except Exception as exc:
            payload = {
                "ok": False,
                "error": str(exc),
                "trace": traceback.format_exc(limit=3),
            }
            status = 400 if isinstance(exc, ValueError) else 500
            self._send(status, body=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))

    def handle_export(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            preview_file = Path(data.get("preview_file", "")).resolve()
            try:
                preview_file.relative_to(PREVIEWS.resolve())
            except ValueError:
                raise ValueError("预览文件路径不合法")
            if not preview_file.exists():
                raise ValueError("预览视频不存在，请重新生成")

            requested_name = str(data.get("filename") or preview_file.name).strip()
            safe_name = "".join(ch if ch.isalnum() or ch in ".-_ " else "_" for ch in requested_name).strip()
            if not safe_name.lower().endswith(".mp4"):
                safe_name += ".mp4"
            final_path = OUTPUTS / safe_name
            counter = 1
            while final_path.exists():
                final_path = OUTPUTS / f"{Path(safe_name).stem}_{counter}.mp4"
                counter += 1

            shutil.copy2(preview_file, final_path)
            report_file = preview_file.with_suffix(".json")
            final_report = final_path.with_suffix(".json")
            if report_file.exists():
                shutil.copy2(report_file, final_report)
            payload = {
                "ok": True,
                "output": str(final_path),
                "report": str(final_report) if final_report.exists() else None,
            }
            self._send(200, body=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
        except Exception as exc:
            payload = {
                "ok": False,
                "error": str(exc),
                "trace": traceback.format_exc(limit=3),
            }
            status = 400 if isinstance(exc, ValueError) else 500
            self._send(status, body=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))


def main():
    WEB.mkdir(parents=True, exist_ok=True)
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    UPLOADS.mkdir(parents=True, exist_ok=True)
    PREVIEWS.mkdir(parents=True, exist_ok=True)
    server = LargeUploadThreadingHTTPServer(("127.0.0.1", 8787), Handler)
    print("口播自动粗剪工具已启动：http://127.0.0.1:8787")
    print("按 Ctrl+C 停止")
    server.serve_forever()


if __name__ == "__main__":
    main()
