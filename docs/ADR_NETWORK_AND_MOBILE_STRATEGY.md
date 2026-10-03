# Architecture Decision Record: Network & Mobile Multi-Client Strategy

* **Status:** Accepted (Phase 1 V1 Local Scope Accepted; Phase 2/3 Roadmap Defined)
* **Date:** 2026-09-25
* **Deciders:** Architecture & Core Engineering Team
* **Supersedes:** None

---

## 1. Context & Executive Recommendation

Deskmate is designed as a desktop companion agent. However, users desire multi-surface accessibility: interacting with the companion from mobile devices, secondary laptops, or home network tablets while preserving unified memory, personality evolution, and tool orchestration.

### The Decision:
**Do not fork the repository. Do not implement cross-network transport in V1. Design V1 contracts to be "Network-Ready" by default.**

* **No Separate Repository:** The Brain Daemon (`brain/`) remains the authoritative single source of truth for intelligence, state, and memory. Alternate devices connect as presentation clients, exactly like the desktop overlay.
* **Phase 1 (V1 Scope):** Local loopback only (`127.0.0.1:8765`), secured against browser-origin hijacking and DNS rebinding.
* **Phase 2 (V2 Scope):** Private mesh exposure (Tailscale / WireGuard) with bearer token authentication.
* **Phase 3 (V2+ Scope):** Mobile companion client implemented as a Web PWA (Three.js / WebGL) with local audio streaming over secure HTTPS/WSS contexts.

---

## 2. The 3-Phase Evolution Roadmap

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Phase 1: Local Desktop V1 (Current Scope)                                │
│ • Loopback bind (127.0.0.1:8765); single desktop client                 │
│ • Origin-header validation / Host-header check against DNS rebinding     │
│ • IPC envelopes support dual-delivery (local paths + HTTP URLs)          │
│ • Refuses non-loopback bind without auth_token_file configured           │
└──────────────────────────────────────────────────────────────────────────┘
                                     │
                                     ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ Phase 2: Private Network Mesh (Tailscale / WireGuard)                    │
│ • Daemon binds to private mesh IP (100.x.y.z) or LAN                     │
│ • Per-device bearer tokens (Authorization: Bearer <token>)               │
│ • Multi-client routing: direct replies to sender; broadcast sync         │
│ • Unified multi-surface presence reconciliation                          │
│ • Last-writer-wins client_id collision resolution (close code 4409)      │
│ • HTTP media endpoints serve audio waveforms and .glb assets on demand   │
└──────────────────────────────────────────────────────────────────────────┘
                                     │
                                     ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ Phase 3: Mobile Companion Client (Web PWA / Three.js)                    │
│ • Zero-install Progressive Web App running WebGL / WebSockets            │
│ • Served over HTTPS / WSS (via tailscale serve with MagicDNS certs)     │
│ • Browser Auth: first-message token transmission in hello.token payload  │
│ • Media Auth: HMAC-SHA256 signed query parameters (?sig=...&exp=...)     │
│ • Streams binary audio chunks (Opus/PCM) over WebSocket                  │
│ • Foreground operation mode (zero reliance on third-party cloud relays)  │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Protocol Design: Eliminating Shared Filesystem Assumptions

In local V1, passing filesystem paths (`wav_path`, `<deploy_dir>/data/current/<glb>`) is fastest. To prevent breaking contracts when remote clients connect, the protocol defines dual-delivery fields:

| Payload | Local Field | Remote Field | Remote Client Handling |
|---|---|---|---|
| `speech` | `wav_path: str` | `audio_url: str` | Fetches audio via `GET /audio/<utterance_id>.wav` |
| `hot_reload_asset` | `manifest.glb_filename` | `glb_url: str` | Downloads asset via `GET /assets/<filename>` |

* **Capability Negotiation:** Clients declare capabilities during the initial WebSocket handshake (`hello`):
  * `capabilities: ["audio_in", "audio_out", "3d_glb", "shared_filesystem", "desktop_presence"]`
  * If `shared_filesystem` is absent, the client consumes `audio_url` and `glb_url`.
