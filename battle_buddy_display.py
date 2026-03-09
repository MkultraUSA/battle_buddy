#!/usr/bin/env python3
"""
Battle Buddy Display
====================
Fullscreen display for the AI Battle Buddy system.
- HEARD audio shown in large bold white text
- AGENT speech shown in large green monospace text
- Scrolling log of recent traffic
- Status bar showing current state
- Accepts input via stdin or a named pipe (/tmp/battle_buddy_display.pipe)

Message format (write to pipe or stdin):
  HEARD: <transcribed text>
  AGENT: <agent speech text>
  STATUS: <status message>
  SUMMARY: <summary text>
  CLEAR
"""

import tkinter as tk
from tkinter import font as tkfont
import threading
import os
import time
import sys
import queue
import signal

# ── Config ─────────────────────────────────────────────────────────────────────
PIPE_PATH       = "/tmp/battle_buddy_display.pipe"
BG_COLOR        = "#0a0a0a"       # Near black background
HEARD_COLOR     = "#FFFFFF"       # White for heard/radio audio
AGENT_COLOR     = "#00FF41"       # Matrix green for agent speech
SUMMARY_COLOR   = "#FFD700"       # Gold for summaries
STATUS_COLOR    = "#888888"       # Grey for status bar
HEADER_COLOR    = "#FF4444"       # Red for header
TIMESTAMP_COLOR = "#444444"       # Dark grey for timestamps
MAX_LINES       = 50              # Max lines in scroll buffer
FONT_HEARD      = ("DejaVu Sans", 16, "bold")
FONT_AGENT      = ("DejaVu Sans Mono", 14, "normal")
FONT_SUMMARY    = ("DejaVu Sans", 13, "italic")
FONT_STATUS     = ("DejaVu Sans Mono", 11, "normal")
FONT_HEADER     = ("DejaVu Sans", 13, "bold")
FONT_TIMESTAMP  = ("DejaVu Sans Mono", 9, "normal")
# ───────────────────────────────────────────────────────────────────────────────


