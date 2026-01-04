# MacroLens

**A production-ready FastMCP server that connects OpenAI's ChatGPT directly to user-owned Google Sheets.**

MacroLens allows users to log meals naturally via text or photo analysis ("Log my lunch") and retrieve historical nutrition data ("What did I eat this week?") without their data ever leaving their own private infrastructure.

---

## Key Features

* **FastMCP Architecture:** Built using the Model Context Protocol (MCP) to expose tools natively to ChatGPT.
* **"Bring Your Own Database" (BYOD):** Zero-retention architecture. All data is written directly to the user's personal Google Sheet via **Google Sheets API v4**.
* **OAuth 2.0 Identity:** Implements a full OAuth handshake flow to securely authenticate users and manage scoped permissions (`spreadsheets`, `drive.file`).
* **Multimodal Analysis:** leverages OpenAI's vision capabilities to analyze food photos and automatically extract macro data (Calories, Protein, Carbs, Fat).
* **Custom UI Widgets:** Uses **Skybridge** technology to render native HTML/JS widgets inside the chat interface for a polished "App-like" experience.
* **Self-Hosted Infrastructure:** Deployed on a Linux VPS using **Systemd** for process management and **Caddy** as a reverse proxy with automatic HTTPS (Let's Encrypt).

---

## Tech Stack

* **Language:** Python 3.10+
* **Framework:** `fastmcp` (Model Context Protocol SDK)
* **Database:** Google Sheets (via `gspread`)
* **Auth:** Google OAuth 2.0 (Identity & Access Management)
* **Infrastructure:** Ubuntu VPS, Caddy Web Server, Systemd
* **Security:** Environment-based config (`.env`), Scoped Permissions

---

## Project Structure

```text
MacroLens/
├── assets/
│   └── widget.html       # Custom UI widget for the ChatGPT interface
├── main.py               # Application entry point (MCP Server & Tool Logic)
├── .env                  # Secrets (API Keys, Client IDs - Not committed)
├── requirements.txt      # Python dependencies
└── README.md             # Documentation