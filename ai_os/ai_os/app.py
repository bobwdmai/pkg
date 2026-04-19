import json
import importlib.util
import queue
import re
import shutil
import site
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from .agent import AiderStyleAgent
from .code_runner import run_source_code

try:
    import speech_recognition as sr
except Exception:  # pragma: no cover
    sr = None  # type: ignore[assignment]


class AIOSApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("AI OS")
        self.geometry("1360x860")
        self.minsize(1080, 720)
        self.configure(bg="#0F172A")

        self.agent = AiderStyleAgent()
        self.pending_response = False
        self._bubble_refs: list[tk.Widget] = []
        self.current_file_path: Path | None = None

        self.settings_path = Path.home() / ".config" / "ai-os" / "settings.json"
        self.settings = self._load_settings()
        self.first_run_marker = self.settings_path.parent / ".first_run_complete"
        self.app_data_dir = Path.home() / ".local" / "share" / "ai-os"
        self.venv_dir = self.app_data_dir / "venv"
        repo_candidate = Path.home() / "ai-os"
        self.workspace_root = repo_candidate.resolve() if repo_candidate.exists() else Path.cwd().resolve()

        self.voice_prompt_queue: queue.Queue[str] = queue.Queue()
        self.interruption_note = ""
        self.last_spoken_text = ""
        self.tts_process: subprocess.Popen[str] | None = None
        self.tts_lock = threading.Lock()
        self.tts_interrupted = False

        self._init_speech_backend()
        self.sr_recognizer = sr.Recognizer() if sr else None
        self.sr_microphone = None
        self.stop_listening = None

        self._init_styles()
        self._build_ui()
        self._apply_settings_to_ui()
        self._add_assistant_message(
            "AI OS ready. I can code, edit files, and now handle live voice interrupt when STT/TTS are enabled."
        )
        self._sync_live_audio_state()
        self._run_first_launch_setup()

    def _run_first_launch_setup(self) -> None:
        if self.first_run_marker.exists():
            return
        self._append_console("[setup] first launch detected: running auto setup/build")
        worker = threading.Thread(target=self._first_launch_setup_worker, daemon=True)
        worker.start()

    def _init_speech_backend(self) -> None:
        global sr
        if sr is not None:
            return
        try:
            pyver = f"python{sys.version_info.major}.{sys.version_info.minor}"
            candidates = [
                self.venv_dir / "lib" / pyver / "site-packages",
                self.venv_dir / "lib64" / pyver / "site-packages",
            ]
            for candidate in candidates:
                if candidate.exists():
                    site.addsitedir(str(candidate))
            import importlib
            sr = importlib.import_module("speech_recognition")  # type: ignore[assignment]
        except Exception:
            sr = None  # type: ignore[assignment]

    def _module_available(self, module_name: str) -> bool:
        return importlib.util.find_spec(module_name) is not None

    def _first_launch_setup_worker(self) -> None:
        self.app_data_dir.mkdir(parents=True, exist_ok=True)
        tasks: list[str] = []
        if not self._module_available("speech_recognition"):
            tasks.append("SpeechRecognition")

        if not tasks:
            self.first_run_marker.parent.mkdir(parents=True, exist_ok=True)
            self.first_run_marker.write_text("ok\n", encoding="utf-8")
            self.after(0, lambda: self._append_console("[setup] first launch setup complete"))
            return

        if not self.venv_dir.exists():
            try:
                subprocess.run(
                    [sys.executable, "-m", "venv", "--system-site-packages", str(self.venv_dir)],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=True,
                )
            except Exception as exc:
                self.after(0, lambda: self._append_console(f"[setup] venv create failed: {exc}"))
                return

        pip_cmd = [str(self.venv_dir / "bin" / "python3"), "-m", "pip", "install", *tasks]
        try:
            proc = subprocess.run(pip_cmd, capture_output=True, text=True, timeout=300)
            if proc.returncode == 0:
                self.first_run_marker.parent.mkdir(parents=True, exist_ok=True)
                self.first_run_marker.write_text("ok\n", encoding="utf-8")
                self._init_speech_backend()
                self.sr_recognizer = sr.Recognizer() if sr else None
                self.after(0, lambda: self._append_console("[setup] installed missing STT dependencies (venv)"))
            else:
                details = (proc.stderr or proc.stdout).strip()
                self.after(0, lambda: self._append_console(f"[setup] auto install failed: {details}"))
        except Exception as exc:
            self.after(0, lambda: self._append_console(f"[setup] auto setup failed: {exc}"))

    def _default_settings(self) -> dict:
        return {
            "live_stt": False,
            "live_tts": False,
            "stt_interrupt": True,
            "auto_write_files": False,
            "file_permissions": {
                "check_all": False,
                "workspace": True,
                "home": False,
                "tmp": True,
                "all_file_types": True,
                "allowed_extensions": ".py,.sh,.js,.ts,.rb,.pl,.php,.json,.md,.txt,.yaml,.yml,.toml,.ini,.cfg,.html,.css,.xml,.sql",
            },
            "models": {
                "fallback": {
                    "enabled": True,
                    "role": "Handle short/simple requests quickly and keep replies compact.",
                },
                "fast": {
                    "enabled": True,
                    "role": "Primary coding assistant for normal tasks, code edits, and debugging.",
                },
                "heavy": {
                    "enabled": True,
                    "role": "Deep reasoning mode for complex debugging and architecture tasks.",
                },
            },
        }

    def _load_settings(self) -> dict:
        defaults = self._default_settings()
        try:
            if not self.settings_path.exists():
                return defaults
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except Exception:
            return defaults

        merged = defaults
        if isinstance(data, dict):
            merged["live_stt"] = bool(data.get("live_stt", defaults["live_stt"]))
            merged["live_tts"] = bool(data.get("live_tts", defaults["live_tts"]))
            merged["stt_interrupt"] = bool(data.get("stt_interrupt", defaults["stt_interrupt"]))
            merged["auto_write_files"] = bool(data.get("auto_write_files", defaults["auto_write_files"]))
            raw_permissions = (
                data.get("file_permissions", {})
                if isinstance(data.get("file_permissions", {}), dict)
                else {}
            )
            for key in ("check_all", "workspace", "home", "tmp", "all_file_types"):
                merged["file_permissions"][key] = bool(
                    raw_permissions.get(key, merged["file_permissions"][key])
                )
            merged["file_permissions"]["allowed_extensions"] = str(
                raw_permissions.get(
                    "allowed_extensions",
                    merged["file_permissions"]["allowed_extensions"],
                )
            )
            raw_models = data.get("models", {}) if isinstance(data.get("models", {}), dict) else {}
            for key in ("fallback", "fast", "heavy"):
                raw_item = raw_models.get(key, {}) if isinstance(raw_models.get(key, {}), dict) else {}
                merged["models"][key]["enabled"] = bool(raw_item.get("enabled", merged["models"][key]["enabled"]))
                merged["models"][key]["role"] = str(raw_item.get("role", merged["models"][key]["role"]))
        return merged

    def _save_settings(self) -> None:
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")

    def _init_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")

        base_bg = "#0F172A"
        panel_bg = "#111827"
        text_primary = "#E5E7EB"
        text_muted = "#9CA3AF"
        accent = "#14B8A6"

        style.configure("Root.TFrame", background=base_bg)
        style.configure("Panel.TFrame", background=panel_bg)
        style.configure("Sidebar.TFrame", background="#0B1220")
        style.configure("TopBar.TFrame", background="#111827")
        style.configure("Title.TLabel", background="#111827", foreground=text_primary, font=("Helvetica", 15, "bold"))
        style.configure("Meta.TLabel", background="#111827", foreground=text_muted, font=("Helvetica", 10))
        style.configure("SidebarTitle.TLabel", background="#0B1220", foreground=text_primary, font=("Helvetica", 12, "bold"))
        style.configure("SidebarMeta.TLabel", background="#0B1220", foreground=text_muted, font=("Helvetica", 10))
        style.configure("TNotebook", background=panel_bg, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(12, 8), background="#1F2937", foreground=text_primary)
        style.map("TNotebook.Tab", background=[("selected", panel_bg)], foreground=[("selected", accent)])
        style.configure("Toggle.TCheckbutton", background="#111827", foreground=text_primary)

        style.configure(
            "Primary.TButton",
            background=accent,
            foreground="#052E2B",
            borderwidth=0,
            focusthickness=0,
            font=("Helvetica", 10, "bold"),
            padding=(12, 8),
        )
        style.map("Primary.TButton", background=[("active", "#2DD4BF")])

        style.configure(
            "Ghost.TButton",
            background="#1F2937",
            foreground=text_primary,
            borderwidth=0,
            padding=(10, 7),
        )
        style.map("Ghost.TButton", background=[("active", "#374151")])

    def _build_ui(self) -> None:
        root = ttk.Frame(self, style="Root.TFrame", padding=12)
        root.pack(fill=tk.BOTH, expand=True)

        root.columnconfigure(0, weight=0)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)

        self._build_sidebar(root)
        self._build_workspace(root)

    def _build_sidebar(self, parent: ttk.Frame) -> None:
        sidebar = ttk.Frame(parent, style="Sidebar.TFrame", padding=12)
        sidebar.grid(row=0, column=0, sticky="nsew", padx=(0, 10))

        ttk.Label(sidebar, text="AI OS", style="SidebarTitle.TLabel").pack(anchor="w")
        ttk.Label(sidebar, text="Local coding assistant", style="SidebarMeta.TLabel").pack(anchor="w", pady=(2, 12))

        ttk.Button(sidebar, text="New Chat", style="Ghost.TButton", command=self.clear_chat).pack(fill=tk.X, pady=(0, 8))
        ttk.Button(sidebar, text="Send Editor To AI", style="Ghost.TButton", command=self.send_editor_to_ai).pack(fill=tk.X)

        ttk.Separator(sidebar, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=12)

        ttk.Label(sidebar, text="Model Mode", style="SidebarTitle.TLabel").pack(anchor="w")
        self.model_mode = tk.StringVar(value="auto")
        model_menu = ttk.OptionMenu(
            sidebar,
            self.model_mode,
            "auto",
            "auto",
            "manual-fast",
            "manual-fallback",
            "manual-heavy",
        )
        model_menu.pack(fill=tk.X, pady=(8, 0))

        ttk.Label(
            sidebar,
            text="Enable/disable model profiles and tune their behavior in Settings.",
            style="SidebarMeta.TLabel",
            wraplength=180,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(10, 0))

    def _build_workspace(self, parent: ttk.Frame) -> None:
        workspace = ttk.Notebook(parent)
        workspace.grid(row=0, column=1, sticky="nsew")

        chat_tab = ttk.Frame(workspace, style="Panel.TFrame", padding=0)
        code_tab = ttk.Frame(workspace, style="Panel.TFrame", padding=10)
        settings_tab = ttk.Frame(workspace, style="Panel.TFrame", padding=12)

        workspace.add(chat_tab, text="Chat")
        workspace.add(code_tab, text="Code")
        workspace.add(settings_tab, text="Settings")

        self._build_chat_tab(chat_tab)
        self._build_code_panel(code_tab)
        self._build_settings_panel(settings_tab)

    def _build_chat_tab(self, parent: ttk.Frame) -> None:
        parent.rowconfigure(1, weight=1)
        parent.columnconfigure(0, weight=1)

        topbar = ttk.Frame(parent, style="TopBar.TFrame", padding=(14, 10))
        topbar.grid(row=0, column=0, sticky="ew")
        ttk.Label(topbar, text="AI Assistant", style="Title.TLabel").pack(anchor="w")
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(topbar, textvariable=self.status_var, style="Meta.TLabel").pack(anchor="w", pady=(2, 0))

        self.chat_canvas = tk.Canvas(parent, bg="#111827", highlightthickness=0)
        self.chat_canvas.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=self.chat_canvas.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.chat_canvas.configure(yscrollcommand=scroll.set)

        self.chat_list = ttk.Frame(self.chat_canvas, style="Panel.TFrame", padding=(16, 14))
        self.chat_window = self.chat_canvas.create_window((0, 0), window=self.chat_list, anchor="nw")

        self.chat_list.bind("<Configure>", self._on_chat_frame_configure)
        self.chat_canvas.bind("<Configure>", self._on_chat_canvas_configure)

        composer = ttk.Frame(parent, style="TopBar.TFrame", padding=10)
        composer.grid(row=2, column=0, columnspan=2, sticky="ew")
        composer.columnconfigure(0, weight=1)

        self.prompt_input = tk.Text(
            composer,
            height=4,
            wrap=tk.WORD,
            bg="#0B1220",
            fg="#E5E7EB",
            insertbackground="#E5E7EB",
            relief=tk.FLAT,
            padx=10,
            pady=8,
            font=("Helvetica", 11),
        )
        self.prompt_input.grid(row=0, column=0, sticky="ew")
        self.prompt_input.bind("<Return>", self._on_enter_pressed)
        self.prompt_input.bind("<Shift-Return>", lambda _event: None)

        mic_btn = ttk.Button(
            composer,
            text="Mic",
            style="Ghost.TButton",
            command=self.start_manual_stt,
        )
        mic_btn.grid(row=0, column=1, sticky="se", padx=(10, 0))

        send_btn = ttk.Button(composer, text="Send", style="Primary.TButton", command=self.send_prompt)
        send_btn.grid(row=0, column=2, sticky="se", padx=(10, 0))

    def _build_code_panel(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Code IDE", style="Title.TLabel").pack(anchor="w")

        editor_row = ttk.Frame(parent, style="Panel.TFrame")
        editor_row.pack(fill=tk.X, pady=(8, 8))
        ttk.Button(editor_row, text="Open File", style="Ghost.TButton", command=self.open_file).pack(side=tk.LEFT)
        ttk.Button(editor_row, text="Save File", style="Ghost.TButton", command=self.save_file).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(editor_row, text="Run Code", style="Primary.TButton", command=self.run_code).pack(side=tk.LEFT, padx=(8, 0))

        self.code_editor = scrolledtext.ScrolledText(
            parent,
            wrap=tk.NONE,
            height=20,
            bg="#0B1220",
            fg="#E5E7EB",
            insertbackground="#E5E7EB",
            relief=tk.FLAT,
            padx=8,
            pady=8,
            font=("Consolas", 11),
        )
        self.code_editor.pack(fill=tk.BOTH, expand=True)
        self.code_editor.insert(tk.END, "# Write or load Python code here.\nprint('hello from AI OS')\n")

        ttk.Label(parent, text="Console", style="Meta.TLabel").pack(anchor="w", pady=(8, 0))
        self.console = scrolledtext.ScrolledText(
            parent,
            wrap=tk.WORD,
            height=10,
            bg="#030712",
            fg="#E5E7EB",
            insertbackground="#E5E7EB",
            relief=tk.FLAT,
            padx=8,
            pady=8,
            font=("Consolas", 10),
        )
        self.console.pack(fill=tk.BOTH, expand=False, pady=(4, 0))
        self.console.insert(tk.END, "Console ready.\n")
        self.console.config(state=tk.DISABLED)

    def _build_settings_panel(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Voice + Model Settings", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            parent,
            text="Live STT listens continuously. If interrupt is enabled, speaking while AI is talking will stop TTS and queue your voice prompt.",
            style="Meta.TLabel",
            wraplength=820,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(4, 10))

        voice_box = ttk.Frame(parent, style="TopBar.TFrame", padding=10)
        voice_box.pack(fill=tk.X)

        self.live_stt_var = tk.BooleanVar(value=False)
        self.live_tts_var = tk.BooleanVar(value=False)
        self.stt_interrupt_var = tk.BooleanVar(value=True)

        ttk.Checkbutton(voice_box, text="Enable Live STT", variable=self.live_stt_var, style="Toggle.TCheckbutton").pack(anchor="w")
        ttk.Checkbutton(voice_box, text="Enable Live TTS", variable=self.live_tts_var, style="Toggle.TCheckbutton").pack(anchor="w", pady=(4, 0))
        ttk.Checkbutton(
            voice_box,
            text="Interrupt TTS when user starts speaking",
            variable=self.stt_interrupt_var,
            style="Toggle.TCheckbutton",
        ).pack(anchor="w", pady=(4, 0))
        self.auto_write_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            voice_box,
            text="Auto-write files from AI response file blocks",
            variable=self.auto_write_var,
            style="Toggle.TCheckbutton",
        ).pack(anchor="w", pady=(4, 0))
        ttk.Label(
            voice_box,
            text="Use block format: ```file:path/to/file.py ... ```",
            style="Meta.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        perm_box = ttk.Frame(parent, style="TopBar.TFrame", padding=10)
        perm_box.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(perm_box, text="File Run/Write Permissions", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            perm_box,
            text=f"Workspace root: {self.workspace_root}",
            style="Meta.TLabel",
            wraplength=820,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(2, 6))

        self.perm_check_all_var = tk.BooleanVar(value=False)
        self.perm_workspace_var = tk.BooleanVar(value=True)
        self.perm_home_var = tk.BooleanVar(value=False)
        self.perm_tmp_var = tk.BooleanVar(value=True)
        self.perm_all_types_var = tk.BooleanVar(value=True)
        self.allowed_ext_var = tk.StringVar(value="")

        ttk.Checkbutton(
            perm_box,
            text="Check all (allow any path)",
            variable=self.perm_check_all_var,
            command=self._on_check_all_permissions,
            style="Toggle.TCheckbutton",
        ).pack(anchor="w")
        ttk.Checkbutton(
            perm_box,
            text="Allow workspace",
            variable=self.perm_workspace_var,
            command=self._on_permission_scope_changed,
            style="Toggle.TCheckbutton",
        ).pack(anchor="w", pady=(3, 0))
        ttk.Checkbutton(
            perm_box,
            text="Allow home",
            variable=self.perm_home_var,
            command=self._on_permission_scope_changed,
            style="Toggle.TCheckbutton",
        ).pack(anchor="w", pady=(3, 0))
        ttk.Checkbutton(
            perm_box,
            text="Allow /tmp",
            variable=self.perm_tmp_var,
            command=self._on_permission_scope_changed,
            style="Toggle.TCheckbutton",
        ).pack(anchor="w", pady=(3, 0))
        ttk.Checkbutton(
            perm_box,
            text="Allow all file types",
            variable=self.perm_all_types_var,
            command=self._on_all_types_toggled,
            style="Toggle.TCheckbutton",
        ).pack(anchor="w", pady=(8, 0))
        ttk.Label(
            perm_box,
            text="Allowed extensions (comma-separated, used when all file types is off)",
            style="Meta.TLabel",
        ).pack(anchor="w", pady=(4, 0))
        self.allowed_ext_entry = ttk.Entry(perm_box, textvariable=self.allowed_ext_var)
        self.allowed_ext_entry.pack(fill=tk.X, pady=(4, 0))

        model_box = ttk.Frame(parent, style="TopBar.TFrame", padding=10)
        model_box.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        ttk.Label(model_box, text="Model Profiles", style="Title.TLabel").pack(anchor="w")

        self.model_vars: dict[str, tk.BooleanVar] = {
            "fallback": tk.BooleanVar(value=True),
            "fast": tk.BooleanVar(value=True),
            "heavy": tk.BooleanVar(value=True),
        }
        self.model_role_widgets: dict[str, tk.Text] = {}

        for key, label in (("fallback", "Fallback 1.5B"), ("fast", "Fast 3B"), ("heavy", "Heavy 14B")):
            frame = ttk.Frame(model_box, style="TopBar.TFrame", padding=8)
            frame.pack(fill=tk.X, pady=(8, 0))
            ttk.Checkbutton(frame, text=f"Enable {label}", variable=self.model_vars[key], style="Toggle.TCheckbutton").pack(anchor="w")
            ttk.Label(frame, text="Behavior instruction", style="Meta.TLabel").pack(anchor="w", pady=(6, 2))
            role_text = tk.Text(
                frame,
                height=3,
                wrap=tk.WORD,
                bg="#0B1220",
                fg="#E5E7EB",
                insertbackground="#E5E7EB",
                relief=tk.FLAT,
                padx=8,
                pady=6,
                font=("Helvetica", 10),
            )
            role_text.pack(fill=tk.X)
            self.model_role_widgets[key] = role_text

        button_row = ttk.Frame(parent, style="Panel.TFrame")
        button_row.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(button_row, text="Apply Settings", style="Primary.TButton", command=self.apply_settings).pack(side=tk.LEFT)

    def _apply_settings_to_ui(self) -> None:
        self.live_stt_var.set(bool(self.settings.get("live_stt", False)))
        self.live_tts_var.set(bool(self.settings.get("live_tts", False)))
        self.stt_interrupt_var.set(bool(self.settings.get("stt_interrupt", True)))
        self.auto_write_var.set(bool(self.settings.get("auto_write_files", False)))
        file_permissions = self.settings.get("file_permissions", {})
        self.perm_check_all_var.set(bool(file_permissions.get("check_all", False)))
        self.perm_workspace_var.set(bool(file_permissions.get("workspace", True)))
        self.perm_home_var.set(bool(file_permissions.get("home", False)))
        self.perm_tmp_var.set(bool(file_permissions.get("tmp", True)))
        self.perm_all_types_var.set(bool(file_permissions.get("all_file_types", True)))
        self.allowed_ext_var.set(str(file_permissions.get("allowed_extensions", "")))
        self._on_all_types_toggled()

        models = self.settings.get("models", {})
        for key in ("fallback", "fast", "heavy"):
            item = models.get(key, {}) if isinstance(models.get(key, {}), dict) else {}
            self.model_vars[key].set(bool(item.get("enabled", True)))
            role = str(item.get("role", ""))
            widget = self.model_role_widgets[key]
            widget.delete("1.0", tk.END)
            widget.insert(tk.END, role)

    def apply_settings(self) -> None:
        self.settings["live_stt"] = self.live_stt_var.get()
        self.settings["live_tts"] = self.live_tts_var.get()
        self.settings["stt_interrupt"] = self.stt_interrupt_var.get()
        self.settings["auto_write_files"] = self.auto_write_var.get()
        self.settings["file_permissions"]["check_all"] = self.perm_check_all_var.get()
        self.settings["file_permissions"]["workspace"] = self.perm_workspace_var.get()
        self.settings["file_permissions"]["home"] = self.perm_home_var.get()
        self.settings["file_permissions"]["tmp"] = self.perm_tmp_var.get()
        self.settings["file_permissions"]["all_file_types"] = self.perm_all_types_var.get()
        self.settings["file_permissions"]["allowed_extensions"] = self.allowed_ext_var.get().strip()

        for key in ("fallback", "fast", "heavy"):
            self.settings["models"][key]["enabled"] = self.model_vars[key].get()
            self.settings["models"][key]["role"] = self.model_role_widgets[key].get("1.0", tk.END).strip()

        self._save_settings()
        self._sync_live_audio_state()
        if self.settings["live_tts"] and shutil.which("espeak-ng") is None:
            self._append_console("[voice] live TTS enabled, but espeak-ng is not installed/found in PATH")
            self.status_var.set("TTS unavailable (missing espeak-ng)")
            return
        self.status_var.set("Settings applied")
        self._append_console("Settings applied.")

    def _on_check_all_permissions(self) -> None:
        all_enabled = self.perm_check_all_var.get()
        self.perm_workspace_var.set(all_enabled)
        self.perm_home_var.set(all_enabled)
        self.perm_tmp_var.set(all_enabled)

    def _on_permission_scope_changed(self) -> None:
        self.perm_check_all_var.set(
            self.perm_workspace_var.get() and self.perm_home_var.get() and self.perm_tmp_var.get()
        )

    def _on_all_types_toggled(self) -> None:
        state = tk.DISABLED if self.perm_all_types_var.get() else tk.NORMAL
        self.allowed_ext_entry.configure(state=state)

    def _allowed_roots(self) -> list[Path]:
        file_permissions = self.settings.get("file_permissions", {})
        if bool(file_permissions.get("check_all", False)):
            return [Path("/")]

        roots: list[Path] = []
        if bool(file_permissions.get("workspace", True)):
            roots.append(self.workspace_root)
        if bool(file_permissions.get("home", False)):
            roots.append(Path.home().resolve())
        if bool(file_permissions.get("tmp", True)):
            roots.append(Path("/tmp"))
        return roots

    def _is_path_allowed(self, target: Path) -> bool:
        resolved = target.resolve(strict=False)
        for root in self._allowed_roots():
            try:
                resolved.relative_to(root.resolve(strict=False))
                return True
            except Exception:
                continue
        return False

    def _allowed_extensions_set(self) -> set[str]:
        file_permissions = self.settings.get("file_permissions", {})
        raw = str(file_permissions.get("allowed_extensions", ""))
        result: set[str] = set()
        for item in raw.split(","):
            value = item.strip().lower()
            if not value:
                continue
            if not value.startswith("."):
                value = f".{value}"
            result.add(value)
        return result

    def _is_extension_allowed(self, target: Path) -> bool:
        file_permissions = self.settings.get("file_permissions", {})
        if bool(file_permissions.get("all_file_types", True)):
            return True
        suffix = target.suffix.lower()
        allowed = self._allowed_extensions_set()
        return suffix in allowed

    def _resolve_target_path(self, raw_path: str) -> Path | None:
        cleaned = raw_path.strip().strip('"').strip("'")
        if not cleaned:
            return None

        candidate = Path(cleaned).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        try:
            return candidate.resolve(strict=False)
        except Exception:
            return None

    def _extract_file_blocks(self, response: str) -> list[tuple[str, str]]:
        blocks: list[tuple[str, str]] = []
        patterns = [
            r"```(?:file|path)\s*:\s*([^\n`]+)\n(.*?)```",
            r"```[a-zA-Z0-9_+-]*\s+path=([^\s\n`]+)\n(.*?)```",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, response, flags=re.DOTALL | re.IGNORECASE):
                raw_path = match.group(1).strip()
                content = match.group(2).rstrip("\n")
                blocks.append((raw_path, content))
        return blocks

    def _auto_write_files_from_response(self, response: str) -> None:
        if not bool(self.settings.get("auto_write_files", False)):
            return

        blocks = self._extract_file_blocks(response)
        if not blocks:
            return

        written = 0
        denied = 0
        invalid = 0
        for raw_path, content in blocks:
            target = self._resolve_target_path(raw_path)
            if target is None:
                invalid += 1
                continue
            if not self._is_path_allowed(target):
                denied += 1
                continue
            if not self._is_extension_allowed(target):
                denied += 1
                self._append_console(f"[auto-write denied] extension not allowed: {target.suffix or '[none]'}")
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content + "\n", encoding="utf-8")
                written += 1
                self._append_console(f"[auto-write] wrote {target}")
            except Exception as exc:
                self._append_console(f"[auto-write] failed {target}: {exc}")

        if written or denied or invalid:
            self.status_var.set(f"Auto-write: {written} written, {denied} denied, {invalid} invalid")

    def _enabled_models(self) -> dict[str, bool]:
        models = self.settings.get("models", {})
        return {
            "fallback": bool(models.get("fallback", {}).get("enabled", True)),
            "fast": bool(models.get("fast", {}).get("enabled", True)),
            "heavy": bool(models.get("heavy", {}).get("enabled", True)),
        }

    def _model_roles(self) -> dict[str, str]:
        models = self.settings.get("models", {})
        return {
            "fallback": str(models.get("fallback", {}).get("role", "")).strip(),
            "fast": str(models.get("fast", {}).get("role", "")).strip(),
            "heavy": str(models.get("heavy", {}).get("role", "")).strip(),
        }

    def _sync_live_audio_state(self) -> None:
        if self.settings.get("live_stt", False):
            self._start_live_stt()
        else:
            self._stop_live_stt()

    def _start_live_stt(self) -> None:
        if self.stop_listening is not None:
            return
        if sr is None:
            self._append_console("[voice] speech_recognition not installed; live STT unavailable")
            self.status_var.set("Live STT unavailable (missing dependency)")
            return

        try:
            self.sr_microphone = sr.Microphone()
            with self.sr_microphone as source:
                if self.sr_recognizer:
                    self.sr_recognizer.adjust_for_ambient_noise(source, duration=0.6)
            self.stop_listening = self.sr_recognizer.listen_in_background(  # type: ignore[union-attr]
                self.sr_microphone,
                self._stt_callback,
                phrase_time_limit=7,
            )
            self.status_var.set("Live STT listening")
            self._append_console("[voice] live STT enabled")
        except Exception as exc:
            self.stop_listening = None
            self.sr_microphone = None
            self._append_console(f"[voice] failed to start live STT: {exc}")
            self.status_var.set("Live STT failed")

    def _stop_live_stt(self) -> None:
        stopper = self.stop_listening
        if stopper is not None:
            try:
                stopper(wait_for_stop=False)
            except Exception:
                pass
        self.stop_listening = None
        self.sr_microphone = None

    def _stt_callback(self, _recognizer: object, audio: object) -> None:
        if not self.settings.get("live_stt", False):
            return

        if self.settings.get("stt_interrupt", True):
            self._interrupt_tts("user started speaking")

        worker = threading.Thread(target=self._process_stt_audio, args=(audio,), daemon=True)
        worker.start()

    def _process_stt_audio(self, audio: object) -> None:
        if not self.sr_recognizer:
            return
        text = ""
        try:
            text = self.sr_recognizer.recognize_google(audio).strip()
        except Exception:
            try:
                text = self.sr_recognizer.recognize_sphinx(audio).strip()
            except Exception:
                text = ""

        if not text:
            return
        self.after(0, lambda: self._handle_voice_prompt(text))

    def start_manual_stt(self) -> None:
        if sr is None or not self.sr_recognizer:
            fallback_text = self._manual_stt_external_fallback()
            if fallback_text:
                self._manual_stt_success(fallback_text)
                return
            self._append_console(
                "[voice] manual STT unavailable. Install: sudo apt install python3-pyaudio python3-venv python3-pip"
            )
            self.status_var.set("Manual STT unavailable")
            return

        self.status_var.set("Listening (manual STT)...")
        self._append_console("[voice] manual STT listening...")
        worker = threading.Thread(target=self._manual_stt_worker, daemon=True)
        worker.start()

    def _manual_stt_external_fallback(self) -> str:
        arecord_bin = shutil.which("arecord")
        whisper_bin = shutil.which("whisper")
        if not arecord_bin or not whisper_bin:
            return ""

        self._append_console("[voice] fallback STT: recording with arecord, transcribing with whisper")
        with tempfile.TemporaryDirectory(prefix="ai_os_stt_") as temp_dir:
            wav_path = Path(temp_dir) / "manual.wav"
            cmd_record = [
                arecord_bin,
                "-q",
                "-f",
                "S16_LE",
                "-r",
                "16000",
                "-c",
                "1",
                "-d",
                "8",
                str(wav_path),
            ]
            try:
                rec = subprocess.run(cmd_record, capture_output=True, text=True, timeout=12)
            except Exception as exc:
                self._append_console(f"[voice] fallback record failed: {exc}")
                return ""
            if rec.returncode != 0:
                self._append_console(f"[voice] fallback record failed: {rec.stderr.strip()}")
                return ""

            cmd_whisper = [
                whisper_bin,
                str(wav_path),
                "--model",
                "tiny",
                "--language",
                "en",
                "--output_format",
                "txt",
                "--output_dir",
                temp_dir,
            ]
            try:
                tr = subprocess.run(cmd_whisper, capture_output=True, text=True, timeout=60)
            except Exception as exc:
                self._append_console(f"[voice] fallback transcribe failed: {exc}")
                return ""
            if tr.returncode != 0:
                details = tr.stderr.strip() or tr.stdout.strip()
                self._append_console(f"[voice] fallback transcribe failed: {details}")
                return ""

            txt_path = Path(temp_dir) / "manual.txt"
            if not txt_path.exists():
                return ""
            return txt_path.read_text(encoding="utf-8").strip()

    def _manual_stt_worker(self) -> None:
        if not self.sr_recognizer:
            return
        try:
            with sr.Microphone() as source:  # type: ignore[union-attr]
                self.sr_recognizer.adjust_for_ambient_noise(source, duration=0.5)
                audio = self.sr_recognizer.listen(source, timeout=8, phrase_time_limit=10)
        except Exception as exc:
            self.after(0, lambda: self._manual_stt_failed(f"[voice] manual STT capture failed: {exc}"))
            return

        text = ""
        try:
            text = self.sr_recognizer.recognize_google(audio).strip()
        except Exception:
            try:
                text = self.sr_recognizer.recognize_sphinx(audio).strip()
            except Exception:
                text = ""

        if not text:
            self.after(0, lambda: self._manual_stt_failed("[voice] manual STT: no speech recognized"))
            return
        self.after(0, lambda: self._manual_stt_success(text))

    def _manual_stt_failed(self, message: str) -> None:
        self._append_console(message)
        self.status_var.set("Manual STT failed")

    def _manual_stt_success(self, text: str) -> None:
        self._append_console(f"[voice] manual STT: {text}")
        self._handle_voice_prompt(text)

    def _handle_voice_prompt(self, text: str) -> None:
        if self.pending_response:
            self.voice_prompt_queue.put(text)
            self.status_var.set("Voice captured; queued while current response finishes")
            return
        self._send_prompt_text(text, source="voice")

    def _interrupt_tts(self, reason: str) -> None:
        with self.tts_lock:
            proc = self.tts_process
            if not proc:
                return
            if proc.poll() is not None:
                self.tts_process = None
                return
            self.tts_interrupted = True
            spoken_excerpt = self.last_spoken_text[:220].replace("\n", " ").strip()
            self.interruption_note = (
                f"Assistant speech was interrupted ({reason}). "
                f"Spoken content that was cut off: {spoken_excerpt or '[none]'}. "
                "Continue from current context."
            )
            try:
                proc.terminate()
            except Exception:
                pass
            self.tts_process = None
        self.after(0, lambda: self.status_var.set("Speech interrupted by user"))

    def _speak_text(self, text: str) -> None:
        if not self.settings.get("live_tts", False):
            self._append_console("[voice] live TTS is disabled in Settings")
            return
        cleaned = text.strip()
        if not cleaned:
            return

        self.last_spoken_text = cleaned
        worker = threading.Thread(target=self._tts_worker, args=(cleaned,), daemon=True)
        worker.start()

    def _tts_worker(self, text: str) -> None:
        cmd = ["espeak-ng", "-s", "165", "-v", "en-us"]
        try:
            proc: subprocess.Popen[str] = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError:
            self.after(0, lambda: self._append_console("[voice] espeak-ng not found; live TTS unavailable"))
            return
        except Exception as exc:
            self.after(0, lambda: self._append_console(f"[voice] failed to start TTS: {exc}"))
            return

        with self.tts_lock:
            self.tts_process = proc
            self.tts_interrupted = False

        _stdout, stderr_text = proc.communicate(text)

        with self.tts_lock:
            interrupted = self.tts_interrupted
            if self.tts_process is proc:
                self.tts_process = None

        if interrupted:
            self.after(0, lambda: self._append_console("[voice] TTS interrupted by user speech"))
        elif proc.returncode not in (0, None):
            details = (stderr_text or "").strip() or f"exit={proc.returncode}"
            self.after(0, lambda: self._append_console(f"[voice] TTS failed: {details}"))

    def _on_enter_pressed(self, event: tk.Event) -> str | None:
        if event.state & 0x1:  # Shift+Enter for newline
            return None
        self.send_prompt()
        return "break"

    def _on_chat_frame_configure(self, _event: tk.Event) -> None:
        self.chat_canvas.configure(scrollregion=self.chat_canvas.bbox("all"))

    def _on_chat_canvas_configure(self, event: tk.Event) -> None:
        self.chat_canvas.itemconfigure(self.chat_window, width=event.width)

    def _scroll_to_bottom(self) -> None:
        self.update_idletasks()
        self.chat_canvas.yview_moveto(1.0)

    def _add_message(self, role: str, text: str) -> None:
        row = ttk.Frame(self.chat_list, style="Panel.TFrame")
        row.pack(fill=tk.X, pady=6)

        is_user = role == "user"
        holder = ttk.Frame(row, style="Panel.TFrame")
        holder.pack(anchor="e" if is_user else "w", fill=tk.X)

        bg = "#0F766E" if is_user else "#1F2937"
        fg = "#F8FAFC"
        title = "You" if is_user else "AI OS"

        bubble = tk.Frame(holder, bg=bg, padx=12, pady=9)
        bubble.pack(anchor="e" if is_user else "w")

        tk.Label(
            bubble,
            text=title,
            bg=bg,
            fg="#CFFAFE" if is_user else "#93C5FD",
            font=("Helvetica", 9, "bold"),
            anchor="w",
            justify=tk.LEFT,
        ).pack(anchor="w")

        tk.Label(
            bubble,
            text=text,
            bg=bg,
            fg=fg,
            font=("Helvetica", 11),
            wraplength=720,
            justify=tk.LEFT,
            anchor="w",
        ).pack(anchor="w", pady=(4, 0))

        self._bubble_refs.append(row)
        self._scroll_to_bottom()

    def _add_user_message(self, text: str) -> None:
        self._add_message("user", text)

    def _add_assistant_message(self, text: str) -> None:
        self._add_message("assistant", text)

    def _append_console(self, text: str) -> None:
        self.console.config(state=tk.NORMAL)
        self.console.insert(tk.END, text + "\n")
        self.console.see(tk.END)
        self.console.config(state=tk.DISABLED)

    def send_prompt(self) -> None:
        prompt = self.prompt_input.get("1.0", tk.END).strip()
        if not prompt:
            return
        self.prompt_input.delete("1.0", tk.END)
        self._send_prompt_text(prompt, source="typed")

    def _send_prompt_text(self, prompt: str, source: str) -> None:
        if self.pending_response:
            if source == "voice":
                self.voice_prompt_queue.put(prompt)
                self.status_var.set("Voice captured; queued")
            return

        speaker_label = "[voice] " if source == "voice" else ""
        self._add_user_message(f"{speaker_label}{prompt}")
        self.status_var.set("Thinking...")
        self.pending_response = True

        interruption_note = self.interruption_note
        self.interruption_note = ""
        run_prompt = prompt
        if bool(self.settings.get("auto_write_files", False)):
            run_prompt = (
                f"{prompt}\n\n"
                "If you want AI OS to auto-create/edit files, include fenced blocks like:\n"
                "```file:relative/or/absolute/path.ext\n"
                "...file content...\n"
                "```"
            )

        worker = threading.Thread(
            target=self._run_agent_prompt,
            args=(run_prompt, interruption_note),
            daemon=True,
        )
        worker.start()

    def _run_agent_prompt(self, prompt: str, interruption_note: str) -> None:
        result = self.agent.run(
            prompt,
            mode=self.model_mode.get(),
            enabled_models=self._enabled_models(),
            model_roles=self._model_roles(),
            interruption_note=interruption_note,
        )
        self.after(0, lambda: self._on_agent_result(result.used_mode, result.response))

    def _on_agent_result(self, used_mode: str, response: str) -> None:
        self._add_assistant_message(response)
        self._auto_write_files_from_response(response)
        self.status_var.set(f"Ready ({used_mode})")
        self.pending_response = False
        self._speak_text(response)

        if not self.voice_prompt_queue.empty():
            try:
                next_prompt = self.voice_prompt_queue.get_nowait()
                self._send_prompt_text(next_prompt, source="voice")
            except queue.Empty:
                pass

    def clear_chat(self) -> None:
        for bubble in self._bubble_refs:
            bubble.destroy()
        self._bubble_refs.clear()
        self.status_var.set("Ready")
        self._add_assistant_message(
            "New chat started. I can be interrupted by speech when Live STT/TTS is enabled in Settings."
        )

    def run_code(self) -> None:
        run_dir = Path("/tmp/ai_os_runs")
        if not self._is_path_allowed(run_dir):
            self._append_console("[run denied] /tmp/ai_os_runs is not allowed by file permissions")
            self.status_var.set("Run denied by permissions")
            return
        source = self.code_editor.get("1.0", tk.END)
        ext = ".py"
        if self.current_file_path:
            ext = self.current_file_path.suffix.lower() or ".py"
        if not self._is_extension_allowed(Path(f"snippet{ext}")):
            self._append_console(f"[run denied] extension not allowed: {ext}")
            self.status_var.set("Run denied by file-type permissions")
            return
        self._append_console("Running code...")
        try:
            result = run_source_code(source, file_extension=ext)
        except subprocess.TimeoutExpired:
            self._append_console("[run timeout] execution exceeded 20s")
            return
        except Exception as exc:
            self._append_console(f"[run error] {exc}")
            return

        self._append_console(f"$ {result.command}")
        if result.stdout.strip():
            self._append_console(result.stdout.rstrip("\n"))
        if result.stderr.strip():
            self._append_console(result.stderr.rstrip("\n"))
        self._append_console(f"[exit code {result.return_code}] ({result.file_path})")

    def send_editor_to_ai(self) -> None:
        code = self.code_editor.get("1.0", tk.END).strip()
        if not code:
            return

        prompt = (
            "Review and improve this Python code. Return a revised version plus short reasoning.\n\n"
            f"```python\n{code}\n```"
        )
        self.prompt_input.delete("1.0", tk.END)
        self.prompt_input.insert(tk.END, prompt)
        self.send_prompt()

    def open_file(self) -> None:
        path = filedialog.askopenfilename(title="Open source file")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as file_handle:
                content = file_handle.read()
        except Exception as exc:
            messagebox.showerror("Open file failed", str(exc))
            return
        self.code_editor.delete("1.0", tk.END)
        self.code_editor.insert(tk.END, content)
        self.current_file_path = Path(path).resolve()
        self._append_console(f"Loaded file: {path}")

    def save_file(self) -> None:
        path = filedialog.asksaveasfilename(title="Save source file")
        if not path:
            return
        content = self.code_editor.get("1.0", tk.END)
        try:
            with open(path, "w", encoding="utf-8") as file_handle:
                file_handle.write(content)
        except Exception as exc:
            messagebox.showerror("Save file failed", str(exc))
            return
        self.current_file_path = Path(path).resolve()
        self._append_console(f"Saved file: {path}")

    def destroy(self) -> None:
        self._stop_live_stt()
        self._interrupt_tts("app shutdown")
        super().destroy()


def main() -> None:
    app = AIOSApp()
    app.mainloop()


if __name__ == "__main__":
    main()
