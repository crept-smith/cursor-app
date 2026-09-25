#!/usr/bin/env python3
"""A small, local Cursor-like coding assistant.

Set OPENROUTER_API_KEY before using the chat feature.  The application keeps all
filesystem operations inside ./workspace and asks for approval before changing it.
"""

import asyncio
import json
import os
import queue
import threading
import urllib.parse
import urllib.request
import urllib.error
import html
import re
import shutil
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# Paste your OpenRouter key here, or set it in the environment.
OPENROUTER_API_KEY = "YOUR_KEY_HERE"

FREE_MODELS = [
    "openrouter/free",
    "cohere/north-mini-code:free",
    "qwen/qwen3.8-27b:free",
    "google/gemma-4-31b-it:free",
    "deepseek/deepseek-r1:free",
    "meta-llama/llama-3.3-70b-instruct:free",
]

WORKSPACE = (Path.cwd() / "workspace").resolve()
WORKSPACE.mkdir(parents=True, exist_ok=True)
API_URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM_PROMPT = """You are a careful local coding agent. The workspace root is ./workspace.
Reply with exactly one JSON object and no markdown fences. For a normal answer use:
{"tool":"final_response","parameters":{"message":"..."}}
To act, use one of these schemas:
{"tool":"create_folder","parameters":{"path":"relative/path"}}
{"tool":"create_file","parameters":{"path":"relative/file.py","content":"full content"}}
{"tool":"edit_file","parameters":{"path":"relative/file.py","content":"full replacement content"}}
{"tool":"delete_file","parameters":{"path":"relative/path"}}
{"tool":"web_search","parameters":{"query":"search terms"}}
{"tool":"fetch_url","parameters":{"url":"https://example.com","path":"optional/output.txt"}}
Filesystem paths must be relative to ./workspace. Never request shell commands, absolute paths,
secrets, or operations outside the workspace. Ask for clarification through final_response when needed.
"""


def safe_path(value: str) -> Path:
    """Resolve a user/model path and reject traversal outside WORKSPACE."""
    raw = Path(str(value).strip())
    if raw.is_absolute():
        raise ValueError("absolute paths are not allowed")
    target = (WORKSPACE / raw).resolve()
    if target != WORKSPACE and WORKSPACE not in target.parents:
        raise ValueError("path escapes the workspace")
    return target


def relative_path(path: Path) -> str:
    return str(path.relative_to(WORKSPACE)).replace(os.sep, "/")


def strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\\s*", "", text, flags=re.I)
        text = re.sub(r"\\s*```$", "", text)
    return text.strip()


def extract_json(text: str) -> dict:
    cleaned = strip_code_fences(text)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("model did not return a JSON object")
        value = json.loads(cleaned[start:end + 1])
    if not isinstance(value, dict) or not isinstance(value.get("tool"), str):
        raise ValueError("invalid tool response")
    value.setdefault("parameters", {})
    return value


def web_search(query: str) -> str:
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        source = response.read().decode("utf-8", "replace")
    results = []
    for match in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', source, re.I | re.S):
        link = html.unescape(match.group(1))
        title = re.sub(r"<[^>]+>", "", match.group(2))
        title = html.unescape(re.sub(r"\\s+", " ", title)).strip()
        results.append({"title": title, "url": link})
        if len(results) >= 8:
            break
    return json.dumps({"query": query, "results": results}, ensure_ascii=False, indent=2)


