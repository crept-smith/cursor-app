#!/usr/bin/env python3
"""Standalone Tkinter Cursor-like local coding assistant."""
import asyncio
import html
import json
import os
import queue
import re
import shutil
import threading
import tkinter as tk
import urllib.parse
import urllib.request
from pathlib import Path
from tkinter import ttk

OPENROUTER_API_KEY = "YOUR_KEY_HERE"
FREE_MODELS = [
    "openrouter/free",
    "cohere/north-mini-code:free",
    "qwen/qwen3.8-27b:free",
    "google/gemma-4-31b-it:free",
    "deepseek/deepseek-r1:free",
    "meta-llama/llama-3.3-70b-instruct:free",
]
API_URL = "https://openrouter.ai/api/v1/chat/completions"
WORKSPACE = (Path.cwd() / "workspace").resolve()
WORKSPACE.mkdir(parents=True, exist_ok=True)
SYSTEM_PROMPT = '''You are a careful local coding agent. Reply with exactly one JSON object,
not markdown. JSON strings must be valid JSON: escape every backslash as \\\\, every quote as \\",
and use \\n for newlines inside strings. Use one of these tools:
{"tool":"final_response","parameters":{"message":"..."}}
{"tool":"create_folder","parameters":{"path":"relative/path"}}
{"tool":"create_file","parameters":{"path":"relative/file","content":"full content"}}
{"tool":"edit_file","parameters":{"path":"relative/file","content":"full content"}}
{"tool":"delete_file","parameters":{"path":"relative/path"}}
{"tool":"web_search","parameters":{"query":"query"}}
{"tool":"fetch_url","parameters":{"url":"https://example.com","path":"optional/file.txt"}}
All filesystem paths are relative to ./workspace. Never use absolute paths or traversal.'''


def safe_path(value):
    p = Path(str(value).strip())
    if p.is_absolute():
        raise ValueError("absolute paths are not allowed")
    result = (WORKSPACE / p).resolve()
    if result != WORKSPACE and WORKSPACE not in result.parents:
        raise ValueError("path escapes workspace")
    return result


def repair_json_escapes(text):
    """Preserve literal programming backslashes in otherwise JSON-like output."""
    valid = set('"\\/bfnrtu')
    out, i = [], 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt in valid:
                out.append(text[i:i + 2])
                i += 2
                continue
            out.append("\\\\")
            i += 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def parse_model_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\\s*", "", text, flags=re.I)
        text = re.sub(r"\\s*```$", "", text).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("model did not return a JSON object")
        candidate = text[start:end + 1]
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            value = json.loads(repair_json_escapes(candidate))
    if not isinstance(value, dict) or not isinstance(value.get("tool"), str):
        raise ValueError("invalid tool response")
    return value