class BattleBuddyDisplay:
    def __init__(self, root):
        self.root = root
        self.message_queue = queue.Queue()
        self.lines = []  # list of (kind, timestamp, text)

        self._setup_window()
        self._setup_fonts()
        self._setup_layout()
        self._start_pipe_listener()
        self._poll_queue()

        # Handle Ctrl+C and window close gracefully
        signal.signal(signal.SIGINT, self._on_quit)
        signal.signal(signal.SIGTERM, self._on_quit)
        self.root.protocol("WM_DELETE_WINDOW", self._on_quit)

    def _setup_window(self):
        self.root.title("Battle Buddy — Situational Awareness Display")
        try:
            icon = tk.PhotoImage(file=os.path.join(os.path.dirname(__file__), "urban-battle-buddy-smiley.png"))
            self.root.wm_iconphoto(True, icon)
        except Exception:
            pass
        self.root.configure(bg=BG_COLOR)
        # Start maximized but windowed — F11 for fullscreen, ESC to exit fullscreen
        self.root.attributes("-zoomed", True)
        self._fullscreen = "--fullscreen" in sys.argv
        if self._fullscreen:
            self.root.attributes("-fullscreen", True)
        self.root.bind("<F11>", self._toggle_fullscreen)
        self.root.bind("<Escape>", lambda e: self.root.attributes("-fullscreen", False))
        self.root.bind("<q>", lambda e: self._on_quit())

    def _setup_fonts(self):
        self.font_heard     = tkfont.Font(family="DejaVu Sans",      size=28, weight="bold")
        self.font_agent     = tkfont.Font(family="DejaVu Sans Mono", size=24, weight="normal")
        self.font_summary   = tkfont.Font(family="DejaVu Sans",      size=20, slant="italic")
        self.font_status    = tkfont.Font(family="DejaVu Sans Mono", size=14)
        self.font_header    = tkfont.Font(family="DejaVu Sans",      size=16, weight="bold")
        self.font_timestamp = tkfont.Font(family="DejaVu Sans Mono", size=11)

    def _setup_layout(self):
        # ── Header bar ──
        header_frame = tk.Frame(self.root, bg="#1a0000", pady=6)
        header_frame.pack(fill=tk.X, side=tk.TOP)

        tk.Label(
            header_frame,
            text="⚔  BATTLE BUDDY  //  SITUATIONAL AWARENESS",
            font=self.font_header,
            bg="#1a0000",
            fg=HEADER_COLOR,
            anchor="w",
            padx=16,
        ).pack(side=tk.LEFT)

        self.clock_label = tk.Label(
            header_frame,
            text="",
            font=self.font_status,
            bg="#1a0000",
            fg=STATUS_COLOR,
            padx=16,
        )
        self.clock_label.pack(side=tk.RIGHT)
        self._update_clock()

        # ── Legend bar ──
        legend_frame = tk.Frame(self.root, bg="#111111", pady=4)
        legend_frame.pack(fill=tk.X, side=tk.TOP)

        # Talkgroup indicator — must be packed RIGHT before LEFT items
        self.talkgroup_var = tk.StringVar(value="")
        tk.Label(
            legend_frame,
            textvariable=self.talkgroup_var,
            font=self.font_status,
            bg="#111111",
            fg="#00CFFF",
            padx=12,
        ).pack(side=tk.RIGHT)

        tk.Label(legend_frame, text="● HEARD / RADIO",  font=self.font_status, bg="#111111", fg=HEARD_COLOR,   padx=12).pack(side=tk.LEFT)
        tk.Label(legend_frame, text="● AGENT SPEAKING", font=self.font_status, bg="#111111", fg=AGENT_COLOR,   padx=12).pack(side=tk.LEFT)
        tk.Label(legend_frame, text="● SUMMARY",        font=self.font_status, bg="#111111", fg=SUMMARY_COLOR, padx=12).pack(side=tk.LEFT)

        # ── Main scroll area ──
        scroll_frame = tk.Frame(self.root, bg=BG_COLOR)
        scroll_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        self.text_area = tk.Text(
            scroll_frame,
            bg=BG_COLOR,
            fg=HEARD_COLOR,
            font=self.font_heard,
            wrap=tk.WORD,
            state=tk.DISABLED,
            relief=tk.FLAT,
            borderwidth=0,
            cursor="none",
            spacing1=6,
            spacing3=6,
        )
        self.text_area.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)

        scrollbar = tk.Scrollbar(scroll_frame, command=self.text_area.yview, bg="#222222", troughcolor="#111111")
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.text_area.configure(yscrollcommand=scrollbar.set)

        # ── Configure text tags ──
        self.text_area.tag_configure("heard",     font=self.font_heard,     foreground=HEARD_COLOR)
        self.text_area.tag_configure("agent",     font=self.font_agent,     foreground=AGENT_COLOR)
        self.text_area.tag_configure("summary",   font=self.font_summary,   foreground=SUMMARY_COLOR)
        self.text_area.tag_configure("timestamp", font=self.font_timestamp, foreground=TIMESTAMP_COLOR)
        self.text_area.tag_configure("divider",   font=self.font_timestamp, foreground="#222222")

        # ── Status bar ──
        self.status_var = tk.StringVar(value="● READY — Listening...")
        status_bar = tk.Label(
            self.root,
            textvariable=self.status_var,
            font=self.font_status,
            bg="#111111",
            fg=STATUS_COLOR,
            anchor="w",
            padx=12,
            pady=5,
        )
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)

    def _toggle_fullscreen(self, event=None):
        self._fullscreen = not self._fullscreen
        self.root.attributes("-fullscreen", self._fullscreen)

    def _update_clock(self):
        self.clock_label.config(text=time.strftime("%Y-%m-%d  %H:%M:%S"))
        self.root.after(1000, self._update_clock)

    def _append_line(self, kind, text):
        timestamp = time.strftime("%H:%M:%S")
        self.text_area.configure(state=tk.NORMAL)

        # Prefix icons
        prefix = {
            "heard":   "📻 ",
            "agent":   "🤖 ",
            "summary": "📋 SUMMARY — ",
        }.get(kind, "")

        self.text_area.insert(tk.END, f"[{timestamp}]  ", "timestamp")
        self.text_area.insert(tk.END, f"{prefix}{text}\n", kind)
        self.text_area.insert(tk.END, "─" * 60 + "\n", "divider")

        # Trim buffer
        self.lines.append((kind, timestamp, text))
        if len(self.lines) > MAX_LINES:
            self.lines.pop(0)
            # Remove first 2 lines (entry + divider) from text widget
            self.text_area.delete("1.0", "3.0")

        self.text_area.configure(state=tk.DISABLED)
        self.text_area.see(tk.END)

    def _process_message(self, raw):
        raw = raw.strip()
        if not raw:
            return
        if raw.upper() == "CLEAR":
            self.text_area.configure(state=tk.NORMAL)
            self.text_area.delete("1.0", tk.END)
            self.text_area.configure(state=tk.DISABLED)
            self.lines.clear()
        elif raw.upper().startswith("TALKGROUP:"):
            tg = raw[10:].strip()
            self.talkgroup_var.set(f"📡 {tg}" if tg else "")
        elif raw.upper().startswith("STATUS:"):
            self.status_var.set("● " + raw[7:].strip())
        elif raw.upper().startswith("HEARD:"):
            self._append_line("heard", raw[6:].strip())
            self.status_var.set("● HEARD — transcribing...")
        elif raw.upper().startswith("AGENT:"):
            self._append_line("agent", raw[6:].strip())
            self.status_var.set("● AGENT SPEAKING")
        elif raw.upper().startswith("SUMMARY:"):
            self._append_line("summary", raw[8:].strip())
            self.status_var.set("● SUMMARY DELIVERED")
        else:
            # Default: treat as HEARD
            self._append_line("heard", raw)

    def _poll_queue(self):
        try:
            while True:
                msg = self.message_queue.get_nowait()
                self._process_message(msg)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _start_pipe_listener(self):
        # Create named pipe if it doesn't exist
        if not os.path.exists(PIPE_PATH):
            os.mkfifo(PIPE_PATH)

        def listen():
            while True:
                try:
                    with open(PIPE_PATH, "r") as pipe:
                        for line in pipe:
                            self.message_queue.put(line.strip())
                except Exception as e:
                    time.sleep(0.5)

        t = threading.Thread(target=listen, daemon=True)
        t.start()

        # Also listen on stdin if piped
        if not sys.stdin.isatty():
            def stdin_listen():
                for line in sys.stdin:
                    self.message_queue.put(line.strip())
            threading.Thread(target=stdin_listen, daemon=True).start()

    def _on_quit(self, *args):
        # Clean up pipe
        if os.path.exists(PIPE_PATH):
            try:
                os.remove(PIPE_PATH)
            except Exception:
                pass
        self.root.destroy()
        sys.exit(0)


def main():
    root = tk.Tk()
    app = BattleBuddyDisplay(root)

    # Demo messages if --demo flag passed
    if "--demo" in sys.argv:
        def send_demos():
            time.sleep(2)
            demos = [
                "STATUS: Initializing audio pipeline...",
                "HEARD: Alpha team this is Bravo, we have eyes on two vehicles moving east on Route 7, over.",
                "AGENT: Understood. Logging two vehicles eastbound on Route 7. Updating contact report.",
                "HEARD: Bravo this is Alpha, confirm vehicle types, over.",
                "HEARD: Looks like one pickup truck and one sedan, both dark colored, over.",
                "AGENT: Two dark vehicles logged — one pickup, one sedan — eastbound Route 7. Grid reference needed.",
                "SUMMARY: Contact report: 2 dark vehicles (pickup + sedan) moving eastbound on Route 7. Reported by Bravo team. Time: " + time.strftime("%H:%M") + ". Awaiting grid reference.",
                "STATUS: Listening for radio traffic...",
            ]
            for msg in demos:
                with open(PIPE_PATH, "w") as p:
                    p.write(msg + "\n")
                time.sleep(3)

        threading.Thread(target=send_demos, daemon=True).start()

    root.mainloop()


if __name__ == "__main__":
    main()