def fetch_url(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=25) as response:
        data = response.read(2_000_000).decode("utf-8", "replace")
    # Dependency-free text extraction; scripts/styles are not useful to the model.
    data = re.sub(r"<script\\b[^>]*>.*?</script>", " ", data, flags=re.I | re.S)
    data = re.sub(r"<style\\b[^>]*>.*?</style>", " ", data, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", data)
    return html.unescape(re.sub(r"\\s+", " ", text)).strip()[:100_000]


class Approval:
    def __init__(self, tool: str, params: dict):
        self.tool, self.params = tool, params
        self.event = threading.Event()
        self.accepted = False


class CursorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Local Cursor Clone")
        self.root.geometry("1100x720")
        self.root.minsize(760, 500)
        self.events = queue.Queue()
        self.history = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.pending = None
        self.busy = False
        self._build_ui()
        self.refresh_tree()
        self.root.after(100, self._drain_events)

    def _build_ui(self):
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)
        left = ttk.Frame(self.root, padding=8)
        left.grid(row=0, column=0, sticky="nsew")
        left.rowconfigure(1, weight=1)
        ttk.Label(left, text="WORKSPACE", font=("TkDefaultFont", 10, "bold")).grid(sticky="w")
        self.tree = ttk.Treeview(left, show="tree")
        self.tree.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        ttk.Button(left, text="Refresh", command=self.refresh_tree).grid(row=2, column=0, sticky="ew", pady=(8, 0))

        main = ttk.Frame(self.root, padding=(0, 8, 8, 8))
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)
        ttk.Label(main, text="Chat", font=("TkDefaultFont", 14, "bold")).grid(row=0, column=0, sticky="w")
        self.chat = tk.Text(main, wrap="word", state="disabled", bg="#17191c", fg="#e8e8e8", insertbackground="white")
        self.chat.grid(row=1, column=0, sticky="nsew", pady=(8, 8))
        self.chat.tag_configure("user", foreground="#8ecbff")
        self.chat.tag_configure("assistant", foreground="#c7f9cc")
        self.chat.tag_configure("error", foreground="#ff8c8c")

        approval = ttk.Frame(main)
        approval.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        approval.columnconfigure(0, weight=1)
        self.approval_label = ttk.Label(approval, text="", foreground="#9a5b00")
        self.approval_label.grid(row=0, column=0, sticky="w")
        self.approve_btn = ttk.Button(approval, text="Approve", command=lambda: self._resolve_approval(True), state="disabled")
        self.approve_btn.grid(row=0, column=1, padx=4)
        self.decline_btn = ttk.Button(approval, text="Decline", command=lambda: self._resolve_approval(False), state="disabled")
        self.decline_btn.grid(row=0, column=2)

        bottom = ttk.Frame(main)
        bottom.grid(row=3, column=0, sticky="ew")
        bottom.columnconfigure(0, weight=1)
        self.prompt = tk.Text(bottom, height=4, wrap="word")
        self.prompt.grid(row=0, column=0, sticky="ew")
        self.prompt.bind("<Control-Return>", lambda _: self.submit())
        self.send_btn = ttk.Button(bottom, text="Send (Ctrl+Enter)", command=self.submit)
        self.send_btn.grid(row=0, column=1, padx=(8, 0), sticky="ns")

    def append(self, text: str, tag=None):
        self.chat.configure(state="normal")
        self.chat.insert("end", text + "\\n\\n", tag)
        self.chat.see("end")
        self.chat.configure(state="disabled")

    def refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        root_id = self.tree.insert("", "end", text="workspace/", open=True)
        self._add_tree(root_id, WORKSPACE)

    def _add_tree(self, parent, folder: Path):
        try:
            entries = sorted(folder.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return
        for item in entries:
            if item.name.startswith("."):
                continue
            node = self.tree.insert(parent, "end", text=item.name + ("/" if item.is_dir() else ""), open=False)
            if item.is_dir():
                self._add_tree(node, item)

    def submit(self):
        if self.busy:
            return
        text = self.prompt.get("1.0", "end").strip()
        if not text:
            return
        self.prompt.delete("1.0", "end")
        self.append("You: " + text, "user")
        self.history.append({"role": "user", "content": text})
        self.busy = True
        self.send_btn.configure(state="disabled")
        threading.Thread(target=self._agent_thread, daemon=True).start()

    def _agent_thread(self):
        try:
            asyncio.run(self._agent_loop())
        except Exception as exc:
            self.events.put(("message", "Error: " + str(exc), "error"))
        finally:
            self.events.put(("busy", False))

    async def _agent_loop(self):
        for _ in range(12):
            raw = await asyncio.to_thread(self._request_with_fallback)
            try:
                action = extract_json(raw)
            except Exception as exc:
                self.events.put(("message", "Model response error: " + str(exc), "error"))
                return
            tool, params = action["tool"], action.get("parameters", {})
            if tool == "final_response":
                message = str(params.get("message", "Done."))
                self.history.append({"role": "assistant", "content": message})
                self.events.put(("message", "Assistant: " + message, "assistant"))
                return
            if tool in {"create_folder", "create_file", "edit_file", "delete_file"}:
                result = await asyncio.to_thread(self._approved_operation, tool, params)
            elif tool == "web_search":
                try:
                    result = await asyncio.to_thread(web_search, str(params.get("query", "")))
                except Exception as exc:
                    result = "web_search failed: " + str(exc)
            elif tool == "fetch_url":
                try:
                    result = await asyncio.to_thread(fetch_url, str(params.get("url", "")))
                    output = params.get("path")
                    if output:
                        # Saving fetched data is itself an approval-protected operation.
                        result = await asyncio.to_thread(self._approved_operation, "create_file", {"path": output, "content": result})
                    else:
                        result = result[:100_000]
                except Exception as exc:
                    result = "fetch_url failed: " + str(exc)
            else:
                result = "Unknown tool: " + tool
            self.history.append({"role": "assistant", "content": json.dumps(action)})
            self.history.append({"role": "user", "content": "Tool result: " + result})
        self.events.put(("message", "Stopped after the maximum number of agent steps.", "error"))

    def _request_with_fallback(self) -> str:
        if not OPENROUTER_API_KEY or OPENROUTER_API_KEY == "YOUR_KEY_HERE":
            raise RuntimeError("Set OPENROUTER_API_KEY in main.py before using the chat")
        headers = {"Authorization": "Bearer " + OPENROUTER_API_KEY, "Content-Type": "application/json", "HTTP-Referer": "http://localhost"}
        messages = self.history[-30:]
        last_error = None
        for model in FREE_MODELS:
            payload = {"model": model, "messages": messages, "temperature": 0.2}
            try:
                request = urllib.request.Request(API_URL, data=json.dumps(payload).encode(), headers=headers, method="POST")
                with urllib.request.urlopen(request, timeout=90) as response:
                    data = json.loads(response.read().decode("utf-8"))
                return data["choices"][0]["message"]["content"]
            except Exception as exc:
                last_error = exc
        raise RuntimeError("all free models failed: " + str(last_error))

    def _approved_operation(self, tool: str, params: dict) -> str:
        approval = Approval(tool, params)
        self.events.put(("approval", approval))
        approval.event.wait()
        if not approval.accepted:
            return "User declined the requested operation. Do not retry it unless asked."
        try:
            path = safe_path(str(params.get("path", "")))
            if tool == "create_folder":
                path.mkdir(parents=True, exist_ok=True)
                result = "created folder " + relative_path(path)
            elif tool in {"create_file", "edit_file"}:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(params.get("content", "")), encoding="utf-8")
                result = ("created file " if tool == "create_file" else "edited file ") + relative_path(path)
            else:
                if not path.exists():
                    result = "path did not exist: " + relative_path(path)
                elif path.is_dir():
                    shutil.rmtree(path)
                    result = "deleted directory " + relative_path(path)
                else:
                    path.unlink()
                    result = "deleted file " + relative_path(path)
            self.events.put(("refresh",))
            return result
        except Exception as exc:
            return "filesystem operation failed: " + str(exc)

    def _resolve_approval(self, accepted: bool):
        if self.pending:
            self.pending.accepted = accepted
            self.pending.event.set()
            self.pending = None
            self.approval_label.configure(text="")
            self.approve_btn.configure(state="disabled")
            self.decline_btn.configure(state="disabled")

    def _drain_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "message":
                    self.append(event[1], event[2])
                elif kind == "busy":
                    self.busy = event[1]
                    self.send_btn.configure(state="disabled" if self.busy else "normal")
                elif kind == "refresh":
                    self.refresh_tree()
                elif kind == "approval":
                    approval = event[1]
                    self.pending = approval
                    details = json.dumps(approval.params, ensure_ascii=False)
                    self.approval_label.configure(text=f"Approve {approval.tool}: {details[:240]}")
                    self.approve_btn.configure(state="normal")
                    self.decline_btn.configure(state="normal")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)


if __name__ == "__main__":
    app_root = tk.Tk()
    CursorApp(app_root)
    app_root.mainloop()