def fetch_url(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as response:
        data = response.read(2_000_000).decode("utf-8", "replace")
    data = re.sub(r"<script\\b[^>]*>.*?</script>", " ", data, flags=re.I | re.S)
    data = re.sub(r"<style\\b[^>]*>.*?</style>", " ", data, flags=re.I | re.S)
    return html.unescape(re.sub(r"\\s+", " ", re.sub(r"<[^>]+>", " ", data))).strip()[:100000]


def search_web(query):
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as response:
        source = response.read().decode("utf-8", "replace")
    found = []
    pattern = r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
    for match in re.finditer(pattern, source, re.I | re.S):
        title = html.unescape(re.sub(r"<[^>]+>", "", match.group(2))).strip()
        found.append({"title": title, "url": html.unescape(match.group(1))})
        if len(found) == 8:
            break
    return json.dumps({"query": query, "results": found}, ensure_ascii=False)


class Approval:
    def __init__(self, tool, params):
        self.tool, self.params = tool, params
        self.done = threading.Event()
        self.accepted = False


class App:
    def __init__(self, root):
        self.root = root
        root.title("Local Cursor Clone")
        root.geometry("1100x720")
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)
        self.events = queue.Queue()
        self.history = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.pending = None
        self.busy = False
        self.make_ui()
        self.refresh_tree()
        root.after(100, self.drain)

    def make_ui(self):
        side = ttk.Frame(self.root, padding=8)
        side.grid(row=0, column=0, sticky="nsew")
        side.rowconfigure(1, weight=1)
        ttk.Label(side, text="WORKSPACE", font=("TkDefaultFont", 10, "bold")).grid(sticky="w")
        self.tree = ttk.Treeview(side, show="tree")
        self.tree.grid(row=1, column=0, sticky="nsew", pady=8)
        ttk.Button(side, text="Refresh", command=self.refresh_tree).grid(row=2, column=0, sticky="ew")
        main = ttk.Frame(self.root, padding=(0, 8, 8, 8))
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)
        ttk.Label(main, text="Chat", font=("TkDefaultFont", 14, "bold")).grid(sticky="w")
        self.chat = tk.Text(main, state="disabled", wrap="word", bg="#17191c", fg="#eeeeee")
        self.chat.grid(row=1, column=0, sticky="nsew", pady=8)
        self.chat.tag_configure("user", foreground="#8ecbff")
        self.chat.tag_configure("assistant", foreground="#c7f9cc")
        self.chat.tag_configure("error", foreground="#ff8c8c")
        approval = ttk.Frame(main)
        approval.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        approval.columnconfigure(0, weight=1)
        self.approval_text = ttk.Label(approval, foreground="#9a5b00")
        self.approval_text.grid(row=0, column=0, sticky="w")
        self.approve = ttk.Button(approval, text="Approve", state="disabled", command=lambda: self.resolve(True))
        self.approve.grid(row=0, column=1, padx=4)
        self.decline = ttk.Button(approval, text="Decline", state="disabled", command=lambda: self.resolve(False))
        self.decline.grid(row=0, column=2)
        bottom = ttk.Frame(main)
        bottom.grid(row=3, column=0, sticky="ew")
        bottom.columnconfigure(0, weight=1)
        self.input = tk.Text(bottom, height=4, wrap="word")
        self.input.grid(row=0, column=0, sticky="ew")
        self.input.bind("<Control-Return>", lambda _: self.submit())
        self.send = ttk.Button(bottom, text="Send (Ctrl+Enter)", command=self.submit)
        self.send.grid(row=0, column=1, padx=(8, 0), sticky="ns")

    def write_chat(self, text, tag=None):
        self.chat.configure(state="normal")
        self.chat.insert("end", text + "\n\n", tag)
        self.chat.see("end")
        self.chat.configure(state="disabled")

    def refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        root = self.tree.insert("", "end", text="workspace/", open=True)
        self.add_nodes(root, WORKSPACE)

    def add_nodes(self, parent, directory):
        try:
            items = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return
        for item in items:
            if item.name.startswith("."):
                continue
            node = self.tree.insert(parent, "end", text=item.name + ("/" if item.is_dir() else ""))
            if item.is_dir():
                self.add_nodes(node, item)

    def submit(self):
        if self.busy:
            return
        prompt = self.input.get("1.0", "end").strip()
        if not prompt:
            return
        self.input.delete("1.0", "end")
        self.write_chat("You: " + prompt, "user")
        self.history.append({"role": "user", "content": prompt})
        self.busy = True
        self.send.configure(state="disabled")
        threading.Thread(target=self.agent_thread, daemon=True).start()

    def agent_thread(self):
        try:
            asyncio.run(self.agent_loop())
        except Exception as exc:
            self.events.put(("message", "Error: " + str(exc), "error"))
        finally:
            self.events.put(("busy", False))

    async def agent_loop(self):
        for _ in range(12):
            raw = await asyncio.to_thread(self.request)
            try:
                action = parse_model_json(raw)
            except Exception as exc:
                self.events.put(("message", "Model response error: " + str(exc), "error"))
                return
            tool, params = action["tool"], action.get("parameters", {})
            if tool == "final_response":
                message = str(params.get("message", "Done."))
                self.events.put(("message", "Assistant: " + message, "assistant"))
                return
            if tool in {"create_folder", "create_file", "edit_file", "delete_file"}:
                result = await asyncio.to_thread(self.filesystem, tool, params)
            elif tool == "web_search":
                try: result = await asyncio.to_thread(search_web, str(params.get("query", "")))
                except Exception as exc: result = "web_search failed: " + str(exc)
            elif tool == "fetch_url":
                try:
                    result = await asyncio.to_thread(fetch_url, str(params.get("url", "")))
                    if params.get("path"):
                        result = await asyncio.to_thread(self.filesystem, "create_file", {"path": params["path"], "content": result})
                except Exception as exc: result = "fetch_url failed: " + str(exc)
            else:
                result = "unknown tool: " + tool
            self.history += [{"role": "assistant", "content": json.dumps(action)}, {"role": "user", "content": "Tool result: " + result}]
        self.events.put(("message", "Maximum agent steps reached.", "error"))

    def request(self):
        if not OPENROUTER_API_KEY or OPENROUTER_API_KEY == "YOUR_KEY_HERE":
            raise RuntimeError("Set OPENROUTER_API_KEY in main.py")
        headers = {"Authorization": "Bearer " + OPENROUTER_API_KEY, "Content-Type": "application/json", "HTTP-Referer": "http://localhost"}
        last = None
        for model in FREE_MODELS:
            try:
                payload = {"model": model, "messages": self.history[-30:], "temperature": 0.2}
                req = urllib.request.Request(API_URL, data=json.dumps(payload).encode(), headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=90) as response:
                    data = json.loads(response.read().decode())
                return data["choices"][0]["message"]["content"]
            except Exception as exc:
                last = exc
        raise RuntimeError("all free models failed: " + str(last))

    def filesystem(self, tool, params):
        approval = Approval(tool, params)
        self.events.put(("approval", approval))
        approval.done.wait()
        if not approval.accepted:
            return "User declined this operation. Do not retry it."
        try:
            path = safe_path(params.get("path", ""))
            if tool == "create_folder":
                path.mkdir(parents=True, exist_ok=True)
                result = "created folder " + str(path.relative_to(WORKSPACE))
            elif tool in {"create_file", "edit_file"}:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(params.get("content", "")), encoding="utf-8")
                result = tool + " completed for " + str(path.relative_to(WORKSPACE))
            elif not path.exists():
                result = "path does not exist"
            elif path.is_dir():
                shutil.rmtree(path); result = "deleted directory"
            else:
                path.unlink(); result = "deleted file"
            self.events.put(("refresh",))
            return result
        except Exception as exc:
            return "filesystem operation failed: " + str(exc)

    def resolve(self, accepted):
        if self.pending:
            self.pending.accepted = accepted
            self.pending.done.set()
            self.pending = None
            self.approval_text.configure(text="")
            self.approve.configure(state="disabled")
            self.decline.configure(state="disabled")

    def drain(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "message": self.write_chat(event[1], event[2])
                elif event[0] == "busy": self.busy = event[1]; self.send.configure(state="disabled" if self.busy else "normal")
                elif event[0] == "refresh": self.refresh_tree()
                elif event[0] == "approval":
                    self.pending = event[1]
                    details = json.dumps(self.pending.params, ensure_ascii=False)[:240]
                    self.approval_text.configure(text=f"Approve {self.pending.tool}: {details}")
                    self.approve.configure(state="normal")
                    self.decline.configure(state="normal")
        except queue.Empty:
            pass
        self.root.after(100, self.drain)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
