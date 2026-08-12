'''
Author: wilbur
Version: 1.0
Date: 2026-08-11
Description: streamingLatencyFixPlan T1.3 验证：本地 chunked SSE 服务每 ~120ms 发一小帧 data，
             用真实 urllib 响应驱动 chatCompletionsAdapter.iterSseData，
             断言分次增量交付（read1 不阻塞凑批），且 payload 内容完整、[DONE] 正常收尾。
'''

from __future__ import annotations

import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

from flamingoAgents.models.chatCompletions import chatCompletionsAdapter

FRAME_COUNT = 8
FRAME_INTERVAL = 0.12


class sseHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        # 不显式设 Content-Length，HTTP/1.1 下用 chunked 传输
        self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()
        self.wfile.flush()
        for index in range(FRAME_COUNT):
            payload = f'data: {json.dumps({"choices": [{"delta": {"content": f"chunk{index}"}}]})}\n\n'
            self.send_chunk(payload.encode('utf-8'))
            time.sleep(FRAME_INTERVAL)
        self.send_chunk(b'data: [DONE]\n\n')
        self.send_chunk(b'')  # 结束 chunked 流

    def send_chunk(self, data: bytes):
        self.wfile.write(f'{len(data):x}\r\n'.encode('ascii') + data + b'\r\n')
        self.wfile.flush()

    def log_message(self, *args):
        pass


def main() -> None:
    server = HTTPServer(('127.0.0.1', 0), sseHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    adapter = chatCompletionsAdapter.__new__(chatCompletionsAdapter)  # 仅测 iterSseData，无需 config/auth
    request = urllib.request.Request(f'http://127.0.0.1:{port}/chat/completions', data=b'{}', method='POST')

    arrivalTimes: list[float] = []
    payloads: list[str] = []
    startedAt = time.monotonic()
    with urllib.request.urlopen(request, timeout=10) as response:
        assert hasattr(response, 'read1'), 'HTTPResponse 应有 read1'
        for payload in adapter.iterSseData(response):
            arrivalTimes.append(time.monotonic() - startedAt)
            payloads.append(payload)
    server.shutdown()

    dataPayloads = [p for p in payloads if p != '[DONE]']
    assert len(dataPayloads) == FRAME_COUNT, f'应收 {FRAME_COUNT} 帧 data，实收 {len(dataPayloads)}'
    for index, payload in enumerate(dataPayloads):
        assert f'chunk{index}' in payload, f'第 {index} 帧内容错序/缺失: {payload}'

    gaps = [arrivalTimes[i + 1] - arrivalTimes[i] for i in range(len(arrivalTimes) - 1)]
    firstArrival = arrivalTimes[0]
    maxGap = max(gaps) if gaps else 0.0
    total = arrivalTimes[-1]
    print(f'首帧到达: {firstArrival * 1000:.0f}ms（发送间隔 {FRAME_INTERVAL * 1000:.0f}ms）')
    print(f'相邻帧最大间隔: {maxGap * 1000:.0f}ms；总跨度: {total * 1000:.0f}ms')
    # read(4096) 行为：首帧≈总时长一次性到达；read1：首帧≈首个发送间隔，间隔≈发送间隔
    assert firstArrival < FRAME_INTERVAL * 5, f'首帧到达过晚（{firstArrival:.2f}s），疑似仍在凑批'
    assert total > FRAME_INTERVAL * (FRAME_COUNT - 1) * 0.8, f'帧被一次性吐出（总跨度 {total:.2f}s 过短）'
    print('T1.3 通过：read1 分次增量交付，节奏与 server 发送一致；[DONE] 收尾正常。')


if __name__ == '__main__':
    main()
