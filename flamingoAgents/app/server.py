'''
Author: wilbur
Version: 1.2
Date: 2026-07-01
Description: Provides local HTTP chat and confirmation endpoints with unified model config loading.
'''

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from flamingoAgents.core.agent import agent
from flamingoAgents.utils.debug import debugConsole
from flamingoAgents.models.registry import loadModelConfig
from flamingoAgents.models.openai import openaiAdapter
from flamingoAgents.tools.registry import createDefaultRegistry


def resultToDict(result) -> dict[str, Any]:
    data = {
        'sessionId': result.sessionId,
        'status': result.status,
        'message': result.message,
    }
    if result.status == 'confirmationRequired':
        data.update({
            'confirmationId': result.confirmationId,
            'reason': result.reason,
            'commandPreview': result.commandPreview,
        })
    return data


def makeHttpHandler(agent: agent):
    class agentHttpHandler(BaseHTTPRequestHandler):
        server_version = 'FlamingoAgents/0.1'

        def do_POST(self) -> None:
            if getattr(agent, 'debugConsole', None):
                agent.debugConsole.debug(f'HTTP POST path={self.path}')
            if self.path == '/chat':
                self.handleChat()
                return
            if self.path == '/confirm':
                self.handleConfirm()
                return
            self.respondJson(404, {'status': 'error', 'message': '未知路径。'})

        def handleChat(self) -> None:
            payload = self.readJson()
            message = payload.get('message')
            sessionId = payload.get('sessionId')
            if not isinstance(message, str) or not message.strip():
                self.respondJson(400, {'status': 'error', 'message': 'message 必须是非空字符串。'})
                return
            if sessionId is not None and not isinstance(sessionId, str):
                self.respondJson(400, {'status': 'error', 'message': 'sessionId 必须是字符串。'})
                return
            if getattr(agent, 'debugConsole', None):
                agent.debugConsole.debug(f'HTTP chat sessionId={sessionId or "<new>"} chars={len(message)}')
            result = agent.runUserMessage(message, sessionId=sessionId)
            statusCode = 200 if result.status != 'error' else 500
            self.respondJson(statusCode, resultToDict(result))

        def handleConfirm(self) -> None:
            payload = self.readJson()
            sessionId = payload.get('sessionId')
            confirmationId = payload.get('confirmationId')
            approved = payload.get('approved')
            if not isinstance(sessionId, str) or not sessionId:
                self.respondJson(400, {'status': 'error', 'message': 'sessionId 必须是非空字符串。'})
                return
            if not isinstance(confirmationId, str) or not confirmationId:
                self.respondJson(400, {'status': 'error', 'message': 'confirmationId 必须是非空字符串。'})
                return
            if not isinstance(approved, bool):
                self.respondJson(400, {'status': 'error', 'message': 'approved 必须是布尔值。'})
                return
            if getattr(agent, 'debugConsole', None):
                agent.debugConsole.debug(f'HTTP confirm sessionId={sessionId} confirmationId={confirmationId} approved={approved}')
            result = agent.continueConfirmation(sessionId, confirmationId, approved)
            statusCode = 200 if result.status != 'error' else 500
            self.respondJson(statusCode, resultToDict(result))

        def readJson(self) -> dict[str, Any]:
            length = int(self.headers.get('Content-Length', '0'))
            rawData = self.rfile.read(length).decode('utf-8') if length else '{}'
            try:
                payload = json.loads(rawData)
            except json.JSONDecodeError:
                return {}
            return payload if isinstance(payload, dict) else {}

        def respondJson(self, statusCode: int, payload: dict[str, Any]) -> None:
            responseBytes = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(statusCode)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(responseBytes)))
            self.end_headers()
            self.wfile.write(responseBytes)

        def log_message(self, format: str, *args) -> None:
            if getattr(agent, 'debugConsole', None) and agent.debugConsole.isDebug:
                super().log_message(format, *args)

    return agentHttpHandler


def buildAgent(debugEnabled: bool, workDir: Path) -> agent:
    printer = debugConsole(debugEnabled)
    config = loadModelConfig()
    adapter = openaiAdapter(config, printer)
    registry = createDefaultRegistry()
    return agent(
        modelAdapter=adapter,
        registry=registry,
        workDir=workDir,
        logDir=workDir / '.agentLogs',
        debugConsole=printer,
        confirmDeletion=None,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description='Flamingo Agents HTTP 服务')
    parser.add_argument('--debug', action='store_true', help='启用详细调试输出')
    parser.add_argument('--host', default='127.0.0.1', help='监听地址')
    parser.add_argument('--port', type=int, default=8765, help='监听端口')
    parser.add_argument('--work-dir', default='.', help='工具执行工作目录')
    args = parser.parse_args()

    workDir = Path(args.work_dir).resolve()
    agent = buildAgent(args.debug, workDir)
    handler = makeHttpHandler(agent)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f'HTTP 服务已启动：http://{args.host}:{args.port}')
    server.serve_forever()


if __name__ == '__main__':
    main()