* **Path Traversal Guards:** Media endpoints in the daemon (`GET /audio/{id}.wav` and `GET /assets/{filename}`) strictly validate filenames against path traversal (`..` and path separators rejected).
* **Storage Tiering & Garbage Collection:**
  * Ephemeral audio lives strictly in `workspace_dir` (fast scratch tier), never persistent `deploy_dir`.
  * Audio files have a $600\,\text{s}$ (10 minute) expiration. A background task in `brain/main.py` scans `workspace_dir` every $300\,\text{s}$ (5 minutes) and unlinks files with `ctime > 600s`.
  * 3D models (`.glb`) in `deploy_dir/assets/` follow the LRU version-retention policy (`keep_recent_versions = 3`) rather than a time-based TTL.

---

## 4. Multi-Client Audio & Event Routing Semantics (Phase 2+)

When multiple presentation clients are connected concurrently:

```
                          ┌───────────────────────┐
                          │   Brain Daemon Core   │
                          └───────────┬───────────┘
                                      │
            ┌─────────────────────────┴─────────────────────────┐
            ▼                                                   ▼
┌───────────────────────┐                           ┌───────────────────────┐
│ Desktop Overlay       │                           │ Mobile Client (PWA)   │
│ Client ID: "desktop"  │                           │ Client ID: "mobile"   │
└───────────────────────┘                           └───────────────────────┘
```

1. **Conversational Replies:** Routed exclusively to the `client_id` that submitted the user input (`user_text_input` or `audio_stream_*`), preventing simultaneous audio playback across multiple rooms. Both `speech` (audio/visemes) and conversational text tokens (`chat_response`) target the originating client session.
2. **Speech Acknowledgment:** Only the client that received a `speech` event emits `speech_finished` for it.
3. **Proactive & Autonomous Speech:** Routed to the **most recently active client** (with desktop override preference if both are active simultaneously).
4. **State & Visual Broadcasts:** `hot_reload_asset`, global interaction history (`chat_response` with `broadcast: true`), and `status_update` fan out to all connected clients.
5. **Presence Telemetry:** Window management events (`set_position`) are targeted exclusively to clients declaring the `desktop_presence` capability.
6. **Interaction Concurrency:** Autonomous redesigns and speech are suppressed if *any* connected client reports active interaction mode (`interaction_state: "active"`).
7. **Client ID Collision Resolution:** A unique `client_id` must map to at most one live WebSocket connection. If an incoming `hello` specifies a `client_id` that is already active, the daemon applies **last-writer-wins**: it accepts the new connection and immediately closes the older socket with close code `4409` (`Client ID Replaced`), preventing zombie connection leaks during network handoffs.
8. **Multi-Surface Presence Reconciliation:** In Phase 2+, desktop presence (`desktop_bridge.py`) ceases to be the sole system-wide presence gate:
   * Interaction on *any* client (e.g., active conversation from mobile while desktop is locked) transitions global companion state to `User_Active`.
   * Staged appearance/voice assets are revealed immediately to whichever surface the user is actively using, rather than waiting for an unattended physical desktop to unlock.

---

## 5. Security, Authentication & Browser Realities

### 1. Local Loopback WebSocket Hijacking Defense (V1 Invariant)
* Web browsers do not enforce Cross-Origin Resource Sharing (CORS) on WebSockets. Any malicious website could attempt `new WebSocket("ws://127.0.0.1:8765/ws/overlay")`.
* **Invariant:** The daemon validates the HTTP `Origin` header during WebSocket negotiation. Non-native browser origins are rejected unless explicitly whitelisted in `[server.allowed_origins] = []` (where empty list allows only native clients with no `Origin` header).
* **DNS Rebinding Defense:** The daemon checks the `Host` header on all HTTP and WebSocket requests, ensuring it matches `127.0.0.1:8765` or `localhost:8765`.
* **CSRF Defense:** All REST mutation endpoints (`POST /chat`, `/reflect`, `/appearance/generate`, `/appearance/revert`) enforce `Content-Type: application/json`.

### 2. Phase 2 Mesh Authentication & File-Driven Rotation
* Non-loopback binding requires token authentication. The daemon refuses to start if `host != "127.0.0.1"` without a configured `auth_token_file`.
* Native presentation clients authenticate by supplying the header:
  ```http
  Authorization: Bearer <shared_token>
  ```
* **File-Driven Rotation (No Enterprise Revocation Endpoints):**
  Deskmate is a personal companion; dynamic token revocation endpoints (`POST /auth/revoke`) and session invalidation state machines are rejected as enterprise scope-creep. Token rotation is file-driven:
  1. The user updates the pre-shared secret in `auth_token_file`.
  2. `watchdog` triggers a hot-reload in `brain/config.py`.
  3. The daemon re-reads the active secret and immediately terminates all active WebSocket connections that authenticated with the previous token using close code `4401` (`Unauthorized`).

