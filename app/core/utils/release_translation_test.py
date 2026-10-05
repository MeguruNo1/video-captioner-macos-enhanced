"""Offline translation protocol acceptance; this never calls an external LLM."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def verify_translation_protocol(output: Path) -> dict:
    from app.core.bk_asr.asr_data import ASRData, ASRDataSeg
    from app.core.subtitle_processor.translate import OpenAITranslator

    requests_seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests_seen.append(self.path)
            content = json.dumps({"1": "你好，世界。", "2": "早上好。"}, ensure_ascii=False)
            payload = {"id": "functional-test", "object": "chat.completion", "created": 1,
                       "model": "functional-test", "choices": [{"index": 0, "finish_reason": "stop",
                       "message": {"role": "assistant", "content": content}}]}
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    overrides = {"OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
                 "OPENAI_API_KEY": "functional-test", "NO_PROXY": "127.0.0.1,localhost",
                 "ALL_PROXY": "socks5://127.0.0.1:9"}
    original = {key: os.environ.get(key) for key in overrides}
    translator = None
    try:
        os.environ.update(overrides)
        translator = OpenAITranslator(thread_num=1, batch_num=2, model="functional-test",
                                      use_cache=False, batch_context_enabled=False, timeout=10)
        source = ASRData([ASRDataSeg("Hello world.", 0, 2000),
                          ASRDataSeg("Good morning.", 2000, 4000)])
        result = translator.translate_subtitle(source)
        if [segment.translated_text for segment in result.segments] != ["你好，世界。", "早上好。"]:
            raise RuntimeError("Translation response mapping failed")
        if [(segment.start_time, segment.end_time) for segment in result.segments] != [(0, 2000), (2000, 4000)]:
            raise RuntimeError("Translation changed original timestamps")
        if requests_seen != ["/v1/chat/completions"]:
            raise RuntimeError("Unexpected translation HTTP requests")
        result.to_srt(save_path=str(output / "translation.srt"))
        return {"status": "passed", "scope": "Local mock OpenAI-compatible HTTP endpoint",
                "checks": ["SDK initialization with SOCKS proxy configured", "Actual HTTP request and response parsing",
                           "Translation mapping, original timestamps and bilingual SRT export"]}
    finally:
        if translator is not None:
            translator.stop()
            translator.client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
