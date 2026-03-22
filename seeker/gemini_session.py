"""Phase 2 — Gemini Multimodal Live API WebSocket session management."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
from typing import Any, Protocol

import websockets
from websockets.asyncio.client import ClientConnection

from seeker.config import GeminiConfig

log = logging.getLogger(__name__)
logging.getLogger("websockets").setLevel(logging.WARNING)


class ToolHandler(Protocol):
    """Protocol for handling Gemini tool calls locally."""

    async def handle(self, name: str, args: dict[str, Any]) -> dict[str, Any]: ...


class GeminiSession:
    """Manages the WebSocket lifecycle with the Gemini Multimodal Live API.

    Responsibilities:
      - Connect, send setup payload, and stream audio
      - Receive and dispatch tool calls
      - Handle GoAway / session resumption
      - Reconnect with exponential backoff
    """

    def __init__(
        self,
        config: GeminiConfig,
        audio_queue: asyncio.Queue[bytes],
        tool_handler: ToolHandler,
    ) -> None:
        self.config = config
        self.audio_queue = audio_queue
        self.tool_handler = tool_handler

        self._ws: ClientConnection | None = None
        self._resumption_handle: str | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the WSS connection to Gemini."""
        url = f"{self.config.endpoint}?key={self.config.api_key}"
        self._ws = await websockets.connect(url)
        self._running = True
        log.info("Gemini WebSocket connected.")

    async def disconnect(self) -> None:
        """Gracefully close the WebSocket."""
        self._running = False
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        log.info("Gemini WebSocket disconnected.")

    async def send_setup(self, system_prompt: str, tools: list[dict]) -> None:
        """Transmit the BidiGenerateContentSetup payload."""
        assert self._ws is not None

        setup_payload: dict[str, Any] = {
            "setup": {
                "model": self.config.model,
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "temperature": 0.0,
                    "speechConfig": {
                        "voiceConfig": {
                            "prebuiltVoiceConfig": {
                                "voiceName": "Puck"
                            }
                        }
                    },
                },
                "systemInstruction": {
                    "parts": [{"text": system_prompt}],
                },
                "tools": [{"functionDeclarations": tools}],
                "inputAudioTranscription": {},
                "realtimeInputConfig": {},
                "contextWindowCompression": {
                    "slidingWindow": {
                        "targetTokens": self.config.target_tokens,
                    },
                },
                "sessionResumption": {
                    "handle": self._resumption_handle,
                },
            }
        }

        await self._ws.send(json.dumps(setup_payload))
        log.info("Setup payload sent (model=%s).", self.config.model)
        log.debug("System prompt:\n%s", system_prompt)

        # Wait for setupComplete acknowledgment
        response = json.loads(await self._ws.recv())
        if "setupComplete" in response:
            log.info("Setup acknowledged by server.")
        else:
            log.warning("Unexpected setup response: %s", response)

    # ------------------------------------------------------------------
    # Audio streaming (egress)
    # ------------------------------------------------------------------

    async def stream_audio(self) -> None:
        """Continuously read from the audio queue and send to Gemini."""
        assert self._ws is not None
        while self._running:
            try:
                pcm_bytes = await asyncio.wait_for(self.audio_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            encoded = base64.b64encode(pcm_bytes).decode("ascii")
            message = {
                "realtimeInput": {
                    "mediaChunks": [
                        {
                            "mimeType": "audio/pcm;rate=16000",
                            "data": encoded,
                        }
                    ]
                }
            }
            await self._ws.send(json.dumps(message))

    # ------------------------------------------------------------------
    # Message reception (ingress)
    # ------------------------------------------------------------------

    async def receive_messages(self) -> None:
        """Listen for server messages and dispatch accordingly."""
        assert self._ws is not None
        silence_seconds = 0
        while self._running:
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
                silence_seconds = 0
            except asyncio.TimeoutError:
                silence_seconds += 10
                log.warning("No message from Gemini for %ds", silence_seconds)
                continue
            except websockets.ConnectionClosed:
                log.warning("WebSocket connection closed unexpectedly.")
                await self._reconnect()
                return

            message = json.loads(raw)

            if "toolCall" in message:
                await self._handle_tool_call(message["toolCall"])
            elif "goAway" in message:
                await self._handle_go_away(message["goAway"])
            elif "sessionResumptionUpdate" in message:
                handle = message["sessionResumptionUpdate"].get("newHandle")
                if handle:
                    self._resumption_handle = handle
                    log.debug("Session resumption handle updated.")
            elif "serverContent" in message:
                content = message["serverContent"]
                # Input transcription — what the model hears
                if "inputTranscription" in content:
                    text = content["inputTranscription"].get("text", "")
                    if text.strip():
                        log.info("Hearing: %s", text.strip())
                # Model turn parts (text, thought, audio)
                parts = content.get("modelTurn", {}).get("parts", [])
                for part in parts:
                    if "text" in part:
                        log.debug("Gemini: %s", part["text"])
                    if "thought" in part:
                        log.debug("Gemini thought: %s", part["thought"])
                    if "inlineData" in part:
                        log.debug("Gemini sent audio data content.")
            else:
                # Log other message types briefly for visibility
                keys = list(message.keys())
                log.info("Received message types: %s", keys)

    # ------------------------------------------------------------------
    # Tool call handling
    # ------------------------------------------------------------------

    async def _handle_tool_call(self, tool_call: dict) -> None:
        """Process a tool call from Gemini and send the response back."""
        for fc in tool_call.get("functionCalls", []):
            call_id = fc["id"]
            name = fc["name"]
            args = fc.get("args", {})

            log.info("Gemini tool call: %s(%s)", name, args)

            result = await self.tool_handler.handle(name, args)

            await self._send_tool_response(call_id, name, result)

    async def _send_tool_response(
        self, call_id: str, name: str, result: dict[str, Any]
    ) -> None:
        """Acknowledge a tool call back to Gemini."""
        assert self._ws is not None
        response = {
            "toolResponse": {
                "functionResponses": [
                    {
                        "id": call_id,
                        "name": name,
                        "response": result,
                    }
                ]
            }
        }
        await self._ws.send(json.dumps(response))
        log.debug("Tool response sent for id=%s", call_id)

    # ------------------------------------------------------------------
    # GoAway / reconnection
    # ------------------------------------------------------------------

    async def _handle_go_away(self, go_away: dict) -> None:
        time_left = go_away.get("timeLeft")
        log.warning("GoAway received — time remaining: %s. Reconnecting...", time_left)
        await self._reconnect()

    async def _reconnect(self) -> None:
        """Reconnect with exponential backoff, preserving the session handle."""
        await self.disconnect()
        attempt = 0
        while True:
            try:
                await self.connect()
                log.info("Reconnected (attempt %d) with session resumption.", attempt + 1)
                return
            except (ConnectionError, TimeoutError, OSError) as exc:
                delay = min(
                    2**attempt + random.uniform(0, 1),
                    self.config.reconnect_max_backoff_s,
                )
                log.warning("Reconnect attempt %d failed: %s. Retrying in %.1fs", attempt + 1, exc, delay)
                await asyncio.sleep(delay)
                attempt += 1