### 3. Phase 3 Browser WebSocket Authentication & Close Codes
Modern web browsers **cannot set an `Authorization: Bearer` header on native WebSocket connections** (`new WebSocket(...)`).
* Browser-based PWA clients authenticate via **first-message token transmission**:
  ```json
  {
    "type": "hello",
    "timestamp": 1718000000.0,
    "payload": {
      "protocol_version": 1,
      "client_type": "mobile_pwa",
      "client_id": "phone_pixel8",
      "token": "secret_bearer_token_xyz",
      "capabilities": ["audio_in", "audio_out", "3d_glb"]
    }
  }
  ```
* **WebSocket Close Code Table:**
  | Code | Reason | Trigger Condition |
  |---|---|---|
  | `4401` | `Unauthorized` | Token missing, malformed, or invalid signature. |
  | `4408` | `Handshake Timeout` | Browser client failed to send `hello` within 3.0 seconds. |
  | `4409` | `Client ID Replaced` | Duplicate `client_id` connected (last-writer-wins). |

### 4. Phase 3 Media Endpoint Authentication (HMAC Signed URLs)
Standard browser media tags (`<audio src="...">`) and 3D asset loaders (`GLTFLoader.load(...)`) cannot send custom HTTP headers without complex service-worker interception.
* Media endpoints (`/audio/{id}` and `/assets/{file}`) support **HMAC-SHA256 signed query parameters**:
  $$\text{Signature} = \text{HMAC-SHA256}(\text{key}=\text{token}, \text{data}=\text{path} + \text{exp})$$
* URLs are emitted with short-lived expiration: `/audio/utt_123.wav?exp=1718000060&sig=4f1a...`
* Expiration duration is capped at **60 seconds**, eliminating credential leakage via browser history or server access logs.
* **Server Verification Logic (Python):**
  ```python
  import time
  import hmac
  import hashlib

  def verify_signed_url(path: str, exp_str: str, sig_hex: str, token: str) -> bool:
      try:
          if time.time() > float(exp_str):
              return False  # Expired
      except ValueError:
          return False
      msg = f"{path}{exp_str}".encode("utf-8")
      expected = hmac.new(token.encode("utf-8"), msg, hashlib.sha256).hexdigest()
      return hmac.compare_digest(expected, sig_hex)
  ```

### 5. PWA Secure Context Requirement
* Mobile browsers strictly require a **Secure Context (HTTPS / WSS)** to access device microphones via `navigator.mediaDevices.getUserMedia`.
* Accessing `http://100.x.y.z:8765` over plain Tailscale will fail microphone permission checks. Phase 3 deployments must enable HTTPS via `tailscale serve` (using automatic MagicDNS TLS certificates) or a local reverse proxy (Caddy / Nginx).

### 6. Privacy Posture
* Mobile companion clients run in the foreground over private mesh networks without public cloud relay servers (APNs/FCM relays are excluded from core architecture).

---

## 6. Actionable Exit Criteria & Testing Matrix

* **Phase 1 (V1 Scope):**
  * Daemon passes `Origin` header validation (rejects `Origin: http://evil.com` with `403 Forbidden`).
  * Daemon passes `Host` header check (rejects `Host: attacker.com` with `400 Bad Request`).
  * Daemon refuses loopback bind when `host != "127.0.0.1"` and `auth_token_file` is unset.
  * Dual-path fields (`audio_url`, `glb_url`) exist in event schemas.
* **Phase 2:**
  * Multi-client handshake (`client_id`, `capabilities`) routes replies exclusively to originating client.
  * Authenticated remote desktop overlay connects across Tailscale using bearer headers.
  * Last-writer-wins terminates stale reconnects cleanly with close code `4409`.
  * Background GC task scans `workspace_dir` every 5 minutes and cleans files $>600\,\text{s}$ old.
* **Phase 3:**
  * Browser PWA connects over HTTPS/WSS; daemon enforces 3.0s handshake timeout (drops with `4408`).
  * Invalid token on `hello` closes socket with `4401`.
  * Server validates HMAC-SHA256 query parameters on `/audio/` and `/assets/` and rejects expired URLs.
  * Mobile client streams microphone audio and renders 3D avatar scene in browser context.