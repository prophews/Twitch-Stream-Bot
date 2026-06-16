import ttkbootstrap as tb
from ttkbootstrap.constants import *
from ttkbootstrap.widgets import ToolTip
from ttkbootstrap.widgets.scrolled import ScrolledText
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import logging
import asyncio
import json
import os
import sys
import webbrowser
from pathlib import Path

from settings import (
    APP_VERSION,
    load_settings,
    save_settings,
    BotSettings,
    DEFAULT_BUILTIN_RESPONSES,
    get_config_path,
    get_runtime_data_dir,
    get_runtime_root,
    profile_settings_payload,
    apply_profile_settings,
)
from bot import Bot
from release_updates import fetch_latest_release, is_installed_copy, is_newer_version


def get_app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


class TkTextHandler(logging.Handler):
    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget

    def emit(self, record):
        message = self.format(record)

        def append():
            try:
                self.text_widget.configure(state="normal")
                self.text_widget.insert("end", message + "\n")
                self.text_widget.see("end")
                self.text_widget.configure(state="disabled")
            except tk.TclError:
                pass

        try:
            self.text_widget.after(0, append)
        except tk.TclError:
            pass


class VerticalScrolledFrame(tb.Frame):
    def __init__(self, parent, *args, **kwargs):
        super().__init__(parent, *args, **kwargs)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        self.scrollbar = tb.Scrollbar(self, orient=VERTICAL, command=self.canvas.yview)
        self.interior = tb.Frame(self.canvas)

        self.window_id = self.canvas.create_window((0, 0), window=self.interior, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side=LEFT, fill=BOTH, expand=True)
        self.scrollbar.pack(side=RIGHT, fill=Y)

        self.interior.bind("<Configure>", self._sync_scroll_region)
        self.canvas.bind("<Configure>", self._sync_canvas_width)
        self.canvas.bind("<Enter>", self._bind_mousewheel)
        self.canvas.bind("<Leave>", self._unbind_mousewheel)

    def _sync_scroll_region(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _sync_canvas_width(self, event):
        self.canvas.itemconfigure(self.window_id, width=event.width)

    def _bind_mousewheel(self, _event=None):
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _unbind_mousewheel(self, _event=None):
        self.canvas.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")



class BotApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"Twitch Stream Bot {APP_VERSION}")
        self.root.geometry("1250x900")
        self.root.wm_minsize(1100, 700)
        
        self.bot_instance = None
        self.bot_thread = None
        self.settings = load_settings()
        self.vol_var = tk.IntVar(value=self.settings.vlc_sr_volume)
        self._autosave_after_id = None
        self._autosave_suspended = True
        self._last_q_id = None
        self.local_library_entries = []
        self.filtered_local_library_entries = []
        
        self._build_ui()
        self._register_automation_autosave()
        self._autosave_suspended = False
        self._setup_logging()
        self._refresh_local_library(log_result=False)
        self._sync_ui()

    def _setup_logging(self):
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        
        # Clear existing handlers first
        if root_logger.hasHandlers():
            root_logger.handlers.clear()

        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%H:%M:%S'
        )

        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        root_logger.addHandler(stream_handler)

        log_dir = get_runtime_data_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / "bot.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

        logging.getLogger("StandaloneBot").info(
            "Runtime settings: root=%s config=%s",
            get_runtime_root(),
            get_config_path(),
        )

        if hasattr(self, "log_output"):
            gui_handler = TkTextHandler(self.log_output)
            gui_handler.setFormatter(formatter)
            root_logger.addHandler(gui_handler)

    def _build_ui(self):
        self.notebook = tb.Notebook(self.root, bootstyle="info")
        self.notebook.pack(fill=BOTH, expand=True, padx=10, pady=10)
        
        self.dash_frame = tb.Frame(self.notebook)
        self.settings_frame = tb.Frame(self.notebook)
        self.automation_frame = tb.Frame(self.notebook)
        self.setup_frame = tb.Frame(self.notebook)
        
        self.notebook.add(self.dash_frame, text=" Dashboard ")
        self.notebook.add(self.settings_frame, text=" Settings ")
        self.notebook.add(self.automation_frame, text=" Loyalty & Automation ")
        self.notebook.add(self.setup_frame, text=" Setup & Instructions ")

        # Both dashboard and automation-tab switches share this variable.
        self.automation_enabled_var = tk.BooleanVar(
            value=self.settings.automation_enabled
        )

        self._build_dashboard()
        self._build_settings()
        self._build_automation()
        self._build_setup()

    def _build_dashboard(self):
        self.dashboard_pane = tb.Panedwindow(self.dash_frame, orient=VERTICAL)
        self.dashboard_pane.pack(fill=BOTH, expand=True)

        dashboard_main = tb.Frame(self.dashboard_pane)
        log_lb = tb.Labelframe(
            self.dashboard_pane, text="Activity Log", padding=10
        )
        self.dashboard_pane.add(dashboard_main, weight=4)
        self.dashboard_pane.add(log_lb, weight=1)

        top_pane = tb.Panedwindow(dashboard_main, orient=HORIZONTAL)
        top_pane.pack(fill=BOTH, expand=True)
        
        left_frame = tb.Frame(top_pane, padding=10)
        right_frame = tb.Frame(top_pane, padding=10)
        
        top_pane.add(left_frame, weight=1)
        top_pane.add(right_frame, weight=2)
        
        # --- Left Side: Status & Controls ---
        status_lb = tb.Labelframe(left_frame, text="Bot Control", padding=10)
        status_lb.pack(fill=X, pady=(0, 10))
        
        self.start_btn = tb.Button(status_lb, text="Start Bot", bootstyle=SUCCESS, command=self.start_bot)
        self.start_btn.pack(fill=X, pady=5)
        
        self.stop_btn = tb.Button(status_lb, text="Stop Bot", bootstyle=DANGER, command=self.stop_bot, state=DISABLED)
        self.stop_btn.pack(fill=X, pady=5)
        
        feature_toggles = tb.Frame(status_lb)
        feature_toggles.pack(fill=X, pady=10)
        self.sr_enabled_var = tb.BooleanVar(value=True)
        self.sr_cb = tb.Checkbutton(
            feature_toggles,
            text="Accept Song Requests",
            variable=self.sr_enabled_var,
            bootstyle="round-toggle",
            command=self._toggle_sr,
        )
        self.sr_cb.pack(fill=X, anchor="w")
        self.dashboard_automation_cb = tb.Checkbutton(
            feature_toggles,
            text="Enable Custom Commands & Timers",
            variable=self.automation_enabled_var,
            bootstyle="round-toggle",
        )
        self.dashboard_automation_cb.pack(fill=X, anchor="w", pady=(8, 0))

        update_row = tb.Frame(status_lb)
        update_row.pack(fill=X, pady=(0, 8))
        self.update_status_var = tk.StringVar(value=f"Installed version: {APP_VERSION}")
        tb.Button(
            update_row,
            text="Check for Updates",
            bootstyle="info",
            command=self._check_for_updates,
        ).pack(side=LEFT)
        tb.Label(
            update_row,
            textvariable=self.update_status_var,
            foreground="#8b9299",
            font=("Helvetica", 8),
        ).pack(side=LEFT, padx=(8, 0))

        profile_lb = tb.Labelframe(status_lb, text="Profiles", padding=8)
        profile_lb.pack(fill=X, pady=(2, 4))

        profile_select_row = tb.Frame(profile_lb)
        profile_select_row.pack(fill=X)
        self.profile_var = tk.StringVar()
        self.profile_combo = tb.Combobox(
            profile_select_row,
            textvariable=self.profile_var,
            values=sorted(self.settings.profiles),
            state="normal"
        )
        self.profile_combo.pack(side=LEFT, fill=X, expand=True, padx=(0, 6))
        tb.Button(
            profile_select_row,
            text="Apply",
            bootstyle="primary",
            command=self._apply_selected_profile,
            width=9
        ).pack(side=RIGHT)

        profile_action_row = tb.Frame(profile_lb)
        profile_action_row.pack(fill=X, pady=(6, 0))
        tb.Button(
            profile_action_row,
            text="Save Current",
            bootstyle="success",
            command=self._save_current_profile
        ).pack(side=LEFT, fill=X, expand=True, padx=(0, 3))
        tb.Button(
            profile_action_row,
            text="Delete",
            bootstyle="outline-danger",
            command=self._delete_selected_profile
        ).pack(side=LEFT, fill=X, expand=True, padx=(3, 0))

        self.profile_status_var = tk.StringVar(value="Type a name to save the current stream layout.")
        tb.Label(
            profile_lb,
            textvariable=self.profile_status_var,
            foreground="#8b9299",
            wraplength=290,
            font=("Helvetica", 8)
        ).pack(anchor="w", pady=(5, 0))
        
        np_lb = tb.Labelframe(left_frame, text="Now Playing", padding=10)
        np_lb.pack(fill=X)
        
        self.np_title_var = tb.StringVar(value="Waiting...")
        self.np_req_var = tb.StringVar(value="")
        
        tb.Label(
            np_lb,
            textvariable=self.np_title_var,
            font=("Helvetica", 13, "bold"),
            wraplength=300
        ).pack(padx=6, pady=(6, 2))
        tb.Label(
            np_lb,
            textvariable=self.np_req_var,
            font=("Helvetica", 9),
            foreground="#8b9299"
        ).pack(pady=(0, 7))
        
        # Player Controls
        ctrl_frame = tb.Frame(np_lb)
        ctrl_frame.pack(fill=X, padx=3, pady=(0, 9))
        for column in range(3):
            ctrl_frame.columnconfigure(column, weight=1, uniform="player_controls")
        
        self.play_btn = tb.Button(
            ctrl_frame,
            text="Pause",
            bootstyle="outline-primary",
            command=self._gui_play_pause,
            state=DISABLED
        )
        self.play_btn.grid(row=0, column=0, sticky="ew", padx=3)

        self.hide_toggle_btn = tb.Button(
            ctrl_frame,
            text="Hide / Show",
            bootstyle="outline-info",
            command=self._gui_toggle_window_hidden,
            state=DISABLED
        )
        self.hide_toggle_btn.grid(row=0, column=1, sticky="ew", padx=3)

        self.skip_btn = tb.Button(
            ctrl_frame,
            text="Skip",
            bootstyle="outline-warning",
            command=self._gui_skip,
            state=DISABLED
        )
        self.skip_btn.grid(row=0, column=2, sticky="ew", padx=3)

        tb.Separator(np_lb).pack(fill=X, padx=6, pady=(0, 7))
        tb.Label(
            np_lb,
            text="OBS DISPLAY",
            font=("Helvetica", 8, "bold"),
            foreground="#707880"
        ).pack(anchor="w", padx=7, pady=(0, 4))

        display_ctrl_frame = tb.Frame(np_lb)
        display_ctrl_frame.pack(fill=X, padx=3, pady=(0, 5))
        for column in range(3):
            display_ctrl_frame.columnconfigure(column, weight=1, uniform="display_controls")

        self.overlay_btn = tb.Button(
            display_ctrl_frame,
            text="Title: Off",
            bootstyle="outline-info",
            command=self._gui_toggle_title,
            state=DISABLED
        )
        self.overlay_btn.grid(row=0, column=0, sticky="ew", padx=3)

        self.time_toggle_btn = tb.Button(
            display_ctrl_frame,
            text="Time: Off",
            bootstyle="outline-info",
            command=self._gui_toggle_time,
            state=DISABLED
        )
        self.time_toggle_btn.grid(row=0, column=1, sticky="ew", padx=3)

        self.progress_toggle_btn = tb.Button(
            display_ctrl_frame,
            text="Bar: Off",
            bootstyle="outline-info",
            command=self._gui_toggle_progress_bar,
            state=DISABLED
        )
        self.progress_toggle_btn.grid(row=0, column=2, sticky="ew", padx=3)

        # Seek bar
        self.seek_var = tk.DoubleVar()
        self.seek_slider = tb.Scale(np_lb, from_=0, to=100, variable=self.seek_var, bootstyle="info", state=DISABLED)
        self.seek_slider.pack(fill=X, padx=3, pady=(7, 1))
        
        self.time_lbl = tb.Label(np_lb, text="0:00 / 0:00", font=("Helvetica", 9), foreground="gray")
        self.time_lbl.pack(anchor="e", padx=4, pady=(0, 3))

        volume_row = tb.Frame(np_lb)
        volume_row.pack(fill=X, padx=4, pady=(2, 3))
        self.dashboard_volume_label_var = tk.StringVar(
            value=f"Volume: {self.vol_var.get()}%"
        )
        tb.Label(
            volume_row,
            textvariable=self.dashboard_volume_label_var,
            width=12,
            anchor="w",
        ).pack(side=LEFT)
        tb.Scale(
            volume_row,
            from_=0,
            to=100,
            orient=tk.HORIZONTAL,
            variable=self.vol_var,
        ).pack(side=LEFT, fill=X, expand=True, padx=(8, 0))
        self.vol_var.trace_add(
            "write",
            lambda *_: self.dashboard_volume_label_var.set(
                f"Volume: {self.vol_var.get()}%"
            ),
        )

        self.is_dragging_slider = False
        self.seek_slider.bind("<ButtonPress-1>", lambda e: setattr(self, 'is_dragging_slider', True))
        self.seek_slider.bind("<ButtonRelease-1>", self._gui_seek_release)
        
        # --- Left Side Bottom: History ---
        hist_lb = tb.Labelframe(left_frame, text="Recently Played / Skipped", padding=10)
        hist_lb.pack(fill=BOTH, expand=True, pady=(10, 0))
        
        hist_columns = ("title", "req")
        self.hist_tree = tb.Treeview(
            hist_lb,
            columns=hist_columns,
            show="headings",
            bootstyle=SECONDARY,
            height=5,
        )
        self.hist_tree.heading("title", text="Title")
        self.hist_tree.heading("req", text="Requested By")
        self.hist_tree.column("title", width=150, stretch=True)
        self.hist_tree.column("req", width=160, minwidth=160, stretch=False)
        
        hist_scrollbar = tb.Scrollbar(hist_lb, orient=VERTICAL, command=self.hist_tree.yview)
        self.hist_tree.configure(yscrollcommand=hist_scrollbar.set)
        hist_scrollbar.pack(side=RIGHT, fill=Y)

        self.hist_tree.pack(side=LEFT, fill=BOTH, expand=True)

        # --- Right Side: Queue + Local Library ---
        right_pane = tb.Panedwindow(right_frame, orient=VERTICAL)
        right_pane.pack(fill=BOTH, expand=True)

        queue_lb = tb.Labelframe(right_pane, text="Song Queue Request Viewer", padding=10)
        library_lb = tb.Labelframe(right_pane, text="Local Library", padding=10)
        right_pane.add(queue_lb, weight=3)
        right_pane.add(library_lb, weight=2)

        q_btn_frame = tb.Frame(queue_lb)
        q_btn_frame.pack(fill=X, side=BOTTOM, pady=(10, 0))
        
        self.remove_btn = tb.Button(q_btn_frame, text="Remove Selected", bootstyle="outline-warning", command=self._gui_remove_selected, state=DISABLED)
        self.remove_btn.pack(side=LEFT, padx=5)

        self.move_up_btn = tb.Button(q_btn_frame, text="▲ Move Up", bootstyle="outline-info", command=self._gui_move_up, state=DISABLED)
        self.move_up_btn.pack(side=LEFT, padx=5)

        self.move_down_btn = tb.Button(q_btn_frame, text="▼ Move Down", bootstyle="outline-info", command=self._gui_move_down, state=DISABLED)
        self.move_down_btn.pack(side=LEFT, padx=5)
        
        self.clear_btn = tb.Button(q_btn_frame, text="Clear Queue", bootstyle="outline-danger", command=self._gui_clear_queue, state=DISABLED)
        self.clear_btn.pack(side=RIGHT, padx=5)

        columns = ("pos", "title", "req")
        self.tree = tb.Treeview(queue_lb, columns=columns, show="headings", bootstyle=INFO)
        self.tree.heading("pos", text="#")
        self.tree.heading("title", text="Title")
        self.tree.heading("req", text="Requested By")
        self.tree.column("pos", width=40, minwidth=40, anchor=CENTER, stretch=False)
        self.tree.column("title", width=200, stretch=True)
        self.tree.column("req", width=160, minwidth=160, stretch=False)
        
        scrollbar = tb.Scrollbar(queue_lb, orient=VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=RIGHT, fill=Y)

        self.tree.pack(side=LEFT, fill=BOTH, expand=True)

        library_config = tb.Frame(library_lb)
        library_config.pack(fill=X, pady=(0, 8))

        self.local_library_enabled_var = tk.BooleanVar(value=self.settings.local_library_enabled)
        tb.Checkbutton(
            library_config,
            text="Enable Local Library",
            variable=self.local_library_enabled_var,
            bootstyle="round-toggle",
            command=self._local_library_toggle_changed
        ).grid(row=0, column=0, sticky="w", pady=(0, 6))

        self.local_library_root_var = tk.StringVar(value=self.settings.local_library_root)
        self.local_library_root_var.trace_add(
            "write",
            lambda *_: (
                self._mark_local_library_dirty(),
                self._schedule_autosave(),
            ),
        )
        tb.Label(library_config, text="Folder:").grid(row=1, column=0, sticky="w", padx=(0, 6))
        tb.Entry(library_config, textvariable=self.local_library_root_var).grid(row=1, column=1, sticky="we", padx=(0, 8))
        tb.Button(
            library_config,
            text="Browse",
            bootstyle="outline-primary",
            command=self._browse_local_library_root
        ).grid(row=1, column=2, sticky="e")
        library_config.columnconfigure(1, weight=1)

        library_top = tb.Frame(library_lb)
        library_top.pack(fill=X, pady=(0, 8))

        self.local_library_status_var = tk.StringVar(value="No local library configured.")
        tb.Label(library_top, textvariable=self.local_library_status_var, foreground="gray").pack(side=LEFT)

        self.refresh_library_btn = tb.Button(
            library_top,
            text="Save & Refresh",
            bootstyle="primary",
            command=self._refresh_local_library
        )
        self.refresh_library_btn.pack(side=RIGHT)

        search_row = tb.Frame(library_lb)
        search_row.pack(fill=X, pady=(0, 8))
        tb.Label(search_row, text="Search:").pack(side=LEFT, padx=(0, 6))
        self.local_library_search_var = tk.StringVar()
        self.local_library_search_var.trace_add("write", lambda *_: self._refresh_local_library_view())
        tb.Entry(search_row, textvariable=self.local_library_search_var).pack(side=LEFT, fill=X, expand=True)

        library_columns = ("title", "path")
        self.local_library_tree = tb.Treeview(
            library_lb,
            columns=library_columns,
            show="headings",
            bootstyle=SECONDARY,
            selectmode="extended"
        )
        self.local_library_tree.heading("title", text="Title")
        self.local_library_tree.heading("path", text="Path")
        self.local_library_tree.column("title", width=240, stretch=True)
        self.local_library_tree.column("path", width=280, stretch=True)

        library_btn_row = tb.Frame(library_lb)
        library_btn_row.pack(fill=X, side=BOTTOM, pady=(8, 0))
        self.add_local_btn = tb.Button(
            library_btn_row,
            text="Add Selected To Queue",
            bootstyle="success",
            command=self._gui_add_selected_local_files,
            state=DISABLED
        )
        self.add_local_btn.pack(side=LEFT)

        library_table_frame = tb.Frame(library_lb)
        library_table_frame.pack(fill=BOTH, expand=True)
        library_scrollbar = tb.Scrollbar(library_table_frame, orient=VERTICAL, command=self.local_library_tree.yview)
        self.local_library_tree.configure(yscrollcommand=library_scrollbar.set)
        library_scrollbar.pack(side=RIGHT, fill=Y)
        self.local_library_tree.pack(side=LEFT, fill=BOTH, expand=True)
        self.local_library_tree.bind(
            "<<TreeviewSelect>>",
            lambda _event: self.add_local_btn.config(
                state=tk.NORMAL if self.local_library_tree.selection() and self.bot_instance else tk.DISABLED
            )
        )

        self.log_output = ScrolledText(log_lb, height=3, autohide=True)
        self.log_output.pack(fill=BOTH, expand=True)
        self.log_output.text.configure(state="disabled", wrap="word")
        self.log_output = self.log_output.text
        self.root.after_idle(self._set_initial_dashboard_split)

    def _set_initial_dashboard_split(self):
        """Reserve a useful Activity Log height on the first dashboard layout."""
        try:
            self.root.update_idletasks()
            total_height = self.dashboard_pane.winfo_height()
            if total_height > 350:
                log_height = max(90, min(110, total_height // 8))
                self.dashboard_pane.sashpos(0, total_height - log_height)
        except tk.TclError:
            pass


    def _build_settings(self):
        save_bar = tb.Frame(self.settings_frame, padding=(10, 10, 10, 0))
        save_bar.pack(fill=X)
        self.save_settings_status_var = tk.StringVar(
            value="Settings autosave after valid changes."
        )
        self.save_settings_btn = tb.Button(
            save_bar,
            text="Save Now (Autosaves)",
            command=self._apply_settings,
            bootstyle=WARNING,
            width=18
        )
        self.save_settings_btn.pack(side=LEFT)
        tb.Label(save_bar, textvariable=self.save_settings_status_var, foreground="gray").pack(side=LEFT, padx=12)

        scrolled = VerticalScrolledFrame(self.settings_frame)
        scrolled.pack(fill=BOTH, expand=True)
        container = tb.Frame(scrolled.interior, padding=10)
        container.pack(fill=BOTH, expand=True)
        
        cred_lf = tb.Labelframe(container, text="Twitch Setup & Connection Data", padding=10)
        cred_lf.pack(fill=X, pady=5)
        ToolTip(cred_lf, "Connects the bot to your Twitch Chat and defines your local web server port.")

        tb.Label(cred_lf, text="Web Server Port:", font=("Helvetica", 11)).grid(row=0, column=0, sticky="w", pady=8)
        self.port_var = tk.IntVar(value=self.settings.web_server_port)
        self._watch_setting_var(self.port_var)
        tb.Entry(cred_lf, textvariable=self.port_var, width=10).grid(row=0, column=1, sticky="w", padx=20, pady=8)
        
        tb.Label(cred_lf, text="Twitch Channel:", font=("Helvetica", 11)).grid(row=1, column=0, sticky="w", pady=8)
        self.channel_var = tk.StringVar(value=self.settings.channel)
        self._watch_setting_var(self.channel_var)
        tb.Entry(cred_lf, textvariable=self.channel_var, width=50).grid(row=1, column=1, sticky="w", padx=20, pady=8)

        tb.Label(cred_lf, text="OAuth Token:", font=("Helvetica", 11)).grid(row=2, column=0, sticky="w", pady=8)
        self.oauth_var = tk.StringVar(value=self.settings.oauth_token)
        self._watch_setting_var(self.oauth_var)
        oauth_entry = tb.Entry(cred_lf, textvariable=self.oauth_var, show="*", width=50)
        oauth_entry.grid(row=2, column=1, sticky="w", padx=20, pady=8)
        ToolTip(oauth_entry, "Get a 'Custom Bot Token' from twitchtokengenerator.com. Paste the ACCESS TOKEN here.")

        # Queue Logic settings
        queue_lf = tb.Labelframe(container, text="Queue Logic", padding=10)
        queue_lf.pack(fill=X, pady=5)
        
        self.fair_queue_var = tk.BooleanVar(value=self.settings.use_fair_queue)
        self._watch_setting_var(self.fair_queue_var)
        fq_cb = tb.Checkbutton(queue_lf, text="Use Fair Play Queueing (distributes plays evenly between users)", variable=self.fair_queue_var, bootstyle="round-toggle")
        fq_cb.pack(anchor="w", pady=5)

        audio_lf = tb.Labelframe(container, text="Media Player & Visuals", padding=10)
        audio_lf.pack(fill=X, pady=5)
        ToolTip(audio_lf, "Configures how the Song Request browser source looks and sounds in OBS.")
        
        tb.Label(audio_lf, text="Master Volume (%):", font=("Helvetica", 11)).grid(row=0, column=0, sticky="w", pady=5)
        self._watch_setting_var(self.vol_var)
        scale = tb.Scale(audio_lf, from_=0, to=100, orient=tk.HORIZONTAL, variable=self.vol_var, length=300)
        scale.grid(row=0, column=1, columnspan=3, sticky="w", padx=20, pady=5)
        
        tb.Label(audio_lf, text="Small SR Window Position:", font=("Helvetica", 11)).grid(row=1, column=0, sticky="w", pady=5)
        self.pos_var = tk.StringVar(value=self.settings.sr_window_position)
        self._watch_setting_var(self.pos_var)
        pos_cb = tb.Combobox(audio_lf, textvariable=self.pos_var, state="readonly", values=["Bottom Left", "Bottom Right", "Top Left", "Top Right"])
        pos_cb.grid(row=1, column=1, columnspan=3, sticky="w", padx=20, pady=5)
        ToolTip(pos_cb, "The corner the video snaps to when not in Fullscreen mode.")

        tb.Label(audio_lf, text="Background Transparency (%):", font=("Helvetica", 11)).grid(row=2, column=0, sticky="w", pady=5)
        self.bg_op_var = tk.IntVar(value=self.settings.sr_bg_opacity)
        self._watch_setting_var(self.bg_op_var)
        op_scale = tb.Scale(audio_lf, from_=0, to=100, orient=tk.HORIZONTAL, variable=self.bg_op_var, length=300)
        op_scale.grid(row=2, column=1, columnspan=3, sticky="w", padx=20, pady=5)
        ToolTip(op_scale, "0% = Fully transparent (no black bars), 100% = Solid black background.")

        tb.Label(audio_lf, text="Custom Window Size (WxH):", font=("Helvetica", 11)).grid(row=3, column=0, sticky="w", pady=5)
        size_frame = tb.Frame(audio_lf)
        size_frame.grid(row=3, column=1, sticky="w", padx=20, pady=5)
        self.w_var = tk.IntVar(value=self.settings.sr_window_width)
        self.h_var = tk.IntVar(value=self.settings.sr_window_height)
        self._watch_setting_var(self.w_var)
        self._watch_setting_var(self.h_var)
        tb.Entry(size_frame, textvariable=self.w_var, width=6).pack(side=LEFT)
        tb.Label(size_frame, text="x").pack(side=LEFT, padx=5)
        tb.Entry(size_frame, textvariable=self.h_var, width=6).pack(side=LEFT)
        ToolTip(size_frame, "Width and Height of the small window. Default: 640x360.")

        tb.Label(audio_lf, text="OBS Title / Time Font Size:", font=("Helvetica", 11)).grid(row=4, column=0, sticky="w", pady=5)
        font_frame = tb.Frame(audio_lf)
        font_frame.grid(row=4, column=1, sticky="w", padx=20, pady=5)
        self.title_font_size_var = tk.IntVar(value=self.settings.sr_title_font_size)
        self.time_font_size_var = tk.IntVar(value=self.settings.sr_time_font_size)
        self._watch_setting_var(self.title_font_size_var)
        self._watch_setting_var(self.time_font_size_var)
        tb.Label(font_frame, text="Title").pack(side=LEFT)
        tb.Entry(font_frame, textvariable=self.title_font_size_var, width=5).pack(side=LEFT, padx=(6, 14))
        tb.Label(font_frame, text="Time").pack(side=LEFT)
        tb.Entry(font_frame, textvariable=self.time_font_size_var, width=5).pack(side=LEFT, padx=(6, 0))
        ToolTip(font_frame, "Font sizes in pixels for the optional OBS title and time display. Recommended range: 8-32.")

        # --- Command Aliases ---
        alias_lf = tb.Labelframe(container, text="Twitch Chat Command Aliases (no ! prefix)", padding=10)
        alias_lf.pack(fill=X, pady=5)
        ToolTip(alias_lf, "Customize the names of the commands users type in Twitch chat.")
        
        alias_defs = [
            ("Song Request", "cmd_sr"),
            ("Skip", "cmd_skip"),
            ("Pause / Resume", "cmd_pause"),
            ("Play / Resume", "cmd_play"),
            ("Hide Window", "cmd_hide"),
            ("Show Window", "cmd_show"),
            ("Queue", "cmd_queue"),
            ("Wrong Song", "cmd_wrongsong"),
            ("Clear Queue", "cmd_clearqueue"),
            ("Fullscreen Toggle", "cmd_full"),
            ("Toggle Song Title", "cmd_info"),
            ("SR On", "cmd_sron"),
            ("SR Off", "cmd_sroff"),
        ]
        self._alias_vars = {}
        for i, (label, attr) in enumerate(alias_defs):
            row, col = divmod(i, 3)
            tb.Label(alias_lf, text=label + ":", font=("Helvetica", 10)).grid(row=row*2, column=col, sticky="w", padx=10, pady=(8,0))
            var = tk.StringVar(value=getattr(self.settings, attr, ""))
            self._watch_setting_var(var)
            self._alias_vars[attr] = var
            tb.Entry(alias_lf, textvariable=var, width=14).grid(row=row*2+1, column=col, sticky="w", padx=10, pady=(0,6))

        num_rows = (len(alias_defs) + 2) // 3
        tb.Label(alias_lf, text="\u26a0 Command alias changes take effect after restarting the bot.",
                 foreground="#ffcc00").grid(row=num_rows*2, column=0, columnspan=3, sticky="w", padx=10, pady=(6,0))

    def _attach_tree_scrollbar(self, tree):
        table_frame = tree.master
        scrollbar = tb.Scrollbar(table_frame, orient=VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar.pack(side=RIGHT, fill=Y)

        def _on_mousewheel(event):
            tree.yview_scroll(int(-1 * (event.delta / 120)), "units")
            return "break"

        tree.bind("<Enter>", lambda _event: tree.bind_all("<MouseWheel>", _on_mousewheel))
        tree.bind("<Leave>", lambda _event: tree.unbind_all("<MouseWheel>"))

    def _build_automation(self):
        save_bar = tb.Frame(self.automation_frame, padding=(10, 10, 10, 0))
        save_bar.pack(fill=X)
        self.automation_status_var = tk.StringVar(
            value="Settings autosave after valid changes."
        )
        tb.Button(
            save_bar,
            text="Save Now (Autosaves)",
            command=self._apply_automation_settings,
            bootstyle=WARNING,
            width=22,
        ).pack(side=LEFT)
        tb.Label(
            save_bar, textvariable=self.automation_status_var, foreground="#aeb4ba"
        ).pack(side=LEFT, padx=12)

        scrolled = VerticalScrolledFrame(self.automation_frame)
        scrolled.pack(fill=BOTH, expand=True)
        container = tb.Frame(scrolled.interior, padding=10)
        container.pack(fill=BOTH, expand=True)

        earning = tb.Labelframe(container, text="Currency & Earning Rules", padding=12)
        earning.pack(fill=X, pady=5)
        self.loyalty_enabled_var = tk.BooleanVar(value=self.settings.loyalty_enabled)
        self.reward_commands_var = tk.BooleanVar(value=self.settings.reward_command_messages)
        tb.Checkbutton(
            earning,
            text="Enable loyalty points",
            variable=self.loyalty_enabled_var,
            bootstyle="round-toggle",
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        earning_specs = [
            ("Currency plural", "currency_name_var", self.settings.currency_name),
            ("Currency singular", "currency_singular_var", self.settings.currency_singular),
            ("Starting balance", "starting_balance_var", self.settings.starting_balance),
            ("Points per message", "points_per_message_var", self.settings.points_per_message),
            (
                "Message cooldown (seconds)",
                "message_reward_cooldown_var",
                self.settings.message_reward_cooldown_seconds,
            ),
            ("Active bonus points", "active_bonus_points_var", self.settings.active_bonus_points),
            (
                "Bonus interval (minutes)",
                "active_bonus_interval_var",
                self.settings.active_bonus_interval_minutes,
            ),
            (
                "Active window (minutes)",
                "active_user_window_var",
                self.settings.active_user_window_minutes,
            ),
            (
                "Subscriber multiplier",
                "subscriber_multiplier_var",
                self.settings.subscriber_points_multiplier,
            ),
            ("VIP multiplier", "vip_multiplier_var", self.settings.vip_points_multiplier),
            ("Mod multiplier", "mod_multiplier_var", self.settings.mod_points_multiplier),
        ]
        for index, (label, attribute, value) in enumerate(earning_specs):
            variable = tk.StringVar(value=str(value))
            setattr(self, attribute, variable)
            row = 1 + index // 2
            column = (index % 2) * 2
            tb.Label(earning, text=label).grid(
                row=row, column=column, sticky="w", padx=(0, 8), pady=4
            )
            tb.Entry(earning, textvariable=variable, width=22).grid(
                row=row, column=column + 1, sticky="w", padx=(0, 25), pady=4
            )
        tb.Checkbutton(
            earning,
            text="Reward command messages too",
            variable=self.reward_commands_var,
            bootstyle="round-toggle",
        ).grid(row=7, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self.loyalty_excluded_users_var = tk.StringVar(
            value=self.settings.loyalty_excluded_users
        )
        tb.Label(earning, text="Excluded users/bots (comma separated)").grid(
            row=8, column=0, sticky="w", pady=4
        )
        tb.Entry(
            earning,
            textvariable=self.loyalty_excluded_users_var,
            width=58,
        ).grid(row=8, column=1, columnspan=3, sticky="ew", padx=(0, 25), pady=4)
        tb.Label(
            earning,
            text=(
                "Active bonuses use recent chat activity. Twitch does not provide a reliable "
                "complete viewer list, so silent lurkers cannot be identified automatically."
            ),
            foreground="#aeb4ba",
            wraplength=930,
        ).grid(row=9, column=0, columnspan=4, sticky="w", pady=(7, 0))

        automation_controls = tb.Labelframe(
            container, text="Custom Commands & Timers", padding=12
        )
        automation_controls.pack(fill=X, pady=5)
        self.automation_tab_enabled_cb = tb.Checkbutton(
            automation_controls,
            text="Enable custom commands and timers",
            variable=self.automation_enabled_var,
            bootstyle="round-toggle",
        )
        self.automation_tab_enabled_cb.pack(anchor="w")
        tb.Label(
            automation_controls,
            text=(
                "This switch is synchronized with Commands & Timers on the Dashboard. "
                "When off, custom chat commands, timed messages, and their Streamer.bot "
                "actions will not run."
            ),
            foreground="#aeb4ba",
            wraplength=930,
        ).pack(anchor="w", pady=(6, 0))

        builtins = tb.Labelframe(container, text="Built-in Commands", padding=12)
        builtins.pack(fill=X, pady=5)
        builtin_specs = [
            ("Check balance", "cmd_balance_var", self.settings.cmd_balance),
            ("Leaderboard", "cmd_leaderboard_var", self.settings.cmd_leaderboard),
            ("Give points", "cmd_give_points_var", self.settings.cmd_give_points),
            ("Moderator add points", "cmd_add_points_var", self.settings.cmd_add_points),
            (
                "Moderator remove points",
                "cmd_remove_points_var",
                self.settings.cmd_remove_points,
            ),
            ("Gamble", "cmd_gamble_var", self.settings.cmd_gamble),
            ("Duel", "cmd_duel_var", self.settings.cmd_duel),
            ("Accept duel", "cmd_duel_accept_var", self.settings.cmd_duel_accept),
            (
                "Decline duel",
                "cmd_duel_decline_var",
                self.settings.cmd_duel_decline,
            ),
            (
                "Raffle entry",
                "cmd_raffle_enter_var",
                self.settings.cmd_raffle_enter,
            ),
        ]
        for index, (label, attribute, value) in enumerate(builtin_specs):
            variable = tk.StringVar(value=value)
            setattr(self, attribute, variable)
            row = index // 2
            column = (index % 2) * 2
            tb.Label(builtins, text=label).grid(
                row=row, column=column, sticky="w", padx=(0, 8), pady=4
            )
            tb.Entry(builtins, textvariable=variable, width=22).grid(
                row=row, column=column + 1, sticky="w", padx=(0, 25), pady=4
            )
        tb.Label(
            builtins,
            text=(
                "Use command words without !. Add/remove commands require Mod or "
                "Broadcaster."
            ),
            foreground="#aeb4ba",
        ).grid(
            row=(len(builtin_specs) + 1) // 2,
            column=0,
            columnspan=4,
            sticky="w",
            pady=(6, 0),
        )

        responses = tb.Labelframe(
            container, text="Built-in Chat Responses", padding=12
        )
        responses.pack(fill=X, pady=5)
        self.builtin_response_vars = {}
        response_specs = [
            ("Balance", "balance"),
            ("Leaderboard", "leaderboard"),
            ("Empty leaderboard", "leaderboard_empty"),
            ("Give points", "give_points"),
            ("Gamble win", "gamble_win"),
            ("Gamble loss", "gamble_loss"),
            ("Duel challenge", "duel_challenge"),
            ("Duel result", "duel_result"),
            ("Duel declined", "duel_decline"),
            ("Moderator adjustment", "points_adjusted"),
            ("Raffle started", "raffle_started"),
            ("Raffle countdown", "raffle_countdown"),
            ("Raffle entry", "raffle_entry"),
            ("Raffle winner", "raffle_winner"),
            ("Raffle winner without reward", "raffle_winner_no_reward"),
            ("Raffle no entries", "raffle_no_entries"),
            ("Raffle cancelled", "raffle_cancelled"),
        ]
        configured_responses = self.settings.builtin_responses
        for row, (label, response_name) in enumerate(response_specs):
            value = configured_responses.get(
                response_name, DEFAULT_BUILTIN_RESPONSES[response_name]
            )
            variable = tk.StringVar(value=value)
            self.builtin_response_vars[response_name] = variable
            tb.Label(responses, text=label, width=22).grid(
                row=row, column=0, sticky="w", padx=(0, 8), pady=3
            )
            tb.Entry(responses, textvariable=variable).grid(
                row=row, column=1, sticky="ew", pady=3
            )
        responses.columnconfigure(1, weight=1)
        tb.Label(
            responses,
            text=(
                "Placeholders use {name}. Common: {user}, {target}, {balance}, "
                "{amount}, {currency}. Duel: {challenger}, {opponent}, {winner}, "
                "{loser}. Leaderboard: {leaderboard}. Leave a response blank for silence."
            ),
            foreground="#aeb4ba",
            wraplength=930,
        ).grid(
            row=len(response_specs),
            column=0,
            columnspan=2,
            sticky="w",
            pady=(7, 0),
        )

        games = tb.Labelframe(container, text="Gambling, Duels & Raffles", padding=12)
        games.pack(fill=X, pady=5)
        self.gambling_enabled_var = tk.BooleanVar(
            value=self.settings.gambling_enabled
        )
        self.duels_enabled_var = tk.BooleanVar(value=self.settings.duels_enabled)
        self.raffle_enabled_var = tk.BooleanVar(value=self.settings.raffle_enabled)
        tb.Checkbutton(
            games,
            text="Enable gambling",
            variable=self.gambling_enabled_var,
            bootstyle="round-toggle",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        tb.Checkbutton(
            games,
            text="Enable viewer duels",
            variable=self.duels_enabled_var,
            bootstyle="round-toggle",
        ).grid(row=0, column=2, columnspan=2, sticky="w", pady=(0, 8))
        tb.Checkbutton(
            games,
            text="Enable raffles",
            variable=self.raffle_enabled_var,
            bootstyle="round-toggle",
        ).grid(row=0, column=4, columnspan=2, sticky="w", pady=(0, 8))
        game_specs = [
            ("Gamble minimum", "gamble_minimum_var", self.settings.gamble_minimum),
            ("Gamble maximum", "gamble_maximum_var", self.settings.gamble_maximum),
            (
                "Gamble win chance (%)",
                "gamble_win_chance_var",
                self.settings.gamble_win_chance_percent,
            ),
            (
                "Gamble payout multiplier",
                "gamble_payout_var",
                self.settings.gamble_payout_multiplier,
            ),
            (
                "Gamble cooldown (seconds)",
                "gamble_cooldown_var",
                self.settings.gamble_cooldown_seconds,
            ),
            ("Duel minimum", "duel_minimum_var", self.settings.duel_minimum),
            ("Duel maximum", "duel_maximum_var", self.settings.duel_maximum),
            (
                "Duel response timeout (seconds)",
                "duel_timeout_var",
                self.settings.duel_timeout_seconds,
            ),
            (
                "Duel cooldown (seconds)",
                "duel_cooldown_var",
                self.settings.duel_cooldown_seconds,
            ),
            (
                "Raffle duration (seconds)",
                "raffle_duration_var",
                self.settings.raffle_duration_seconds,
            ),
            (
                "Raffle reminder interval (seconds)",
                "raffle_countdown_interval_var",
                self.settings.raffle_countdown_interval_seconds,
            ),
            (
                "Raffle winner reward",
                "raffle_reward_var",
                self.settings.raffle_reward_points,
            ),
        ]
        for index, (label, attribute, value) in enumerate(game_specs):
            variable = tk.StringVar(value=str(value))
            setattr(self, attribute, variable)
            row = 1 + index // 2
            column = (index % 2) * 2
            tb.Label(games, text=label).grid(
                row=row, column=column, sticky="w", padx=(0, 8), pady=4
            )
            tb.Entry(games, textvariable=variable, width=22).grid(
                row=row, column=column + 1, sticky="w", padx=(0, 25), pady=4
            )
        tb.Label(
            games,
            text=(
                "Gamble accepts a number or 'all'. Duels transfer the wager from the "
                "loser to the winner. Raffles are streamer-started from this dashboard; "
                "viewers enter with the configured raffle command."
            ),
            foreground="#aeb4ba",
            wraplength=930,
        ).grid(row=7, column=0, columnspan=6, sticky="w", pady=(7, 0))

        raffle_controls = tb.Labelframe(container, text="Raffle Control", padding=12)
        raffle_controls.pack(fill=X, pady=5)
        raffle_top = tb.Frame(raffle_controls)
        raffle_top.pack(fill=X)
        self.raffle_title_var = tk.StringVar(value=self.settings.raffle_default_title)
        tb.Label(raffle_top, text="Title").pack(side=LEFT)
        tb.Entry(raffle_top, textvariable=self.raffle_title_var, width=34).pack(
            side=LEFT, padx=(6, 14)
        )
        tb.Label(raffle_top, text="Entry command").pack(side=LEFT)
        tb.Entry(raffle_top, textvariable=self.cmd_raffle_enter_var, width=16).pack(
            side=LEFT, padx=(6, 14)
        )
        tb.Button(
            raffle_top,
            text="Start Raffle",
            command=self._start_dashboard_raffle,
            bootstyle="success",
        ).pack(side=LEFT)
        tb.Button(
            raffle_top,
            text="Draw Winner",
            command=self._draw_dashboard_raffle,
            bootstyle="warning",
        ).pack(side=LEFT, padx=6)
        tb.Button(
            raffle_top,
            text="Cancel",
            command=self._cancel_dashboard_raffle,
            bootstyle="outline-danger",
        ).pack(side=LEFT)
        self.raffle_status_var = tk.StringVar(
            value="Start the bot first, then start a raffle when chat is connected."
        )
        tb.Label(
            raffle_controls,
            textvariable=self.raffle_status_var,
            foreground="#aeb4ba",
            wraplength=930,
        ).pack(anchor="w", pady=(8, 0))

        streamerbot = tb.Labelframe(container, text="Streamer.bot Connection", padding=12)
        streamerbot.pack(fill=X, pady=5)
        self.streamerbot_http_enabled_var = tk.BooleanVar(
            value=self.settings.streamerbot_http_enabled
        )
        self.streamerbot_http_url_var = tk.StringVar(
            value=self.settings.streamerbot_http_url
        )
        self.streamerbot_actions = []
        self.streamerbot_action_ids = {}
        self.streamerbot_action_names_by_id = {}
        tb.Checkbutton(
            streamerbot,
            text="Allow custom commands to execute Streamer.bot actions",
            variable=self.streamerbot_http_enabled_var,
            bootstyle="round-toggle",
        ).pack(anchor="w")
        url_row = tb.Frame(streamerbot)
        url_row.pack(fill=X, pady=(8, 0))
        tb.Label(url_row, text="HTTP DoAction URL").pack(side=LEFT)
        tb.Entry(
            url_row, textvariable=self.streamerbot_http_url_var, width=60
        ).pack(side=LEFT, padx=10)
        tb.Button(
            url_row,
            text="Refresh Actions",
            command=self._test_streamerbot_connection,
            bootstyle="primary",
        ).pack(side=LEFT)
        tb.Button(
            url_row,
            text="Test Selected Action",
            command=self._test_selected_streamerbot_action,
            bootstyle="success",
        ).pack(side=LEFT, padx=(6, 0))
        tb.Label(
            streamerbot,
            text=(
                "Default: http://127.0.0.1:7474/DoAction. Enable Streamer.bot's HTTP "
                "server and leave its Host at 127.0.0.1."
            ),
            foreground="#aeb4ba",
        ).pack(anchor="w", pady=(6, 0))

        commands = tb.Labelframe(container, text="Custom Commands", padding=12)
        commands.pack(fill=BOTH, expand=True, pady=5)
        self.custom_command_rules = [dict(rule) for rule in self.settings.custom_commands]
        custom_table_frame = tb.Frame(commands)
        custom_table_frame.pack(fill=X)
        self.custom_command_tree = ttk.Treeview(
            custom_table_frame,
            columns=("command", "cost", "permission", "action", "response"),
            show="headings",
            height=7,
        )
        for column, label, width in (
            ("command", "Command", 130),
            ("cost", "Cost", 60),
            ("permission", "Permission", 90),
            ("action", "Streamer.bot Action", 190),
            ("response", "Chat Response", 390),
        ):
            self.custom_command_tree.heading(column, text=label)
            self.custom_command_tree.column(column, width=width, anchor="w")
        self._attach_tree_scrollbar(self.custom_command_tree)
        self.custom_command_tree.bind(
            "<<TreeviewSelect>>", self._load_selected_custom_command
        )

        editor = tb.Frame(commands)
        editor.pack(fill=X, pady=(10, 0))
        self.custom_enabled_var = tk.BooleanVar(value=True)
        self.custom_name_var = tk.StringVar()
        self.custom_aliases_var = tk.StringVar()
        self.custom_permission_var = tk.StringVar(value="everyone")
        self.custom_cost_var = tk.StringVar(value="0")
        self.custom_cooldown_var = tk.StringVar(value="0")
        self.custom_user_cooldown_var = tk.StringVar(value="0")
        self.custom_action_var = tk.StringVar()
        self.custom_action_id_var = tk.StringVar()
        self.custom_response_var = tk.StringVar()
        editor.columnconfigure(1, weight=1)
        editor_specs = [
            ("Command", self.custom_name_var),
            ("Aliases (comma separated)", self.custom_aliases_var),
            ("Cost", self.custom_cost_var),
            ("Global cooldown seconds", self.custom_cooldown_var),
            ("Per-user cooldown seconds", self.custom_user_cooldown_var),
        ]
        for row, (label, variable) in enumerate(editor_specs):
            tb.Label(editor, text=label).grid(
                row=row, column=0, sticky="w", padx=(0, 8), pady=3
            )
            tb.Entry(editor, textvariable=variable, width=34).grid(
                row=row, column=1, sticky="ew", pady=3
            )
        tb.Label(editor, text="Streamer.bot action").grid(
            row=5, column=0, sticky="w", padx=(0, 8), pady=3
        )
        self.custom_action_combo = tb.Combobox(
            editor,
            textvariable=self.custom_action_var,
            values=(),
            width=31,
        )
        self.custom_action_combo.grid(
            row=5, column=1, sticky="ew", pady=3
        )
        self.custom_action_combo.bind(
            "<<ComboboxSelected>>", self._custom_action_selected
        )
        tb.Label(editor, text="Permission").grid(row=6, column=0, sticky="w", pady=3)
        permission_row = tb.Frame(editor)
        permission_row.grid(row=6, column=1, sticky="ew", pady=3)
        tb.Combobox(
            permission_row,
            textvariable=self.custom_permission_var,
            values=("everyone", "subscriber", "vip", "mod", "broadcaster"),
            state="readonly",
            width=18,
        ).pack(side=LEFT)
        tb.Checkbutton(
            permission_row,
            text="Command enabled",
            variable=self.custom_enabled_var,
            bootstyle="round-toggle",
        ).pack(side=LEFT, padx=(16, 0))
        tb.Label(editor, text="Chat response").grid(row=7, column=0, sticky="w", pady=3)
        tb.Entry(editor, textvariable=self.custom_response_var).grid(
            row=7, column=1, sticky="ew", pady=3
        )
        tb.Label(
            editor,
            text=(
                "Placeholders: {user}, {command}, {args}, {balance}, {cost}, "
                "{currency}, {currency_singular}. Blank response means silent."
            ),
            foreground="#aeb4ba",
            wraplength=820,
        ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(4, 8))

        button_row = tb.Frame(commands)
        button_row.pack(fill=X)
        tb.Button(
            button_row,
            text="Add / Update Command",
            command=self._save_custom_command_rule,
            bootstyle="success",
        ).grid(row=0, column=0, sticky="w", padx=(0, 6), pady=3)
        tb.Button(
            button_row,
            text="New",
            command=self._clear_custom_command_editor,
            bootstyle="primary",
        ).grid(row=0, column=1, sticky="w", padx=(0, 6), pady=3)
        tb.Button(
            button_row,
            text="Delete Selected",
            command=self._delete_custom_command_rule,
            bootstyle="outline-danger",
        ).grid(row=0, column=2, sticky="w", padx=(0, 6), pady=3)
        tb.Button(
            button_row,
            text="Export Rules",
            command=self._export_custom_commands,
            bootstyle="secondary",
        ).grid(row=1, column=0, sticky="w", padx=(0, 6), pady=3)
        tb.Button(
            button_row,
            text="Import Rules",
            command=self._import_custom_commands,
            bootstyle="secondary",
        ).grid(row=1, column=1, sticky="w", padx=(0, 6), pady=3)
        self._refresh_custom_command_tree()

        timers = tb.Labelframe(container, text="Timed Messages & Actions", padding=12)
        timers.pack(fill=BOTH, expand=True, pady=5)
        self.timed_message_rules = [dict(rule) for rule in self.settings.timed_messages]
        timer_table_frame = tb.Frame(timers)
        timer_table_frame.pack(fill=X)
        self.timer_tree = ttk.Treeview(
            timer_table_frame,
            columns=("name", "interval", "minimum", "action", "message"),
            show="headings",
            height=5,
        )
        for column, label, width in (
            ("name", "Name", 130),
            ("interval", "Minutes", 70),
            ("minimum", "Min Chat", 70),
            ("action", "Streamer.bot Action", 190),
            ("message", "Chat Message", 390),
        ):
            self.timer_tree.heading(column, text=label)
            self.timer_tree.column(column, width=width, anchor="w")
        self._attach_tree_scrollbar(self.timer_tree)
        self.timer_tree.bind("<<TreeviewSelect>>", self._load_selected_timer)

        timer_editor = tb.Frame(timers)
        timer_editor.pack(fill=X, pady=(8, 0))
        self.timer_enabled_var = tk.BooleanVar(value=True)
        self.timer_name_var = tk.StringVar()
        self.timer_interval_var = tk.StringVar(value="10")
        self.timer_minimum_var = tk.StringVar(value="5")
        self.timer_action_var = tk.StringVar()
        self.timer_action_id_var = tk.StringVar()
        self.timer_message_var = tk.StringVar()
        timer_editor.columnconfigure(1, weight=1)
        timer_specs = [
            ("Name", self.timer_name_var),
            ("Interval minutes", self.timer_interval_var),
            ("Minimum chat messages", self.timer_minimum_var),
        ]
        for row, (label, variable) in enumerate(timer_specs):
            tb.Label(timer_editor, text=label).grid(
                row=row, column=0, sticky="w", padx=(0, 8), pady=3
            )
            tb.Entry(timer_editor, textvariable=variable, width=34).grid(
                row=row, column=1, sticky="ew", pady=3
            )
        tb.Label(timer_editor, text="Streamer.bot action").grid(
            row=3, column=0, sticky="w", padx=(0, 8), pady=3
        )
        self.timer_action_combo = tb.Combobox(
            timer_editor,
            textvariable=self.timer_action_var,
            values=(),
            width=31,
        )
        self.timer_action_combo.grid(
            row=3, column=1, sticky="ew", pady=3
        )
        self.timer_action_combo.bind(
            "<<ComboboxSelected>>", self._timer_action_selected
        )
        tb.Label(timer_editor, text="Chat message").grid(
            row=4, column=0, sticky="w", pady=3
        )
        tb.Entry(timer_editor, textvariable=self.timer_message_var).grid(
            row=4, column=1, sticky="ew", pady=3
        )
        timer_option_row = tb.Frame(timer_editor)
        timer_option_row.grid(row=5, column=1, sticky="ew", pady=3)
        tb.Checkbutton(
            timer_option_row,
            text="Timer enabled",
            variable=self.timer_enabled_var,
            bootstyle="round-toggle",
        ).pack(side=LEFT)
        tb.Label(
            timer_option_row,
            text="Placeholders: {timer}, {chat_messages}, {currency}, {currency_singular}.",
            foreground="#aeb4ba",
        ).pack(side=LEFT, padx=(16, 0))
        timer_buttons = tb.Frame(timers)
        timer_buttons.pack(fill=X, pady=(5, 0))
        tb.Button(
            timer_buttons,
            text="Add / Update Timer",
            command=self._save_timer_rule,
            bootstyle="success",
        ).grid(row=0, column=0, sticky="w", padx=(0, 6), pady=3)
        tb.Button(
            timer_buttons,
            text="New",
            command=self._clear_timer_editor,
            bootstyle="primary",
        ).grid(row=0, column=1, sticky="w", padx=(0, 6), pady=3)
        tb.Button(
            timer_buttons,
            text="Delete Selected",
            command=self._delete_timer_rule,
            bootstyle="outline-danger",
        ).grid(row=0, column=2, sticky="w", padx=(0, 6), pady=3)
        self._refresh_timer_tree()

        balances = tb.Labelframe(container, text="Balances & Leaderboard", padding=12)
        balances.pack(fill=BOTH, expand=True, pady=5)
        balance_table_frame = tb.Frame(balances)
        balance_table_frame.pack(fill=X)
        self.balance_tree = ttk.Treeview(
            balance_table_frame,
            columns=("rank", "user", "balance"),
            show="headings",
            height=7,
        )
        for column, label, width in (
            ("rank", "#", 45),
            ("user", "User", 260),
            ("balance", "Balance", 130),
        ):
            self.balance_tree.heading(column, text=label)
            self.balance_tree.column(column, width=width, anchor="w")
        self._attach_tree_scrollbar(self.balance_tree)

        balance_controls = tb.Frame(balances)
        balance_controls.pack(fill=X, pady=(8, 0))
        self.balance_user_var = tk.StringVar()
        self.balance_amount_var = tk.StringVar(value="10")
        self.balance_reason_var = tk.StringVar(value="dashboard adjustment")
        balance_controls.columnconfigure(1, weight=1)
        balance_controls.columnconfigure(3, weight=1)
        balance_controls.columnconfigure(5, weight=2)
        tb.Label(balance_controls, text="User").grid(
            row=0, column=0, sticky="w", padx=(0, 6), pady=3
        )
        tb.Entry(
            balance_controls, textvariable=self.balance_user_var, width=18
        ).grid(row=0, column=1, sticky="ew", padx=(0, 12), pady=3)
        tb.Label(balance_controls, text="Amount").grid(
            row=0, column=2, sticky="w", padx=(0, 6), pady=3
        )
        tb.Entry(
            balance_controls, textvariable=self.balance_amount_var, width=9
        ).grid(row=0, column=3, sticky="ew", padx=(0, 12), pady=3)
        tb.Label(balance_controls, text="Reason").grid(
            row=0, column=4, sticky="w", padx=(0, 6), pady=3
        )
        tb.Entry(
            balance_controls, textvariable=self.balance_reason_var, width=28
        ).grid(row=0, column=5, sticky="ew", pady=3)
        tb.Button(
            balance_controls,
            text="Add",
            command=lambda: self._adjust_dashboard_balance(1),
            bootstyle="success",
        ).grid(row=1, column=0, sticky="w", padx=(0, 6), pady=3)
        tb.Button(
            balance_controls,
            text="Remove",
            command=lambda: self._adjust_dashboard_balance(-1),
            bootstyle="outline-danger",
        ).grid(row=1, column=1, sticky="w", padx=(0, 6), pady=3)
        tb.Button(
            balance_controls,
            text="Refresh",
            command=self._refresh_balance_tree,
            bootstyle="primary",
        ).grid(row=1, column=2, sticky="w", padx=(0, 6), pady=3)
        tb.Button(
            balance_controls,
            text="Backup",
            command=self._backup_loyalty_database,
            bootstyle="secondary",
        ).grid(row=1, column=3, sticky="w", padx=(0, 6), pady=3)
        tb.Button(
            balance_controls,
            text="Restore",
            command=self._restore_loyalty_database,
            bootstyle="secondary",
        ).grid(row=1, column=4, sticky="w", padx=(0, 6), pady=3)
        self.balance_tree.bind("<<TreeviewSelect>>", self._select_balance_user)
        self._refresh_balance_tree()

    def _refresh_custom_command_tree(self):
        for item in self.custom_command_tree.get_children():
            self.custom_command_tree.delete(item)
        for index, rule in enumerate(self.custom_command_rules):
            aliases = ", ".join(rule.get("aliases", []))
            label = f"!{rule.get('name', '')}"
            if aliases:
                label += f" ({aliases})"
            self.custom_command_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    label,
                    rule.get("cost", 0),
                    rule.get("permission", "everyone"),
                    rule.get("streamerbot_action", ""),
                    rule.get("response", ""),
                ),
            )

    def _load_selected_custom_command(self, _event=None):
        selected = self.custom_command_tree.selection()
        if not selected:
            return
        rule = self.custom_command_rules[int(selected[0])]
        self.custom_enabled_var.set(bool(rule.get("enabled", True)))
        self.custom_name_var.set(rule.get("name", ""))
        self.custom_aliases_var.set(", ".join(rule.get("aliases", [])))
        self.custom_permission_var.set(rule.get("permission", "everyone"))
        self.custom_cost_var.set(str(rule.get("cost", 0)))
        self.custom_cooldown_var.set(str(rule.get("cooldown_seconds", 0)))
        self.custom_user_cooldown_var.set(str(rule.get("user_cooldown_seconds", 0)))
        self.custom_action_var.set(rule.get("streamerbot_action", ""))
        self.custom_action_id_var.set(rule.get("streamerbot_action_id", ""))
        self.custom_response_var.set(rule.get("response", ""))

    def _clear_custom_command_editor(self):
        for item in self.custom_command_tree.selection():
            self.custom_command_tree.selection_remove(item)
        self.custom_enabled_var.set(True)
        self.custom_name_var.set("")
        self.custom_aliases_var.set("")
        self.custom_permission_var.set("everyone")
        self.custom_cost_var.set("0")
        self.custom_cooldown_var.set("0")
        self.custom_user_cooldown_var.set("0")
        self.custom_action_var.set("")
        self.custom_action_id_var.set("")
        self.custom_response_var.set("")

    def _save_custom_command_rule(self):
        name = self.custom_name_var.get().strip().lstrip("!").lower()
        if not name or " " in name:
            self.automation_status_var.set("Command names cannot be blank or contain spaces.")
            return
        aliases = [
            value.strip().lstrip("!").lower()
            for value in self.custom_aliases_var.get().split(",")
            if value.strip()
        ]
        candidate_names = {name, *aliases}
        if len(candidate_names) != 1 + len(aliases):
            self.automation_status_var.set("Command aliases must be unique.")
            return
        reserved_names = {
            self.cmd_balance_var.get().strip().lstrip("!").lower(),
            self.cmd_leaderboard_var.get().strip().lstrip("!").lower(),
            self.cmd_give_points_var.get().strip().lstrip("!").lower(),
            self.cmd_add_points_var.get().strip().lstrip("!").lower(),
            self.cmd_remove_points_var.get().strip().lstrip("!").lower(),
            self.cmd_gamble_var.get().strip().lstrip("!").lower(),
            self.cmd_duel_var.get().strip().lstrip("!").lower(),
            self.cmd_duel_accept_var.get().strip().lstrip("!").lower(),
            self.cmd_duel_decline_var.get().strip().lstrip("!").lower(),
            self.cmd_raffle_enter_var.get().strip().lstrip("!").lower(),
            self.settings.cmd_sr.lower(),
            self.settings.cmd_skip.lower(),
            self.settings.cmd_pause.lower(),
            self.settings.cmd_play.lower(),
            self.settings.cmd_hide.lower(),
            self.settings.cmd_show.lower(),
            self.settings.cmd_queue.lower(),
            self.settings.cmd_wrongsong.lower(),
            self.settings.cmd_clearqueue.lower(),
            self.settings.cmd_full.lower(),
            self.settings.cmd_info.lower(),
            self.settings.cmd_sron.lower(),
            self.settings.cmd_sroff.lower(),
        }
        if candidate_names & reserved_names:
            self.automation_status_var.set(
                "That name conflicts with a built-in loyalty or song-request command."
            )
            return
        selected = self.custom_command_tree.selection()
        selected_index = int(selected[0]) if selected else None
        for index, current in enumerate(self.custom_command_rules):
            if index == selected_index:
                continue
            current_names = {
                str(current.get("name", "")).lower(),
                *[str(alias).lower() for alias in current.get("aliases", [])],
            }
            if candidate_names & current_names:
                self.automation_status_var.set(
                    "That command name or alias is already used by another custom command."
                )
                return
        try:
            rule = {
                "enabled": bool(self.custom_enabled_var.get()),
                "name": name,
                "aliases": aliases,
                "permission": self.custom_permission_var.get(),
                "cost": max(0, int(self.custom_cost_var.get())),
                "cooldown_seconds": max(0, int(self.custom_cooldown_var.get())),
                "user_cooldown_seconds": max(
                    0, int(self.custom_user_cooldown_var.get())
                ),
                "streamerbot_action": self.custom_action_var.get().strip(),
                "streamerbot_action_id": self._selected_streamerbot_action_id(
                    self.custom_action_var.get(),
                    self.custom_action_id_var.get(),
                ),
                "response": self.custom_response_var.get().strip(),
            }
        except ValueError:
            self.automation_status_var.set("Costs and cooldowns must be whole numbers.")
            return
        if selected:
            self.custom_command_rules[int(selected[0])] = rule
        else:
            match = next(
                (
                    index
                    for index, current in enumerate(self.custom_command_rules)
                    if current.get("name", "").lower() == name
                ),
                None,
            )
            if match is None:
                self.custom_command_rules.append(rule)
            else:
                self.custom_command_rules[match] = rule
        self._refresh_custom_command_tree()
        self._clear_custom_command_editor()
        self.automation_status_var.set("Command changed. Autosaving...")
        self._schedule_autosave(50)

    def _delete_custom_command_rule(self):
        selected = self.custom_command_tree.selection()
        if not selected:
            self.automation_status_var.set("Select a custom command to delete.")
            return
        del self.custom_command_rules[int(selected[0])]
        self._refresh_custom_command_tree()
        self._clear_custom_command_editor()
        self.automation_status_var.set("Command removed. Autosaving...")
        self._schedule_autosave(50)

    def _refresh_timer_tree(self):
        for item in self.timer_tree.get_children():
            self.timer_tree.delete(item)
        for index, rule in enumerate(self.timed_message_rules):
            self.timer_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    rule.get("name", ""),
                    rule.get("interval_minutes", 1),
                    rule.get("minimum_chat_messages", 0),
                    rule.get("streamerbot_action", ""),
                    rule.get("message", ""),
                ),
            )

    def _load_selected_timer(self, _event=None):
        selected = self.timer_tree.selection()
        if not selected:
            return
        rule = self.timed_message_rules[int(selected[0])]
        self.timer_enabled_var.set(bool(rule.get("enabled", True)))
        self.timer_name_var.set(rule.get("name", ""))
        self.timer_interval_var.set(str(rule.get("interval_minutes", 10)))
        self.timer_minimum_var.set(str(rule.get("minimum_chat_messages", 5)))
        self.timer_action_var.set(rule.get("streamerbot_action", ""))
        self.timer_action_id_var.set(rule.get("streamerbot_action_id", ""))
        self.timer_message_var.set(rule.get("message", ""))

    def _clear_timer_editor(self):
        for item in self.timer_tree.selection():
            self.timer_tree.selection_remove(item)
        self.timer_enabled_var.set(True)
        self.timer_name_var.set("")
        self.timer_interval_var.set("10")
        self.timer_minimum_var.set("5")
        self.timer_action_var.set("")
        self.timer_action_id_var.set("")
        self.timer_message_var.set("")

    def _save_timer_rule(self):
        name = self.timer_name_var.get().strip()
        if not name:
            self.automation_status_var.set("Timer name cannot be blank.")
            return
        try:
            rule = {
                "enabled": bool(self.timer_enabled_var.get()),
                "name": name[:60],
                "interval_minutes": max(1, int(self.timer_interval_var.get())),
                "minimum_chat_messages": max(0, int(self.timer_minimum_var.get())),
                "streamerbot_action": self.timer_action_var.get().strip(),
                "streamerbot_action_id": self._selected_streamerbot_action_id(
                    self.timer_action_var.get(),
                    self.timer_action_id_var.get(),
                ),
                "message": self.timer_message_var.get().strip(),
            }
        except ValueError:
            self.automation_status_var.set(
                "Timer interval and minimum chat messages must be whole numbers."
            )
            return
        selected = self.timer_tree.selection()
        if selected:
            self.timed_message_rules[int(selected[0])] = rule
        else:
            match = next(
                (
                    index
                    for index, current in enumerate(self.timed_message_rules)
                    if current.get("name", "").casefold() == name.casefold()
                ),
                None,
            )
            if match is None:
                self.timed_message_rules.append(rule)
            else:
                self.timed_message_rules[match] = rule
        self._refresh_timer_tree()
        self._clear_timer_editor()
        self.automation_status_var.set("Timer changed. Autosaving...")
        self._schedule_autosave(50)

    def _delete_timer_rule(self):
        selected = self.timer_tree.selection()
        if not selected:
            self.automation_status_var.set("Select a timer to delete.")
            return
        del self.timed_message_rules[int(selected[0])]
        self._refresh_timer_tree()
        self._clear_timer_editor()
        self.automation_status_var.set("Timer removed. Autosaving...")
        self._schedule_autosave(50)

    def _export_custom_commands(self):
        path = filedialog.asksaveasfilename(
            title="Export Custom Commands",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json")],
            initialfile="stream-bot-automation-rules.json",
        )
        if not path:
            return
        payload = {
            "format": "twitch-stream-bot-automation-rules",
            "version": 1,
            "commands": self.custom_command_rules,
            "timers": self.timed_message_rules,
        }
        try:
            Path(path).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            self.automation_status_var.set(f"Could not export commands: {exc}")
            return
        self.automation_status_var.set(
            "Automation rules exported without credentials or loyalty balances."
        )

    def _import_custom_commands(self):
        path = filedialog.askopenfilename(
            title="Import Custom Commands",
            filetypes=[("JSON files", "*.json")],
        )
        if not path:
            return
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            commands = payload.get("commands", [])
            timers = payload.get("timers", [])
            if payload.get("format") not in {
                "twitch-stream-bot-automation-rules",
                "twitch-stream-bot-custom-commands",
            }:
                raise ValueError("This is not a Stream Bot automation export.")
            if not isinstance(commands, list):
                raise ValueError("The commands field must be a list.")
            if not isinstance(timers, list):
                raise ValueError("The timers field must be a list.")
            from loyalty_engine import (
                normalize_custom_command_rule,
                normalize_timed_message_rule,
            )

            imported = []
            for command in commands:
                normalized = normalize_custom_command_rule(command)
                if normalized is None:
                    raise ValueError("One or more commands have an invalid or missing name.")
                imported.append(normalized)
            imported_timers = []
            for timer in timers:
                normalized = normalize_timed_message_rule(timer)
                if normalized is None:
                    raise ValueError("One or more timers have an invalid or missing name.")
                imported_timers.append(normalized)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self.automation_status_var.set(f"Could not import commands: {exc}")
            return
        self.custom_command_rules = imported
        self.timed_message_rules = imported_timers
        self._refresh_custom_command_tree()
        self._refresh_timer_tree()
        self._clear_custom_command_editor()
        self.automation_status_var.set(
            "Automation rules imported. Autosaving..."
        )
        self._schedule_autosave(50)

    def _apply_automation_settings(self, quiet=False):
        try:
            self.settings.starting_balance = max(0, int(self.starting_balance_var.get()))
            self.settings.points_per_message = max(0, int(self.points_per_message_var.get()))
            self.settings.message_reward_cooldown_seconds = max(
                0, int(self.message_reward_cooldown_var.get())
            )
            self.settings.active_bonus_points = max(
                0, int(self.active_bonus_points_var.get())
            )
            self.settings.active_bonus_interval_minutes = max(
                1, int(self.active_bonus_interval_var.get())
            )
            self.settings.active_user_window_minutes = max(
                1, int(self.active_user_window_var.get())
            )
            self.settings.subscriber_points_multiplier = max(
                0.0, float(self.subscriber_multiplier_var.get())
            )
            self.settings.vip_points_multiplier = max(
                0.0, float(self.vip_multiplier_var.get())
            )
            self.settings.mod_points_multiplier = max(
                0.0, float(self.mod_multiplier_var.get())
            )
            self.settings.gamble_minimum = max(
                1, int(self.gamble_minimum_var.get())
            )
            self.settings.gamble_maximum = max(
                self.settings.gamble_minimum,
                int(self.gamble_maximum_var.get()),
            )
            self.settings.gamble_win_chance_percent = max(
                0.0, min(100.0, float(self.gamble_win_chance_var.get()))
            )
            self.settings.gamble_payout_multiplier = max(
                1.0, float(self.gamble_payout_var.get())
            )
            self.settings.gamble_cooldown_seconds = max(
                0, int(self.gamble_cooldown_var.get())
            )
            self.settings.duel_minimum = max(1, int(self.duel_minimum_var.get()))
            self.settings.duel_maximum = max(
                self.settings.duel_minimum,
                int(self.duel_maximum_var.get()),
            )
            self.settings.duel_timeout_seconds = max(
                10, int(self.duel_timeout_var.get())
            )
            self.settings.duel_cooldown_seconds = max(
                0, int(self.duel_cooldown_var.get())
            )
            self.settings.raffle_duration_seconds = max(
                10, int(self.raffle_duration_var.get())
            )
            self.settings.raffle_countdown_interval_seconds = max(
                0, int(self.raffle_countdown_interval_var.get())
            )
            self.settings.raffle_reward_points = max(
                0, int(self.raffle_reward_var.get())
            )
        except (tk.TclError, ValueError):
            self.automation_status_var.set(
                "Waiting for valid numeric loyalty and game settings."
            )
            return False

        self.settings.loyalty_enabled = bool(self.loyalty_enabled_var.get())
        self.settings.automation_enabled = bool(self.automation_enabled_var.get())
        self.settings.gambling_enabled = bool(self.gambling_enabled_var.get())
        self.settings.duels_enabled = bool(self.duels_enabled_var.get())
        self.settings.raffle_enabled = bool(self.raffle_enabled_var.get())
        self.settings.raffle_default_title = (
            self.raffle_title_var.get().strip() or "Raffle"
        )
        self.settings.currency_name = self.currency_name_var.get().strip() or "points"
        self.settings.currency_singular = (
            self.currency_singular_var.get().strip() or "point"
        )
        self.settings.reward_command_messages = bool(self.reward_commands_var.get())
        self.settings.loyalty_excluded_users = self.loyalty_excluded_users_var.get().strip()
        self.settings.builtin_responses = {
            response_name: variable.get()
            for response_name, variable in self.builtin_response_vars.items()
        }
        for attribute, variable in (
            ("cmd_balance", self.cmd_balance_var),
            ("cmd_leaderboard", self.cmd_leaderboard_var),
            ("cmd_give_points", self.cmd_give_points_var),
            ("cmd_add_points", self.cmd_add_points_var),
            ("cmd_remove_points", self.cmd_remove_points_var),
            ("cmd_gamble", self.cmd_gamble_var),
            ("cmd_duel", self.cmd_duel_var),
            ("cmd_duel_accept", self.cmd_duel_accept_var),
            ("cmd_duel_decline", self.cmd_duel_decline_var),
            ("cmd_raffle_enter", self.cmd_raffle_enter_var),
        ):
            value = variable.get().strip().lstrip("!").lower()
            if not value or " " in value:
                self.automation_status_var.set(
                    "Built-in command names cannot be blank or contain spaces."
                )
                return False
            setattr(self.settings, attribute, value)
        built_in_names = [
            self.settings.cmd_balance,
            self.settings.cmd_leaderboard,
            self.settings.cmd_give_points,
            self.settings.cmd_add_points,
            self.settings.cmd_remove_points,
            self.settings.cmd_gamble,
            self.settings.cmd_duel,
            self.settings.cmd_duel_accept,
            self.settings.cmd_duel_decline,
            self.settings.cmd_raffle_enter,
        ]
        if len(set(built_in_names)) != len(built_in_names):
            self.automation_status_var.set("Built-in command names must be unique.")
            return False
        song_request_names = {
            variable.get().strip().lstrip("!").lower()
            for variable in getattr(self, "_alias_vars", {}).values()
            if variable.get().strip()
        }
        conflicts = sorted(set(built_in_names) & song_request_names)
        if conflicts:
            self.automation_status_var.set(
                "Loyalty/game commands conflict with song-request aliases: "
                + ", ".join(conflicts)
            )
            return False
        self.settings.custom_commands = [dict(rule) for rule in self.custom_command_rules]
        self.settings.timed_messages = [dict(rule) for rule in self.timed_message_rules]
        self.settings.streamerbot_http_enabled = bool(
            self.streamerbot_http_enabled_var.get()
        )
        self.settings.streamerbot_http_url = (
            self.streamerbot_http_url_var.get().strip()
            or "http://127.0.0.1:7474/DoAction"
        )
        save_settings(self.settings)

        if self.bot_instance:
            for attribute in (
                "loyalty_enabled",
                "automation_enabled",
                "currency_name",
                "currency_singular",
                "starting_balance",
                "points_per_message",
                "message_reward_cooldown_seconds",
                "active_bonus_points",
                "active_bonus_interval_minutes",
                "active_user_window_minutes",
                "reward_command_messages",
                "subscriber_points_multiplier",
                "vip_points_multiplier",
                "mod_points_multiplier",
                "loyalty_excluded_users",
                "cmd_balance",
                "cmd_leaderboard",
                "cmd_give_points",
                "cmd_add_points",
                "cmd_remove_points",
                "gambling_enabled",
                "cmd_gamble",
                "gamble_minimum",
                "gamble_maximum",
                "gamble_win_chance_percent",
                "gamble_payout_multiplier",
                "gamble_cooldown_seconds",
                "duels_enabled",
                "cmd_duel",
                "cmd_duel_accept",
                "cmd_duel_decline",
                "duel_minimum",
                "duel_maximum",
                "duel_timeout_seconds",
                "duel_cooldown_seconds",
                "raffle_enabled",
                "cmd_raffle_enter",
                "raffle_default_title",
                "raffle_duration_seconds",
                "raffle_countdown_interval_seconds",
                "raffle_reward_points",
                "builtin_responses",
                "custom_commands",
                "timed_messages",
                "streamerbot_http_enabled",
                "streamerbot_http_url",
            ):
                setattr(
                    self.bot_instance.settings,
                    attribute,
                    getattr(self.settings, attribute),
                )
        self.automation_status_var.set(
            "Autosaved." if quiet else "Loyalty and automation settings saved."
        )
        if not quiet:
            self._refresh_balance_tree()
        return True

    def _apply_streamerbot_actions(self, actions):
        self.streamerbot_actions = sorted(
            actions,
            key=lambda action: action.get("name", "").casefold(),
        )
        self.streamerbot_action_ids = {}
        self.streamerbot_action_names_by_id = {}
        for action in self.streamerbot_actions:
            name = str(action.get("name", "")).strip()
            action_id = str(action.get("id", "")).strip()
            if name and name not in self.streamerbot_action_ids:
                self.streamerbot_action_ids[name] = action_id
            if action_id:
                self.streamerbot_action_names_by_id[action_id] = name
        rules_changed = False
        for rule in [*self.custom_command_rules, *self.timed_message_rules]:
            action_id = str(rule.get("streamerbot_action_id", "")).strip()
            current_name = str(rule.get("streamerbot_action", "")).strip()
            discovered_name = self.streamerbot_action_names_by_id.get(action_id, "")
            if discovered_name and discovered_name != current_name:
                rule["streamerbot_action"] = discovered_name
                rules_changed = True
        action_names = tuple(self.streamerbot_action_ids)
        self.custom_action_combo.configure(values=action_names)
        self.timer_action_combo.configure(values=action_names)
        self.custom_action_id_var.set(
            self._selected_streamerbot_action_id(
                self.custom_action_var.get(),
                self.custom_action_id_var.get(),
            )
        )
        self.timer_action_id_var.set(
            self._selected_streamerbot_action_id(
                self.timer_action_var.get(),
                self.timer_action_id_var.get(),
            )
        )
        if rules_changed:
            self._refresh_custom_command_tree()
            self._refresh_timer_tree()
            self.automation_status_var.set(
                "Streamer.bot action names were updated from their saved action IDs. "
                "Autosaving the renamed actions."
            )
            self._schedule_autosave(50)

    def _selected_streamerbot_action_id(self, action_name, current_id=""):
        action_name = str(action_name).strip()
        current_id = str(current_id).strip()
        if not action_name:
            return ""
        discovered_id = self.streamerbot_action_ids.get(action_name, "")
        if discovered_id:
            return discovered_id
        if any(
            str(action.get("id", "")).strip() == current_id
            and str(action.get("name", "")).strip() == action_name
            for action in self.streamerbot_actions
        ):
            return current_id
        return current_id if not self.streamerbot_actions else ""

    def _custom_action_selected(self, _event=None):
        self.custom_action_id_var.set(
            self.streamerbot_action_ids.get(self.custom_action_var.get().strip(), "")
        )

    def _timer_action_selected(self, _event=None):
        self.timer_action_id_var.set(
            self.streamerbot_action_ids.get(self.timer_action_var.get().strip(), "")
        )

    def _test_streamerbot_connection(self):
        url = (
            self.streamerbot_http_url_var.get().strip()
            or "http://127.0.0.1:7474/DoAction"
        )
        self.automation_status_var.set("Testing Streamer.bot connection...")

        def worker():
            from loyalty_engine import LoyaltyEngine

            test_settings = self.settings.model_copy(deep=True)
            test_settings.streamerbot_http_url = url
            engine = LoyaltyEngine(test_settings)
            try:
                actions = asyncio.run(engine.get_streamerbot_actions())
            except Exception as exc:
                error_message = f"Streamer.bot connection failed: {exc}"
                self.root.after(
                    0,
                    lambda message=error_message: self.automation_status_var.set(message),
                )
                return

            self.root.after(
                0,
                lambda discovered=actions: self._apply_streamerbot_actions(discovered),
            )
            configured_rules = [
                rule
                for rule in [*self.custom_command_rules, *self.timed_message_rules]
                if str(rule.get("streamerbot_action", "")).strip()
                or str(rule.get("streamerbot_action_id", "")).strip()
            ]
            available_names = {action["name"] for action in actions}
            available_ids = {action["id"] for action in actions if action.get("id")}
            missing = sorted(
                {
                    str(rule.get("streamerbot_action", "")).strip()
                    or str(rule.get("streamerbot_action_id", "")).strip()
                    for rule in configured_rules
                    if str(rule.get("streamerbot_action_id", "")).strip()
                    not in available_ids
                    and str(rule.get("streamerbot_action", "")).strip()
                    not in available_names
                },
                key=str.casefold,
            )
            if missing:
                message = (
                    f"Connected. {len(actions)} action(s) found; missing configured action(s): "
                    + ", ".join(missing)
                )
            else:
                message = (
                    f"Connected to Streamer.bot. {len(actions)} action(s) available; "
                    "all configured action names were found."
                )
            self.root.after(0, lambda: self.automation_status_var.set(message))

        threading.Thread(target=worker, daemon=True).start()

    def _test_selected_streamerbot_action(self):
        if not self.streamerbot_http_enabled_var.get():
            self.automation_status_var.set(
                "Enable Streamer.bot action execution before testing an action."
            )
            return
        if self.timer_tree.selection() and self.timer_action_var.get().strip():
            action_name = self.timer_action_var.get().strip()
            action_id = self._selected_streamerbot_action_id(
                action_name, self.timer_action_id_var.get()
            )
        else:
            action_name = self.custom_action_var.get().strip()
            action_id = self._selected_streamerbot_action_id(
                action_name, self.custom_action_id_var.get()
            )
        if not action_name and not action_id:
            self.automation_status_var.set(
                "Choose a Streamer.bot action in a command or timer first."
            )
            return

        url = (
            self.streamerbot_http_url_var.get().strip()
            or "http://127.0.0.1:7474/DoAction"
        )
        self.automation_status_var.set(f"Testing Streamer.bot action: {action_name}")

        def worker():
            from loyalty_engine import LoyaltyEngine

            test_settings = self.settings.model_copy(deep=True)
            test_settings.streamerbot_http_enabled = True
            test_settings.streamerbot_http_url = url
            engine = LoyaltyEngine(test_settings)
            try:
                succeeded = asyncio.run(
                    engine.test_streamerbot_action(action_name, action_id)
                )
            except Exception as exc:
                message = f"Streamer.bot action test failed: {exc}"
            else:
                message = (
                    f"Streamer.bot action ran successfully: {action_name}"
                    if succeeded
                    else f"Streamer.bot rejected or could not run: {action_name}"
                )
            self.root.after(
                0,
                lambda result=message: self.automation_status_var.set(result),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _loyalty_engine_for_dashboard(self):
        if self.bot_instance:
            return self.bot_instance.loyalty
        from loyalty_engine import LoyaltyEngine

        return LoyaltyEngine(self.settings)

    def _refresh_balance_tree(self):
        if not hasattr(self, "balance_tree"):
            return
        for item in self.balance_tree.get_children():
            self.balance_tree.delete(item)
        try:
            leaders = self._loyalty_engine_for_dashboard().leaderboard(25)
        except Exception as exc:
            self.automation_status_var.set(f"Could not open loyalty database: {exc}")
            return
        for rank, row in enumerate(leaders, 1):
            self.balance_tree.insert(
                "",
                "end",
                values=(rank, row["display_name"], row["balance"]),
            )

    def _select_balance_user(self, _event=None):
        selected = self.balance_tree.selection()
        if not selected:
            return
        values = self.balance_tree.item(selected[0], "values")
        if len(values) >= 2:
            self.balance_user_var.set(values[1])

    def _adjust_dashboard_balance(self, direction):
        username = self.balance_user_var.get().strip().lstrip("@")
        if not username:
            self.automation_status_var.set("Enter or select a user first.")
            return
        try:
            amount = abs(int(self.balance_amount_var.get())) * direction
        except ValueError:
            self.automation_status_var.set("Balance amount must be a whole number.")
            return
        reason = self.balance_reason_var.get().strip() or "dashboard adjustment"
        try:
            balance = self._loyalty_engine_for_dashboard().adjust_balance(
                username,
                amount,
                reason,
                username,
            )
        except Exception as exc:
            self.automation_status_var.set(f"Could not update balance: {exc}")
            return
        self.automation_status_var.set(
            f"{username} now has {balance} {self.settings.currency_name}."
        )
        self._refresh_balance_tree()

    def _raffle_chat_channel(self):
        if not self.bot_instance:
            return None
        channel = getattr(self.bot_instance.loyalty, "latest_channel", None)
        if channel is not None:
            return channel
        get_channel = getattr(self.bot_instance, "get_channel", None)
        if callable(get_channel):
            try:
                return get_channel(self.settings.channel)
            except Exception:
                return None
        return None

    def _run_raffle_action(self, action_name, coroutine_factory):
        if not self.bot_instance or not getattr(self, "_bot_loop", None):
            self.raffle_status_var.set("Start the bot before using raffle controls.")
            return
        if not self._apply_automation_settings(quiet=True):
            self.raffle_status_var.set("Fix invalid raffle settings before continuing.")
            return
        channel = self._raffle_chat_channel()
        if channel is None:
            self.raffle_status_var.set(
                "The bot needs an active Twitch chat connection before it can announce raffles."
            )
            return

        future = asyncio.run_coroutine_threadsafe(
            coroutine_factory(channel),
            self._bot_loop,
        )

        def _done(_future):
            try:
                result = _future.result()
            except Exception as exc:
                message = f"Raffle {action_name} failed: {exc}"
            else:
                message = result
            self.root.after(0, lambda: self.raffle_status_var.set(message))

        future.add_done_callback(_done)

    def _start_dashboard_raffle(self):
        title = self.raffle_title_var.get().strip() or "Raffle"
        entry_command = self.cmd_raffle_enter_var.get().strip().lstrip("!")

        async def do_start(channel):
            started = await self.bot_instance.loyalty.start_raffle(
                title=title,
                entry_command=entry_command,
                duration_seconds=int(self.raffle_duration_var.get()),
                countdown_interval_seconds=int(self.raffle_countdown_interval_var.get()),
                reward_points=int(self.raffle_reward_var.get()),
                channel=channel,
            )
            if not started:
                return "A raffle is already active, or raffles are disabled."
            return f"Raffle active: {title}. Viewers enter with !{entry_command or self.settings.cmd_raffle_enter}."

        self._run_raffle_action("start", do_start)

    def _draw_dashboard_raffle(self):
        async def do_draw(channel):
            winner = await self.bot_instance.loyalty.draw_raffle(channel)
            if winner is None:
                return "Raffle ended with no winner."
            return f"Raffle winner: {winner.get('display_name') or winner['username']}."

        self._run_raffle_action("draw", do_draw)

    def _cancel_dashboard_raffle(self):
        async def do_cancel(channel):
            cancelled = await self.bot_instance.loyalty.cancel_raffle(channel)
            return "Raffle cancelled." if cancelled else "There is no active raffle to cancel."

        self._run_raffle_action("cancel", do_cancel)

    def _backup_loyalty_database(self):
        path = filedialog.asksaveasfilename(
            title="Backup Loyalty Database",
            defaultextension=".sqlite3",
            filetypes=[("SQLite database", "*.sqlite3"), ("All files", "*.*")],
            initialfile="loyalty-backup.sqlite3",
        )
        if not path:
            return
        try:
            self._loyalty_engine_for_dashboard().backup_database(Path(path))
        except Exception as exc:
            self.automation_status_var.set(f"Could not back up loyalty data: {exc}")
            return
        self.automation_status_var.set(
            "Loyalty database backed up. It contains usernames and balances, but no OAuth token."
        )

    def _restore_loyalty_database(self):
        path = filedialog.askopenfilename(
            title="Restore Loyalty Database",
            filetypes=[("SQLite database", "*.sqlite3"), ("All files", "*.*")],
        )
        if not path:
            return
        if not messagebox.askyesno(
            "Restore Loyalty Database",
            "Replace all current balances and loyalty history with this backup?",
        ):
            return
        try:
            self._loyalty_engine_for_dashboard().restore_database(Path(path))
        except Exception as exc:
            self.automation_status_var.set(f"Could not restore loyalty data: {exc}")
            return
        self._refresh_balance_tree()
        self.automation_status_var.set("Loyalty database restored successfully.")

    def _build_setup(self):
        scrolled = VerticalScrolledFrame(self.setup_frame)
        scrolled.pack(fill=BOTH, expand=True)
        container = tb.Frame(scrolled.interior, padding=20)
        container.pack(fill=BOTH, expand=True)

        obs_frame = tb.Labelframe(container, text="OBS Studio Browser Source Setup", padding=15)
        obs_frame.pack(fill=X, pady=10)
        
        tb.Label(obs_frame, text="1. Add a new 'Browser Source' to your OBS scenes.").pack(anchor="w", pady=2)
        tb.Label(obs_frame, text="2. For stream audio control, check 'Control Audio via OBS' so the source appears in the OBS mixer.").pack(anchor="w", pady=2)
        tb.Label(obs_frame, text="3. Set the Width to 1920 and Height to 1080 (this is required for proper fullscreen scaling).").pack(anchor="w", pady=2)
        tb.Label(obs_frame, text="4. Delete any Custom CSS in the OBS properties window (leave it blank).").pack(anchor="w", pady=2)
        tb.Label(obs_frame, text="5. Leave the Browser Source visible in OBS. The page hides itself automatically when no song is playing.").pack(anchor="w", pady=2)
        tb.Label(obs_frame, text="6. Turn OFF 'Shutdown source when not visible' so the bot can stay connected.").pack(anchor="w", pady=2)
        tb.Label(obs_frame, text="7. Refresh the OBS Browser Source once after changing this URL or the app port.").pack(anchor="w", pady=2)
        tb.Label(obs_frame, text="8. If you want to hear SR locally, set the source's Audio Monitoring to 'Monitor and Output'.").pack(anchor="w", pady=2)
        tb.Label(obs_frame, text="9. Use this Player Integration URL as the source URL:").pack(anchor="w", pady=(8,2))
        tb.Label(obs_frame, text="10. Use the Dashboard's OBS Display controls to toggle the title, time, and progress bar.").pack(anchor="w", pady=2)
        
        self.url_entry = tb.Entry(obs_frame, width=50)
        self.url_entry.insert(0, f"http://127.0.0.1:{self.port_var.get()}/player")
        self.url_entry.configure(state='readonly')
        self.url_entry.pack(anchor="w", padx=20, pady=(0, 10))

        # Update the URL automatically if they change the port input
        def _update_url(*args):
            try:
                new_port = self.port_var.get()
                self.url_entry.configure(state='normal')
                self.url_entry.delete(0, tk.END)
                self.url_entry.insert(0, f"http://127.0.0.1:{new_port}/player")
                self.url_entry.configure(state='readonly')
            except Exception:
                pass
        self.port_var.trace_add("write", _update_url)

        cmd_frame = tb.Labelframe(container, text="How to Get an OAuth Token", padding=15)
        cmd_frame.pack(fill=X, pady=10)
        tb.Label(cmd_frame, text="1. Go to https://twitchtokengenerator.com").pack(anchor="w", pady=2)
        tb.Label(cmd_frame, text="2. Scroll down to 'Custom Scope Token' / 'Custom Bot Token' and open that generator.").pack(anchor="w", pady=2)
        tb.Label(cmd_frame, text="3. Log in with the Twitch account the bot should speak as, usually your bot account.").pack(anchor="w", pady=2)
        tb.Label(cmd_frame, text="4. Required scopes: check chat:read so the bot can see !sr commands.").pack(anchor="w", pady=2)
        tb.Label(cmd_frame, text="5. Required scopes: check chat:edit so the bot can reply in chat.").pack(anchor="w", pady=2)
        tb.Label(cmd_frame, text="6. Click 'Generate Token' / 'Generate Access Token'.").pack(anchor="w", pady=2)
        tb.Label(cmd_frame, text="7. Copy only the string labeled 'ACCESS TOKEN'. Do not paste Client ID or Client Secret.").pack(anchor="w", pady=2)
        tb.Label(cmd_frame, text="8. Paste that Access Token into Settings -> OAuth Token, then click Save Settings.").pack(anchor="w", pady=2)
        tb.Label(cmd_frame, text="9. Keep this token private. Anyone with it can chat as that Twitch account until the token is revoked.").pack(anchor="w", pady=2)

        cmd_frame = tb.Labelframe(container, text="Twitch Chat Commands", padding=15)
        cmd_frame.pack(fill=X, pady=10)
        tb.Label(cmd_frame, text="!sr <url> or !sr <search term> - Request a song").pack(anchor="w", pady=2)
        tb.Label(cmd_frame, text="!skip (Mod/VIP/Broadcaster) - Skip current song and play the next queued item").pack(anchor="w", pady=2)
        tb.Label(cmd_frame, text="!stop (Mod/VIP/Broadcaster) - Stop the media player without clearing the queue").pack(anchor="w", pady=2)
        tb.Label(cmd_frame, text="!pause (Mod/VIP/Broadcaster) - Pause or resume the current song").pack(anchor="w", pady=2)
        tb.Label(cmd_frame, text="!play or !resume (Mod/VIP/Broadcaster) - Resume paused media or restart the stopped queue").pack(anchor="w", pady=2)
        tb.Label(cmd_frame, text="!hide (Mod/VIP/Broadcaster) - Toggle the SR window hidden/visible while the song keeps playing").pack(anchor="w", pady=2)
        tb.Label(cmd_frame, text="!show (Mod/VIP/Broadcaster) - Show the SR window again without interrupting playback").pack(anchor="w", pady=2)
        tb.Label(cmd_frame, text="!queue - View upcoming songs").pack(anchor="w", pady=2)
        tb.Label(cmd_frame, text="!wrongsong - Remove your last requested song").pack(anchor="w", pady=2)
        tb.Label(cmd_frame, text="!clearqueue (Mod/Broadcaster) - Empty the queue").pack(anchor="w", pady=2)
        tb.Label(cmd_frame, text="!full (Mod/Broadcaster) - Toggle SR between small PiP window and full screen").pack(anchor="w", pady=2)
        tb.Label(cmd_frame, text="!info (Mod/VIP/Broadcaster) - Toggle the persistent song title in the OBS source").pack(anchor="w", pady=2)
        tb.Label(cmd_frame, text="!raffle - Enter the active streamer-started raffle when raffles are enabled").pack(anchor="w", pady=2)

        local_frame = tb.Labelframe(container, text="Local Library", padding=15)
        local_frame.pack(fill=X, pady=10)
        tb.Label(local_frame, text="1. On the Dashboard, enable Local Library and choose one media folder.").pack(anchor="w", pady=2)
        tb.Label(local_frame, text="2. Click Save & Refresh in the Local Library panel to scan that folder.").pack(anchor="w", pady=2)
        tb.Label(local_frame, text="3. Search or select local audio/video files and click 'Add Selected To Queue'.").pack(anchor="w", pady=2)
        tb.Label(local_frame, text="4. Local files play through the same OBS browser source and localhost player as normal SR songs.").pack(anchor="w", pady=2)

        profile_frame = tb.Labelframe(container, text="Stream Profiles", padding=15)
        profile_frame.pack(fill=X, pady=10)
        tb.Label(profile_frame, text="1. Configure the SR window and OBS Display options the way you want them.").pack(anchor="w", pady=2)
        tb.Label(profile_frame, text="2. Type a profile name on the Dashboard and click 'Save Current'.").pack(anchor="w", pady=2)
        tb.Label(
            profile_frame,
            text=(
                "3. Profiles include every non-secret modifiable setting: SR layout, "
                "local library, loyalty/game rules, commands, timers, aliases, and "
                "Streamer.bot integration. OAuth and runtime database/state paths are excluded."
            ),
            wraplength=970,
        ).pack(anchor="w", pady=2)
        tb.Label(profile_frame, text="4. Select a saved profile and click Apply to switch layouts immediately.").pack(anchor="w", pady=2)
        tb.Label(profile_frame, text="5. Streamer.bot: add a Core -> Network -> Fetch URL sub-action using the URL below.").pack(anchor="w", pady=2)
        tb.Label(profile_frame, text="6. Start this bot, then replace PROFILE_NAME with the saved profile name. Spaces must be written as %20.").pack(anchor="w", pady=2)

        self.profile_api_entry = tb.Entry(profile_frame, width=90)
        self.profile_api_entry.insert(
            0,
            f"http://127.0.0.1:{self.port_var.get()}/api/profiles/apply?name=PROFILE_NAME"
        )
        self.profile_api_entry.configure(state="readonly")
        self.profile_api_entry.pack(anchor="w", padx=20, pady=(4, 2))

        def _update_profile_api_url(*args):
            try:
                new_port = self.port_var.get()
                self.profile_api_entry.configure(state="normal")
                self.profile_api_entry.delete(0, tk.END)
                self.profile_api_entry.insert(
                    0,
                    f"http://127.0.0.1:{new_port}/api/profiles/apply?name=PROFILE_NAME"
                )
                self.profile_api_entry.configure(state="readonly")
            except Exception:
                pass

        self.port_var.trace_add("write", _update_profile_api_url)

        loyalty_frame = tb.Labelframe(
            container, text="Loyalty Points & Custom Commands (2.0)", padding=15
        )
        loyalty_frame.pack(fill=X, pady=10)
        tb.Label(
            loyalty_frame,
            text="1. Open Loyalty & Automation and choose your own currency names and earning rules.",
        ).pack(anchor="w", pady=2)
        tb.Label(
            loyalty_frame,
            text="2. Enable loyalty, configure message/active-chat rewards, and save.",
        ).pack(anchor="w", pady=2)
        tb.Label(
            loyalty_frame,
            text="3. Built-in balance, leaderboard, add, and remove command words are editable.",
        ).pack(anchor="w", pady=2)
        tb.Label(
            loyalty_frame,
            text="4. Custom commands can require a role, charge points, use cooldowns, reply in chat, and trigger a Streamer.bot action.",
        ).pack(anchor="w", pady=2)
        tb.Label(
            loyalty_frame,
            text=(
                "5. Streamer.bot integration: enable its HTTP Server at 127.0.0.1:7474, "
                "enable action execution here, click Refresh Actions, then choose an action "
                "from the command or timer dropdown."
            ),
        ).pack(anchor="w", pady=2)
        tb.Label(
            loyalty_frame,
            text=(
                "6. Use Test Selected Action before testing in Twitch chat. No Streamer.bot "
                "trigger is required because this app calls the selected action directly."
            ),
        ).pack(anchor="w", pady=2)
        tb.Label(
            loyalty_frame,
            text=(
                "7. Games: !gamble <amount|all>, !duel <user> <amount|all>, "
                "!accept, !decline, and dashboard-started raffles. Names, limits, "
                "odds, payouts, timeouts, rewards, and cooldowns are configurable."
            ),
        ).pack(anchor="w", pady=2)
        tb.Label(
            loyalty_frame,
            text="8. Settings autosave shortly after each valid change; Save Now remains available as a manual fallback.",
        ).pack(anchor="w", pady=2)
        tb.Label(
            loyalty_frame,
            text="9. Balances are stored locally in %LOCALAPPDATA%\\Twitch Song Request Bot\\data\\loyalty.sqlite3.",
        ).pack(anchor="w", pady=2)
        tb.Label(
            loyalty_frame,
            text="10. Silent lurkers cannot be rewarded automatically because Twitch does not provide a reliable complete viewer list; active-chat bonuses use recent messages.",
            foreground="#ffcc00",
            wraplength=970,
        ).pack(anchor="w", pady=(4, 0))



    def _sync_ui(self):
        if self.bot_instance:
            current_bot_state = getattr(self.bot_instance, 'is_sr_enabled', True)
            if self.sr_enabled_var.get() != current_bot_state:
                self.sr_enabled_var.set(current_bot_state)

            np = getattr(self.bot_instance, 'main_vlc_now_playing_details', None)
            is_sr = getattr(self.bot_instance, 'main_vlc_content_type', None) == getattr(self.bot_instance, 'VLC_SR_PRIORITY', 'VLC_SR')
            
            if np and is_sr:
                self.np_title_var.set(np.get('title', 'Unknown'))
                self.np_req_var.set(f"Requested by: @{np.get('requested_by', 'Unknown')}")
                
                if self.bot_instance.main_vlc_is_paused:
                    self.play_btn.config(text="Play")
                else:
                    self.play_btn.config(text="Pause")
                    
                self.play_btn.config(state=tk.NORMAL)
                self.hide_toggle_btn.config(state=tk.NORMAL)
                self._sync_obs_display_buttons()
                self.seek_slider.config(state=tk.NORMAL)

                dur = self.bot_instance.main_vlc_duration
                curr = self.bot_instance.main_vlc_current_time
                if dur > 0:
                    self.seek_slider.config(to=dur)
                    if getattr(self, 'is_dragging_slider', False) == False:
                        self.seek_var.set(curr)
                        
                    def fmt(s):
                        m, s = divmod(int(s), 60)
                        return f"{m}:{s:02d}"
                        
                    self.time_lbl.config(text=f"{fmt(curr)} / {fmt(dur)}")
            else:
                self.np_title_var.set("Not Playing")
                self.np_req_var.set("")
                self.play_btn.config(state=tk.DISABLED, text="Pause")
                self.hide_toggle_btn.config(state=tk.DISABLED)
                self._sync_obs_display_buttons()
                self.seek_slider.config(state=tk.DISABLED)
                self.seek_var.set(0)
                self.time_lbl.config(text="0:00 / 0:00")

            sr_cog = self.bot_instance.get_cog("SRCog")
            if sr_cog:
                # Refresh queue view if needed
                current_q = list(sr_cog.song_request_deque)
                # Compute quick signature
                q_id = "|".join([str(s.get('timestamp', i)) for i, s in enumerate(current_q)])
                # Compute quick signature for queue and history
                q_id = id(sr_cog.song_request_deque) + sum(id(x) for x in sr_cog.song_request_deque) + len(sr_cog.song_request_deque) + len(getattr(sr_cog, 'song_history_deque', []))
                if q_id != getattr(self, '_last_q_id', None):
                    selected_iid = self.tree.selection()
                    sel_index = None
                    if selected_iid:
                        try:
                            idx_val = self.tree.item(selected_iid[0], "values")[0]
                            sel_index = int(idx_val) - 1
                        except IndexError: # Handle cases where selected item might be invalid
                            sel_index = None

                    self.tree.delete(*self.tree.get_children())
                    for idx, song in enumerate(sr_cog.song_request_deque):
                        title = song.get('title', 'Unknown Title')
                        user = song.get('requested_by', 'Unknown')
                        self.tree.insert("", "end", values=(idx + 1, title, user))
                        
                    # Also sync history
                    self.hist_tree.delete(*self.hist_tree.get_children())
                    if hasattr(sr_cog, 'song_history_deque'):
                        for song in reversed(sr_cog.song_history_deque):
                            title = song.get('title', 'Unknown Title')
                            user = song.get('requested_by', 'Unknown')
                            self.hist_tree.insert("", "end", values=(title, user))
                            
                    self._last_q_id = q_id

                    # Re-select item if it was moved or still exists
                    if getattr(self, '_pending_reselect', None) is not None:
                        if self._pending_reselect < len(sr_cog.song_request_deque):
                            children = self.tree.get_children()
                            if self._pending_reselect < len(children):
                                self.tree.selection_set(children[self._pending_reselect])
                        self._pending_reselect = None
                    elif sel_index is not None and sel_index < len(sr_cog.song_request_deque):
                        children = self.tree.get_children()
                        if sel_index < len(children):
                            self.tree.selection_set(children[sel_index])
            if self.local_library_tree.selection():
                self.add_local_btn.config(state=tk.NORMAL)
            else:
                self.add_local_btn.config(state=tk.DISABLED)
            if hasattr(self, "raffle_status_var"):
                raffle = getattr(self.bot_instance.loyalty, "active_raffle", None)
                if raffle:
                    import time as _time

                    remaining = max(0, round(float(raffle.get("ends_at", 0)) - _time.time()))
                    entries = len(raffle.get("entries", {}))
                    self.raffle_status_var.set(
                        f"Active raffle: {raffle.get('title', 'Raffle')} | "
                        f"!{raffle.get('entry_command', self.settings.cmd_raffle_enter)} | "
                        f"{entries} entr{'y' if entries == 1 else 'ies'} | "
                        f"{remaining}s left"
                    )
        else:
            self.add_local_btn.config(state=tk.DISABLED)

        self.root.after(500, self._sync_ui)

    def _sync_obs_display_buttons(self):
        controls = (
            (self.overlay_btn, "Title", self.bot_instance.is_title_visible),
            (self.time_toggle_btn, "Time", self.bot_instance.is_time_visible),
            (self.progress_toggle_btn, "Bar", self.bot_instance.is_progress_visible),
        )
        for button, label, enabled in controls:
            button.configure(
                state=tk.NORMAL,
                text=f"{label}: {'On' if enabled else 'Off'}",
                bootstyle="info" if enabled else "outline-info"
            )

    def _watch_setting_var(self, var):
        var.trace_add("write", lambda *_: self._schedule_autosave())

    def _mark_settings_dirty(self):
        if hasattr(self, "save_settings_btn"):
            self.save_settings_btn.configure(bootstyle=WARNING)
        if hasattr(self, "save_settings_status_var"):
            self.save_settings_status_var.set("Autosave pending...")
        self._schedule_autosave()

    def _mark_settings_saved(self):
        if hasattr(self, "save_settings_btn"):
            self.save_settings_btn.configure(bootstyle=SUCCESS)
        if hasattr(self, "save_settings_status_var"):
            self.save_settings_status_var.set("Saved.")

    def _register_automation_autosave(self):
        variables = [
            self.loyalty_enabled_var,
            self.automation_enabled_var,
            self.reward_commands_var,
            self.currency_name_var,
            self.currency_singular_var,
            self.starting_balance_var,
            self.points_per_message_var,
            self.message_reward_cooldown_var,
            self.active_bonus_points_var,
            self.active_bonus_interval_var,
            self.active_user_window_var,
            self.subscriber_multiplier_var,
            self.vip_multiplier_var,
            self.mod_multiplier_var,
            self.loyalty_excluded_users_var,
            self.cmd_balance_var,
            self.cmd_leaderboard_var,
            self.cmd_give_points_var,
            self.cmd_add_points_var,
            self.cmd_remove_points_var,
            self.cmd_gamble_var,
            self.cmd_duel_var,
            self.cmd_duel_accept_var,
            self.cmd_duel_decline_var,
            self.gambling_enabled_var,
            self.duels_enabled_var,
            self.gamble_minimum_var,
            self.gamble_maximum_var,
            self.gamble_win_chance_var,
            self.gamble_payout_var,
            self.gamble_cooldown_var,
            self.duel_minimum_var,
            self.duel_maximum_var,
            self.duel_timeout_var,
            self.duel_cooldown_var,
            self.raffle_enabled_var,
            self.raffle_title_var,
            self.cmd_raffle_enter_var,
            self.raffle_duration_var,
            self.raffle_countdown_interval_var,
            self.raffle_reward_var,
            self.streamerbot_http_enabled_var,
            self.streamerbot_http_url_var,
            *self.builtin_response_vars.values(),
        ]
        for variable in variables:
            variable.trace_add("write", lambda *_: self._schedule_autosave())

    def _schedule_autosave(self, delay_ms=700):
        if self._autosave_suspended:
            return
        if self._autosave_after_id is not None:
            try:
                self.root.after_cancel(self._autosave_after_id)
            except tk.TclError:
                pass
        if hasattr(self, "automation_status_var"):
            self.automation_status_var.set("Autosave pending...")
        if hasattr(self, "save_settings_status_var"):
            self.save_settings_status_var.set("Autosave pending...")
        self._autosave_after_id = self.root.after(delay_ms, self._run_autosave)

    def _run_autosave(self):
        self._autosave_after_id = None
        self._autosave_suspended = True
        try:
            settings_valid = self._apply_settings(
                quiet=True, refresh_library=False
            )
            automation_valid = self._apply_automation_settings(quiet=True)
        finally:
            self._autosave_suspended = False
        if settings_valid and automation_valid:
            self._mark_settings_saved()

    def flush_pending_autosave(self):
        if self._autosave_after_id is None:
            return
        try:
            self.root.after_cancel(self._autosave_after_id)
        except tk.TclError:
            pass
        self._autosave_after_id = None
        self._run_autosave()

    def _profile_runtime_state(self):
        if self.bot_instance:
            return {
                "accept_requests": bool(self.bot_instance.is_sr_enabled),
                "show_title": bool(self.bot_instance.is_title_visible),
                "show_time": bool(self.bot_instance.is_time_visible),
                "show_progress": bool(self.bot_instance.is_progress_visible),
            }

        state = {}
        try:
            state_path = self.settings.bot_state_path
            if state_path.exists():
                state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}

        return {
            "accept_requests": bool(self.sr_enabled_var.get()),
            "show_title": bool(state.get("is_title_visible", False)),
            "show_time": bool(state.get("is_time_visible", False)),
            "show_progress": bool(state.get("is_progress_visible", False)),
        }

    def _capture_current_profile(self):
        if not self._apply_settings(quiet=True, refresh_library=False):
            raise ValueError("Invalid general settings.")
        if not self._apply_automation_settings(quiet=True):
            raise ValueError("Invalid loyalty or automation settings.")
        runtime_state = self._profile_runtime_state()
        return {
            "format_version": 2,
            "settings": profile_settings_payload(self.settings),
            **runtime_state,
            "window_position": self.pos_var.get(),
            "window_width": max(160, min(1920, int(self.w_var.get()))),
            "window_height": max(90, min(1080, int(self.h_var.get()))),
            "background_opacity": max(0, min(100, int(self.bg_op_var.get()))),
            "title_font_size": max(8, min(48, int(self.title_font_size_var.get()))),
            "time_font_size": max(8, min(48, int(self.time_font_size_var.get()))),
        }

    def _load_settings_into_ui(self):
        general_variables = {
            "channel_var": self.settings.channel,
            "oauth_var": self.settings.oauth_token,
            "vol_var": self.settings.vlc_sr_volume,
            "pos_var": self.settings.sr_window_position,
            "bg_op_var": self.settings.sr_bg_opacity,
            "w_var": self.settings.sr_window_width,
            "h_var": self.settings.sr_window_height,
            "title_font_size_var": self.settings.sr_title_font_size,
            "time_font_size_var": self.settings.sr_time_font_size,
            "port_var": self.settings.web_server_port,
            "fair_queue_var": self.settings.use_fair_queue,
            "local_library_enabled_var": self.settings.local_library_enabled,
            "local_library_root_var": self.settings.local_library_root,
        }
        automation_variables = {
            "loyalty_enabled_var": self.settings.loyalty_enabled,
            "automation_enabled_var": self.settings.automation_enabled,
            "reward_commands_var": self.settings.reward_command_messages,
            "currency_name_var": self.settings.currency_name,
            "currency_singular_var": self.settings.currency_singular,
            "starting_balance_var": self.settings.starting_balance,
            "points_per_message_var": self.settings.points_per_message,
            "message_reward_cooldown_var": self.settings.message_reward_cooldown_seconds,
            "active_bonus_points_var": self.settings.active_bonus_points,
            "active_bonus_interval_var": self.settings.active_bonus_interval_minutes,
            "active_user_window_var": self.settings.active_user_window_minutes,
            "subscriber_multiplier_var": self.settings.subscriber_points_multiplier,
            "vip_multiplier_var": self.settings.vip_points_multiplier,
            "mod_multiplier_var": self.settings.mod_points_multiplier,
            "loyalty_excluded_users_var": self.settings.loyalty_excluded_users,
            "cmd_balance_var": self.settings.cmd_balance,
            "cmd_leaderboard_var": self.settings.cmd_leaderboard,
            "cmd_give_points_var": self.settings.cmd_give_points,
            "cmd_add_points_var": self.settings.cmd_add_points,
            "cmd_remove_points_var": self.settings.cmd_remove_points,
            "cmd_gamble_var": self.settings.cmd_gamble,
            "cmd_duel_var": self.settings.cmd_duel,
            "cmd_duel_accept_var": self.settings.cmd_duel_accept,
            "cmd_duel_decline_var": self.settings.cmd_duel_decline,
            "gambling_enabled_var": self.settings.gambling_enabled,
            "duels_enabled_var": self.settings.duels_enabled,
            "gamble_minimum_var": self.settings.gamble_minimum,
            "gamble_maximum_var": self.settings.gamble_maximum,
            "gamble_win_chance_var": self.settings.gamble_win_chance_percent,
            "gamble_payout_var": self.settings.gamble_payout_multiplier,
            "gamble_cooldown_var": self.settings.gamble_cooldown_seconds,
            "duel_minimum_var": self.settings.duel_minimum,
            "duel_maximum_var": self.settings.duel_maximum,
            "duel_timeout_var": self.settings.duel_timeout_seconds,
            "duel_cooldown_var": self.settings.duel_cooldown_seconds,
            "raffle_enabled_var": self.settings.raffle_enabled,
            "raffle_title_var": self.settings.raffle_default_title,
            "cmd_raffle_enter_var": self.settings.cmd_raffle_enter,
            "raffle_duration_var": self.settings.raffle_duration_seconds,
            "raffle_countdown_interval_var": self.settings.raffle_countdown_interval_seconds,
            "raffle_reward_var": self.settings.raffle_reward_points,
            "streamerbot_http_enabled_var": self.settings.streamerbot_http_enabled,
            "streamerbot_http_url_var": self.settings.streamerbot_http_url,
        }
        for attribute, value in {**general_variables, **automation_variables}.items():
            variable = getattr(self, attribute, None)
            if variable is not None:
                variable.set(value)
        for response_name, variable in self.builtin_response_vars.items():
            variable.set(
                self.settings.builtin_responses.get(
                    response_name, DEFAULT_BUILTIN_RESPONSES[response_name]
                )
            )
        for attribute, variable in getattr(self, "_alias_vars", {}).items():
            variable.set(getattr(self.settings, attribute))
        self.custom_command_rules = [
            dict(rule) for rule in self.settings.custom_commands
        ]
        self.timed_message_rules = [
            dict(rule) for rule in self.settings.timed_messages
        ]
        self._refresh_custom_command_tree()
        self._refresh_timer_tree()
        self._clear_custom_command_editor()
        self._clear_timer_editor()

    def _refresh_profile_choices(self, selected_name=None):
        names = sorted(self.settings.profiles, key=str.casefold)
        self.profile_combo.configure(values=names)
        if selected_name is not None:
            self.profile_var.set(selected_name)

    def _persist_profile_runtime_state(self, profile):
        state_path = self.settings.bot_state_path
        state = {}
        try:
            if state_path.exists():
                state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}

        state.update({
            "is_sr_enabled": bool(profile["accept_requests"]),
            "is_title_visible": bool(profile["show_title"]),
            "is_time_visible": bool(profile["show_time"]),
            "is_progress_visible": bool(profile["show_progress"]),
        })
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _save_current_profile(self):
        name = self.profile_var.get().strip()
        if not name:
            self.profile_status_var.set("Enter a profile name first.")
            return
        if len(name) > 40:
            self.profile_status_var.set("Profile names must be 40 characters or fewer.")
            return

        try:
            profile = self._capture_current_profile()
        except (tk.TclError, ValueError):
            self.profile_status_var.set("Fix invalid window or font-size values before saving.")
            return

        self.settings.profiles[name] = profile
        if self.bot_instance:
            self.bot_instance.settings.profiles[name] = dict(profile)
        save_settings(self.settings)
        self._refresh_profile_choices(name)
        self.profile_status_var.set(f"Saved profile: {name}")
        logging.getLogger("StandaloneBot").info(f"Saved stream profile '{name}'.")

    def _apply_selected_profile(self):
        name = self.profile_var.get().strip()
        profile = self.settings.profiles.get(name)
        if not profile:
            self.profile_status_var.set("Choose an existing profile to apply.")
            return

        previous_channel = self.settings.channel
        previous_port = self.settings.web_server_port
        self._autosave_suspended = True
        try:
            self.settings = apply_profile_settings(
                self.settings, profile.get("settings")
            )
            self._load_settings_into_ui()
            self.sr_enabled_var.set(bool(profile.get("accept_requests", True)))
            self.pos_var.set(
                profile.get("window_position", self.settings.sr_window_position)
            )
            self.w_var.set(
                profile.get("window_width", self.settings.sr_window_width)
            )
            self.h_var.set(
                profile.get("window_height", self.settings.sr_window_height)
            )
            self.bg_op_var.set(
                profile.get("background_opacity", self.settings.sr_bg_opacity)
            )
            self.title_font_size_var.set(
                profile.get("title_font_size", self.settings.sr_title_font_size)
            )
            self.time_font_size_var.set(
                profile.get("time_font_size", self.settings.sr_time_font_size)
            )
        finally:
            self._autosave_suspended = False
        self._apply_settings(quiet=True)
        self._apply_automation_settings(quiet=True)

        normalized_profile = self._capture_current_profile()
        normalized_profile.update({
            "accept_requests": bool(profile.get("accept_requests", True)),
            "show_title": bool(profile.get("show_title", False)),
            "show_time": bool(profile.get("show_time", False)),
            "show_progress": bool(profile.get("show_progress", False)),
        })
        self._persist_profile_runtime_state(normalized_profile)

        if self.bot_instance and getattr(self, "_bot_loop", None):
            self.bot_instance.is_sr_enabled = normalized_profile["accept_requests"]
            asyncio.run_coroutine_threadsafe(
                self.bot_instance.vlc_set_title_visible(normalized_profile["show_title"]),
                self._bot_loop
            )
            asyncio.run_coroutine_threadsafe(
                self.bot_instance.vlc_set_time_visible(normalized_profile["show_time"]),
                self._bot_loop
            )
            asyncio.run_coroutine_threadsafe(
                self.bot_instance.vlc_set_progress_visible(normalized_profile["show_progress"]),
                self._bot_loop
            )

        restart_required = (
            self.bot_instance
            and (
                self.settings.channel != previous_channel
                or self.settings.web_server_port != previous_port
            )
        )
        if restart_required:
            self.profile_status_var.set(
                f"Applied profile: {name}. Restart bot for channel/port changes."
            )
        else:
            self.profile_status_var.set(f"Applied profile: {name}")
        logging.getLogger("StandaloneBot").info(f"Applied stream profile '{name}'.")

    def _delete_selected_profile(self):
        name = self.profile_var.get().strip()
        if name not in self.settings.profiles:
            self.profile_status_var.set("Choose an existing profile to delete.")
            return
        if not messagebox.askyesno("Delete Profile", f"Delete the profile '{name}'?"):
            return

        del self.settings.profiles[name]
        if self.bot_instance:
            self.bot_instance.settings.profiles.pop(name, None)
        save_settings(self.settings)
        self._refresh_profile_choices("")
        self.profile_status_var.set(f"Deleted profile: {name}")
        logging.getLogger("StandaloneBot").info(f"Deleted stream profile '{name}'.")

    def _mark_local_library_dirty(self):
        if hasattr(self, "refresh_library_btn"):
            self.refresh_library_btn.configure(bootstyle="warning")
        if hasattr(self, "local_library_status_var"):
            self.local_library_status_var.set("Local library settings changed. Click Save & Refresh.")

    def _save_local_library_settings(self):
        enabled = self.local_library_enabled_var.get() if hasattr(self, "local_library_enabled_var") else False
        root_value = self.local_library_root_var.get().strip() if hasattr(self, "local_library_root_var") else ""
        changed = (
            self.settings.local_library_enabled != enabled
            or self.settings.local_library_root != root_value
        )
        self.settings.local_library_enabled = enabled
        self.settings.local_library_root = root_value
        if changed:
            save_settings(self.settings)
            logging.getLogger("StandaloneBot").info("Local library settings saved.")
        if getattr(self, "bot_instance", None):
            self.bot_instance.settings.local_library_enabled = enabled
            self.bot_instance.settings.local_library_root = root_value

    def _local_library_toggle_changed(self):
        self._save_local_library_settings()
        self._refresh_local_library()

    def _browse_local_library_root(self):
        selected = filedialog.askdirectory(
            title="Select Local Library Folder",
            initialdir=self.local_library_root_var.get() or str(Path.home())
        )
        if selected:
            self.local_library_root_var.set(selected)
            self._refresh_local_library()

    def _scan_local_library(self, root: Path):
        supported = {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".aac", ".mp4", ".webm", ".mov", ".mkv"}
        entries = []
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in supported:
                continue
            relative_path = str(path.relative_to(root))
            entries.append({
                "path": str(path.resolve()),
                "title": path.stem,
                "relative_path": relative_path,
            })
        entries.sort(key=lambda item: item["relative_path"].lower())
        return entries

    def _refresh_local_library(self, log_result: bool = True):
        self._save_local_library_settings()
        root_value = self.local_library_root_var.get().strip() if hasattr(self, "local_library_root_var") else ""
        enabled = self.local_library_enabled_var.get() if hasattr(self, "local_library_enabled_var") else False

        if not enabled:
            self.local_library_entries = []
            self.filtered_local_library_entries = []
            self.local_library_status_var.set("Local library is disabled.")
            self.refresh_library_btn.configure(bootstyle="primary")
            self._refresh_local_library_view()
            return

        if not root_value:
            self.local_library_entries = []
            self.filtered_local_library_entries = []
            self.local_library_status_var.set("Choose a local library folder.")
            self.refresh_library_btn.configure(bootstyle="primary")
            self._refresh_local_library_view()
            return

        root_path = Path(root_value)
        if not root_path.exists() or not root_path.is_dir():
            self.local_library_entries = []
            self.filtered_local_library_entries = []
            self.local_library_status_var.set("Configured local library folder does not exist.")
            self.refresh_library_btn.configure(bootstyle="danger")
            self._refresh_local_library_view()
            return

        self.local_library_entries = self._scan_local_library(root_path)
        self.local_library_status_var.set(f"{len(self.local_library_entries)} file(s) found in {root_path}")
        self.refresh_library_btn.configure(bootstyle="success")
        self._refresh_local_library_view()

        if log_result:
            logging.getLogger("StandaloneBot").info(
                f"Local library scan complete: {len(self.local_library_entries)} file(s) found."
            )

    def _refresh_local_library_view(self):
        query = self.local_library_search_var.get().strip().lower() if hasattr(self, "local_library_search_var") else ""
        if query:
            self.filtered_local_library_entries = [
                item for item in self.local_library_entries
                if query in item["title"].lower() or query in item["relative_path"].lower()
            ]
        else:
            self.filtered_local_library_entries = list(self.local_library_entries)

        if hasattr(self, "local_library_tree"):
            self.local_library_tree.delete(*self.local_library_tree.get_children())
            for index, item in enumerate(self.filtered_local_library_entries):
                self.local_library_tree.insert("", "end", iid=str(index), values=(item["title"], item["relative_path"]))

    def _gui_add_selected_local_files(self):
        if not self.bot_instance or not getattr(self, "_bot_loop", None):
            logging.getLogger("StandaloneBot").warning("Start the bot before queueing local library items.")
            return

        selected = self.local_library_tree.selection()
        if not selected:
            return

        entries = [self.filtered_local_library_entries[int(iid)] for iid in selected]

        async def do_add():
            sr_cog = self.bot_instance.get_cog("SRCog")
            if not sr_cog:
                return

            requester = self.settings.channel or "streamer"
            added = 0
            for entry in entries:
                _, error = await sr_cog.enqueue_local_file(entry["path"], requested_by=requester)
                if error:
                    logging.getLogger("StandaloneBot").warning(f"Local library add failed: {error}")
                    continue
                added += 1

            if added:
                logging.getLogger("StandaloneBot").info(f"Added {added} local library item(s) to the queue.")

        asyncio.run_coroutine_threadsafe(do_add(), self._bot_loop)

    def _apply_settings(self, quiet=False, refresh_library=True):
        try:
            volume = int(self.vol_var.get())
            opacity = int(self.bg_op_var.get())
            width = int(self.w_var.get())
            height = int(self.h_var.get())
            title_size = int(self.title_font_size_var.get())
            time_size = int(self.time_font_size_var.get())
            port = int(self.port_var.get())
        except (tk.TclError, TypeError, ValueError):
            self.save_settings_status_var.set(
                "Waiting for valid numeric settings."
            )
            return False
        self.settings.channel = self.channel_var.get().strip().lstrip('#').lower()
        self.settings.oauth_token = self.oauth_var.get().strip()
        self.settings.vlc_sr_volume = max(0, min(100, volume))
        self.settings.sr_window_position = self.pos_var.get()
        self.settings.sr_bg_opacity = max(0, min(100, opacity))
        self.settings.sr_window_width = max(160, min(1920, width))
        self.settings.sr_window_height = max(90, min(1080, height))
        self.settings.sr_title_font_size = max(8, min(48, title_size))
        self.settings.sr_time_font_size = max(8, min(48, time_size))
        self.w_var.set(self.settings.sr_window_width)
        self.h_var.set(self.settings.sr_window_height)
        self.title_font_size_var.set(self.settings.sr_title_font_size)
        self.time_font_size_var.set(self.settings.sr_time_font_size)
        self.settings.web_server_port = max(1, min(65535, port))
        self.settings.use_fair_queue = self.fair_queue_var.get()
        self.settings.local_library_enabled = self.local_library_enabled_var.get()
        self.settings.local_library_root = self.local_library_root_var.get().strip()
        self.settings.obs_ws_enabled = False
        self.settings.obs_ws_host = "127.0.0.1"
        self.settings.obs_ws_port = 4455
        self.settings.obs_ws_password = ""
        self.settings.obs_browser_source_name = ""
        self.settings.obs_browser_scene_name = ""
        self.settings.obs_force_show_on_play = False
        self.settings.obs_hide_when_idle = False
        self.settings.obs_auto_refresh = False

        # Save command aliases
        for attr, var in getattr(self, '_alias_vars', {}).items():
            val = var.get().strip().lstrip('!')
            if val:
                setattr(self.settings, attr, val)
        save_settings(self.settings)
        if refresh_library:
            self._refresh_local_library(log_result=False)
        self._mark_settings_saved()
        if not quiet:
            logging.getLogger("StandaloneBot").info("Settings saved to config.")

        # Dynamically push visual/audio changes to running bot
        if getattr(self, 'bot_instance', None):
            for attribute in BotSettings.model_fields:
                setattr(
                    self.bot_instance.settings,
                    attribute,
                    getattr(self.settings, attribute),
                )
            self.bot_instance.settings.channel = self.settings.channel
            self.bot_instance.settings.oauth_token = self.settings.oauth_token
            self.bot_instance.settings.vlc_sr_volume = self.settings.vlc_sr_volume
            self.bot_instance.settings.sr_window_position = self.settings.sr_window_position
            self.bot_instance.settings.sr_bg_opacity = self.settings.sr_bg_opacity
            self.bot_instance.settings.sr_window_width = self.settings.sr_window_width
            self.bot_instance.settings.sr_window_height = self.settings.sr_window_height
            self.bot_instance.settings.sr_title_font_size = self.settings.sr_title_font_size
            self.bot_instance.settings.sr_time_font_size = self.settings.sr_time_font_size
            self.bot_instance.settings.web_server_port = self.settings.web_server_port
            self.bot_instance.settings.use_fair_queue = self.settings.use_fair_queue
            self.bot_instance.settings.local_library_enabled = self.settings.local_library_enabled
            self.bot_instance.settings.local_library_root = self.settings.local_library_root
            self.bot_instance.settings.obs_ws_enabled = self.settings.obs_ws_enabled
            self.bot_instance.settings.obs_ws_host = self.settings.obs_ws_host
            self.bot_instance.settings.obs_ws_port = self.settings.obs_ws_port
            self.bot_instance.settings.obs_ws_password = self.settings.obs_ws_password
            self.bot_instance.settings.obs_browser_source_name = self.settings.obs_browser_source_name
            self.bot_instance.settings.obs_browser_scene_name = self.settings.obs_browser_scene_name
            self.bot_instance.settings.obs_force_show_on_play = self.settings.obs_force_show_on_play
            self.bot_instance.settings.obs_hide_when_idle = self.settings.obs_hide_when_idle
            self.bot_instance.settings.obs_auto_refresh = self.settings.obs_auto_refresh
            self.bot_instance.sr_volume_level = self.settings.vlc_sr_volume
            asyncio.run_coroutine_threadsafe(self.bot_instance.vlc_set_volume(self.settings.vlc_sr_volume), self._bot_loop)
            asyncio.run_coroutine_threadsafe(self.bot_instance.vlc_set_position(self.settings.sr_window_position), self._bot_loop)
            asyncio.run_coroutine_threadsafe(self.bot_instance.vlc_set_bg_opacity(self.settings.sr_bg_opacity), self._bot_loop)
            asyncio.run_coroutine_threadsafe(
                self.bot_instance.vlc_set_window_size(self.settings.sr_window_width, self.settings.sr_window_height), 
                self._bot_loop
            )
            asyncio.run_coroutine_threadsafe(
                self.bot_instance.vlc_set_hud_font_sizes(
                    self.settings.sr_title_font_size,
                    self.settings.sr_time_font_size
                ),
                self._bot_loop
            )
        return True

    def _toggle_sr(self):
        if self.bot_instance:
            self.bot_instance.is_sr_enabled = self.sr_enabled_var.get()
            s = "Enabled" if self.sr_enabled_var.get() else "Disabled"
            logging.getLogger("StandaloneBot").info(f"Requests {s}.")

    def _gui_skip(self):
        if not self.bot_instance: return
        async def do_skip():
            sr_cog = self.bot_instance.get_cog("SRCog")
            if not sr_cog: return
            
            if self.bot_instance.main_vlc_content_type == self.bot_instance.VLC_SR_PRIORITY:
                skipped_details = self.bot_instance.main_vlc_now_playing_details
                await self.bot_instance.vlc_stop_all(clear_state=False)
                await sr_cog.cleanup_played_song(skipped_details)
                await sr_cog.play_next_in_queue()
                logging.getLogger("StandaloneBot").info("Skipped currently playing SR from dashboard.")
            elif sr_cog.song_request_deque:
                s = sr_cog.pop_next_song()
                if not sr_cog.song_history_deque or sr_cog.song_history_deque[-1].get('filepath') != s.get('filepath'):
                    sr_cog.song_history_deque.append(s)
                logging.getLogger("StandaloneBot").info(f"Skipped upcoming queue song: {s.get('title')} ")
            else:
                logging.getLogger("StandaloneBot").info("Nothing to skip.")
                
        asyncio.run_coroutine_threadsafe(do_skip(), self._bot_loop)

    def _gui_play_pause(self):
        if not self.bot_instance: return
        asyncio.run_coroutine_threadsafe(self.bot_instance.vlc_toggle_pause(), self._bot_loop)
        
    def _gui_toggle_title(self):
        if not self.bot_instance: return
        asyncio.run_coroutine_threadsafe(self.bot_instance.vlc_toggle_title(), self._bot_loop)

    def _gui_toggle_time(self):
        if not self.bot_instance: return
        asyncio.run_coroutine_threadsafe(self.bot_instance.vlc_toggle_time(), self._bot_loop)

    def _gui_toggle_window_hidden(self):
        if not self.bot_instance: return
        asyncio.run_coroutine_threadsafe(self.bot_instance.vlc_toggle_window_hidden(), self._bot_loop)

    def _gui_toggle_progress_bar(self):
        if not self.bot_instance: return
        asyncio.run_coroutine_threadsafe(self.bot_instance.vlc_toggle_progress_bar(), self._bot_loop)
        
    def _gui_seek_release(self, event):
        self.is_dragging_slider = False
        if not self.bot_instance: return
        val = self.seek_var.get()
        asyncio.run_coroutine_threadsafe(self.bot_instance.vlc_seek(val), self._bot_loop)

    def _gui_clear_queue(self):
        if not self.bot_instance: return
        sr_cog = self.bot_instance.get_cog("SRCog")
        if sr_cog:
            num = sr_cog.clear_queue()
            logging.getLogger("StandaloneBot").info(f"Queue cleared ({num} items deleted).")

    def _gui_remove_selected(self):
        if not self.bot_instance: return
        sel = self.tree.selection()
        if not sel: return
        idx_val = self.tree.item(sel[0], "values")[0]
        idx = int(idx_val) - 1
        
        sr_cog = self.bot_instance.get_cog("SRCog")
        if sr_cog and 0 <= idx < len(sr_cog.song_request_deque):
            song = sr_cog.song_request_deque[idx]
            del sr_cog.song_request_deque[idx]
            sr_cog._persist_queue()
            logging.getLogger("StandaloneBot").info(f"Deleted '{song.get('title')}' via dashboard.")
            self._last_q_id = None

    def _gui_move_up(self):
        self._gui_move_selected(-1)

    def _gui_move_down(self):
        self._gui_move_selected(1)

    def _gui_move_selected(self, direction: int):
        if not self.bot_instance: return
        sel = self.tree.selection()
        if not sel: return
        idx_val = self.tree.item(sel[0], "values")[0]
        idx = int(idx_val) - 1
        sr_cog = self.bot_instance.get_cog("SRCog")
        if sr_cog:
            moved = sr_cog.move_song(idx, direction)
            if moved:
                self._last_q_id = None
                # Re-select the moved item after refresh
                self._pending_reselect = idx + direction

    def _check_for_updates(self):
        self.update_status_var.set("Checking GitHub...")

        def check():
            try:
                release = fetch_latest_release()
                newer = is_newer_version(release.version, APP_VERSION)
            except Exception as exc:
                self.root.after(
                    0,
                    lambda: self.update_status_var.set(
                        f"Update check failed: {exc}"
                    ),
                )
                return

            def show_result():
                if not newer:
                    self.update_status_var.set(f"Version {APP_VERSION} is current.")
                    return
                self.update_status_var.set(f"Version {release.version} is available.")
                installed_copy = is_installed_copy()
                has_app_update = installed_copy and bool(release.app_update_url)
                if has_app_update:
                    package_description = "the smaller app-only update"
                elif installed_copy and release.installer_url:
                    package_description = "the full installer"
                else:
                    package_description = "the official release page"
                preservation_note = (
                    "\n\nThe app-only update keeps the FFmpeg files already "
                    "installed on this computer."
                    if has_app_update
                    else ""
                )
                if messagebox.askyesno(
                    "Update Available",
                    (
                        f"Twitch Stream Bot {release.version} is available.\n\n"
                        f"Open {package_description}?"
                        f"{preservation_note}"
                    ),
                ):
                    webbrowser.open(release.update_url(installed_copy))

            self.root.after(0, show_result)

        threading.Thread(target=check, daemon=True).start()

    def start_bot(self):
        if getattr(self, "bot_thread", None) and self.bot_thread.is_alive():
            logging.getLogger("StandaloneBot").info("Bot is already starting or running.")
            return

        self._apply_settings()
        if not self._apply_automation_settings():
            logging.getLogger("StandaloneBot").error(
                "Cannot start bot until the Loyalty & Automation settings are valid."
            )
            self.notebook.select(self.automation_frame)
            return
        
        if not self.settings.oauth_token or not self.settings.channel:
            logging.getLogger("StandaloneBot").error("Cannot Start Bot! Ensure Token and Channel are entered.")
            self.notebook.select(self.settings_frame)
            return

        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.skip_btn.config(state=tk.NORMAL)
        self.remove_btn.config(state=tk.NORMAL)
        self.move_up_btn.config(state=tk.NORMAL)
        self.move_down_btn.config(state=tk.NORMAL)
        self.clear_btn.config(state=tk.NORMAL)
        self.overlay_btn.config(state=tk.NORMAL)
        self.time_toggle_btn.config(state=tk.NORMAL)
        self.progress_toggle_btn.config(state=tk.NORMAL)
        
        logging.getLogger("StandaloneBot").info("=> Initializing Services! ...")
        logging.getLogger("StandaloneBot").info(
            "Streamer.bot integration is %s.",
            "enabled" if self.settings.streamerbot_http_enabled else "disabled",
        )
        
        self._bot_loop = asyncio.new_event_loop()
        self.bot_thread = threading.Thread(target=self._run_bot_thread, daemon=True)
        self.bot_thread.start()

    def _run_bot_thread(self):
        asyncio.set_event_loop(self._bot_loop)
        self._bot_loop.set_exception_handler(self._handle_bot_loop_exception)
        try:
            from settings import BotSettings
            self.bot_instance = Bot(profile_applied_callback=self._handle_remote_profile_applied)
            self.bot_instance.is_sr_enabled = self.sr_enabled_var.get()
            self.bot_instance.run()
        except Exception as e:
            logging.getLogger("StandaloneBot").error(f"Engine crash code: {e}")
            if "Access Token" in str(e) or "token" in str(e).lower():
                logging.getLogger("StandaloneBot").error(
                    "Twitch rejected the OAuth token. Regenerate a Custom Bot Token with chat:read and chat:edit scopes, then paste the ACCESS TOKEN value into Settings."
                )
            self.root.after(0, self._reset_ui)
        finally:
            # Always save state when the bot thread exits, for any reason
            if self.bot_instance:
                try:
                    self.bot_instance.save_bot_state()
                    logging.getLogger("StandaloneBot").info("State auto-saved on exit.")
                except Exception:
                    pass

    def _handle_bot_loop_exception(self, loop, context):
        exception = context.get("exception")
        message = context.get("message", "")
        if isinstance(exception, ConnectionResetError) and "forcibly closed" in str(exception).lower():
            logging.getLogger("StandaloneBot").debug(f"Ignoring closed OBS/browser connection: {exception}")
            return
        if "_ProactorBasePipeTransport._call_connection_lost" in message:
            logging.getLogger("StandaloneBot").debug(f"Ignoring closed Windows pipe transport: {message}")
            return
        loop.default_exception_handler(context)

    def _handle_remote_profile_applied(self, name, profile):
        def update_dashboard():
            self._autosave_suspended = True
            try:
                self.settings = apply_profile_settings(
                    self.settings, profile.get("settings")
                )
                self.settings.profiles[name] = dict(profile)
                self.settings.sr_window_position = profile["window_position"]
                self.settings.sr_window_width = profile["window_width"]
                self.settings.sr_window_height = profile["window_height"]
                self.settings.sr_bg_opacity = profile["background_opacity"]
                self.settings.sr_title_font_size = profile["title_font_size"]
                self.settings.sr_time_font_size = profile["time_font_size"]
                self._load_settings_into_ui()
                self.profile_var.set(name)
                self.sr_enabled_var.set(profile["accept_requests"])
            finally:
                self._autosave_suspended = False
            self.profile_status_var.set(f"Applied by Streamer.bot: {name}")

        self.root.after(0, update_dashboard)

    def stop_bot(self):
        if not getattr(self, "bot_thread", None) or not self.bot_thread.is_alive():
            self._reset_ui()
            return

        self.stop_btn.config(state=tk.DISABLED)
        self.start_btn.config(state=tk.DISABLED)
        logging.getLogger("StandaloneBot").info("Saving state and shutting down...")
        if not self.bot_instance:
            logging.getLogger("StandaloneBot").info("Bot was still starting; resetting dashboard controls.")
            self._reset_ui()
            return

        # Save state synchronously before async shutdown
        try:
            self.bot_instance.save_bot_state()
        except Exception:
            pass

        future = asyncio.run_coroutine_threadsafe(self._shutdown_bot(), self._bot_loop)

        def _shutdown_done(_future):
            try:
                _future.result()
            except Exception as exc:
                logging.getLogger("StandaloneBot").warning(f"Shutdown reported an error: {exc}")

        future.add_done_callback(_shutdown_done)
    
    async def _shutdown_bot(self):
        try:
            await asyncio.wait_for(self.bot_instance.shutdown(), timeout=5)
        except asyncio.TimeoutError:
            logging.getLogger("StandaloneBot").warning("Bot shutdown timed out; resetting dashboard controls.")
        except Exception as exc:
            logging.getLogger("StandaloneBot").warning(f"Bot shutdown hit an error: {exc}")
        finally:
            self.root.after(0, self._reset_ui)

    def _reset_ui(self):
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.skip_btn.config(state=tk.DISABLED)
        self.remove_btn.config(state=tk.DISABLED)
        self.move_up_btn.config(state=tk.DISABLED)
        self.move_down_btn.config(state=tk.DISABLED)
        self.clear_btn.config(state=tk.DISABLED)
        self.seek_slider.config(state=tk.DISABLED)
        self.play_btn.config(state=tk.DISABLED)
        self.hide_toggle_btn.config(state=tk.DISABLED)
        self.overlay_btn.config(state=tk.DISABLED, text="Title: Off", bootstyle="outline-info")
        self.time_toggle_btn.config(state=tk.DISABLED, text="Time: Off", bootstyle="outline-info")
        self.progress_toggle_btn.config(state=tk.DISABLED, text="Bar: Off", bootstyle="outline-info")
        self.add_local_btn.config(state=tk.DISABLED)
        logging.getLogger("StandaloneBot").info("All services offline.")
        
        self.tree.delete(*self.tree.get_children())
        self.np_title_var.set("Waiting...")
        self.np_req_var.set("")
        self._last_q_id = None
        self.bot_instance = None
        self.bot_thread = None
        self._bot_loop = None

if __name__ == "__main__":
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except:
        pass

    import atexit

    root = tb.Window(themename="darkly")
    app = BotApp(root)

    def _on_close():
        # Save state before the window closes regardless of how it's triggered
        app.flush_pending_autosave()
        if app.bot_instance:
            try:
                app.bot_instance.save_bot_state()
            except Exception:
                pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)

    def _atexit_save():
        if app.bot_instance:
            try:
                app.bot_instance.save_bot_state()
            except Exception:
                pass

    atexit.register(_atexit_save)

    root.mainloop()
