from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import sounddevice as sd
import soundfile as sf
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "Minecraft Redub"
DEFAULT_INDEX_FILE = "29.json"
DEFAULT_ASSETS_ROOT = Path("compressContents") / "assets"
DEFAULT_OBJECTS_ROOT = Path("objects")
DEFAULT_RECORD_DELAY_SECONDS = 0.75
DEFAULT_SAMPLE_RATE = 44100
MAX_TRIM_PADDING_MS = 1500
MAX_ALIGN_PADDING_MS = 5000


@dataclass(frozen=True)
class AudioItem:
	relative_path: Path
	object_hash: str
	size: int

	def source_path(self, objects_root: Path) -> Path:
		return objects_root / self.object_hash[:2] / self.object_hash

	def target_path(self, assets_root: Path) -> Path:
		return assets_root / self.relative_path


def load_audio_items(index_path: Path) -> list[AudioItem]:
	with index_path.open("r", encoding="utf-8") as handle:
		index_data = json.load(handle)

	objects = index_data.get("objects", {})
	items: list[AudioItem] = []
	for path_text, metadata in objects.items():
		if not str(path_text).lower().endswith(".ogg"):
			continue
		if not isinstance(metadata, dict):
			continue
		relative_path = Path(path_text)
		object_hash = str(metadata.get("hash", ""))
		if len(object_hash) < 2:
			continue
		size = int(metadata.get("size", 0) or 0)
		items.append(AudioItem(relative_path=relative_path, object_hash=object_hash, size=size))

	return items


def ensure_parent_dir(path: Path) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)


def safe_relative_audio(path: Path) -> Path:
	parts = [part for part in path.parts if part not in (".", "")]
	return Path(*parts)


def mix_down_to_mono(data: np.ndarray) -> np.ndarray:
	if data.ndim == 1:
		return np.asarray(data, dtype=np.float32)
	if data.shape[1] == 1:
		return np.asarray(data[:, 0], dtype=np.float32)
	return np.asarray(np.mean(data, axis=1), dtype=np.float32)


def read_audio(path: Path) -> tuple[np.ndarray, int]:
	data, sample_rate = sf.read(path, dtype="float32", always_2d=True)
	return mix_down_to_mono(data), int(sample_rate)


def auto_trim(audio: np.ndarray, threshold_ratio: float = 0.04, pad_ms: int = 25, sample_rate: int = DEFAULT_SAMPLE_RATE) -> tuple[np.ndarray, int, int]:
	if audio.size == 0:
		return audio, 0, 0

	peak = float(np.max(np.abs(audio)))
	if peak <= 0:
		return audio, 0, 0

	threshold = max(0.01, peak * threshold_ratio)
	active = np.flatnonzero(np.abs(audio) >= threshold)
	if active.size == 0:
		return audio, 0, 0

	pad_samples = int(sample_rate * pad_ms / 1000)
	start = max(0, int(active[0]) - pad_samples)
	end = min(audio.size, int(active[-1]) + pad_samples + 1)
	return audio[start:end], start, audio.size - end


def apply_manual_trim(audio: np.ndarray, sample_rate: int, lead_ms: float, tail_ms: float) -> np.ndarray:
	if audio.size == 0:
		return audio

	lead_samples = int(sample_rate * lead_ms / 1000.0)
	tail_samples = int(sample_rate * tail_ms / 1000.0)

	start_index = 0
	end_index = audio.size
	pre_pad = 0
	post_pad = 0

	if lead_samples >= 0:
		start_index = min(audio.size, lead_samples)
	else:
		pre_pad = min(abs(lead_samples), int(MAX_TRIM_PADDING_MS * sample_rate / 1000))

	if tail_samples >= 0:
		end_index = max(start_index, audio.size - tail_samples)
	else:
		post_pad = min(abs(tail_samples), int(MAX_TRIM_PADDING_MS * sample_rate / 1000))

	result = audio[start_index:end_index]
	if pre_pad > 0:
		result = np.concatenate((np.zeros(pre_pad, dtype=np.float32), result))
	if post_pad > 0:
		result = np.concatenate((result, np.zeros(post_pad, dtype=np.float32)))

	return result

def resample_audio(audio: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    if audio.size == 0 or src_sr == dst_sr:
        return audio
    duration = float(audio.size) / float(src_sr)
    new_len = int(round(duration * float(dst_sr)))
    if new_len <= 0:
        return np.zeros(0, dtype=np.float32)
    src_positions = np.linspace(0.0, duration, num=audio.size, endpoint=False)
    dst_positions = np.linspace(0.0, duration, num=new_len, endpoint=False)
    return np.interp(dst_positions, src_positions, audio).astype(np.float32)

class WaveformCanvas(ttk.Frame):
	def __init__(self, master: tk.Misc, title: str, color: str) -> None:
		super().__init__(master)
		self._title = ttk.Label(self, text=title, font=("Segoe UI", 11, "bold"))
		self._title.pack(anchor="w", pady=(0, 4))
		self._canvas = tk.Canvas(self, width=460, height=180, background="#111827", highlightthickness=1, highlightbackground="#374151")
		self._canvas.pack(fill="both", expand=True)
		self._color = color
		self._data = np.zeros(0, dtype=np.float32)
		self._reference_peak = 0.0
		self._marker_positions: list[float] = []

	def set_audio(self, audio: np.ndarray | None, sample_rate: int | None = None, reference_peak: float | None = None) -> None:
		self._data = np.asarray(audio if audio is not None else np.zeros(0, dtype=np.float32), dtype=np.float32)
		self._reference_peak = max(0.0, float(reference_peak)) if reference_peak is not None else 0.0
		self.redraw()

	def set_markers(self, marker_positions: list[float] | None) -> None:
		self._marker_positions = [] if marker_positions is None else [min(1.0, max(0.0, float(p))) for p in marker_positions]
		self.redraw()

	def redraw(self) -> None:
		self._canvas.delete("all")
		width = max(1, self._canvas.winfo_width())
		height = max(1, self._canvas.winfo_height())
		self._canvas.create_line(0, height // 2, width, height // 2, fill="#374151")
		self._canvas.create_text(8, 8, text=self._title.cget("text"), anchor="nw", fill="#d1d5db", font=("Segoe UI", 9))

		if self._data.size == 0:
			self._canvas.create_text(width // 2, height // 2, text="No audio loaded", fill="#9ca3af")
			return

		sample_count = self._data.size
		bins = min(sample_count, max(120, width))
		points_per_bin = max(1, sample_count // bins)
		amplitude = self._reference_peak if self._reference_peak > 0 else float(np.max(np.abs(self._data)))
		if amplitude <= 0:
			amplitude = 1.0
		center_y = height / 2
		step_x = width / bins

		for bin_index in range(bins):
			start = bin_index * points_per_bin
			end = sample_count if bin_index == bins - 1 else min(sample_count, start + points_per_bin)
			segment = self._data[start:end]
			if segment.size == 0:
				continue
			peak = float(np.max(np.abs(segment)))
			normalized = peak / amplitude
			bar_height = max(1.0, normalized * (height * 0.38))
			x = bin_index * step_x
			self._canvas.create_line(x, center_y - bar_height, x, center_y + bar_height, fill=self._color)

		for marker in self._marker_positions:
			x = marker * width
			self._canvas.create_line(x, 0, x, height, fill="#ef4444", width=2)


class Tooltip:
	def __init__(self, widget: tk.Widget, text: str) -> None:
		self.widget = widget
		self.text = text
		self.tipwindow: tk.Toplevel | None = None
		self._after_id: str | None = None
		widget.bind("<Enter>", self._schedule_show)
		widget.bind("<Leave>", self._hide_tip)
		widget.bind("<Motion>", self._move_tip)

	def _schedule_show(self, _event: tk.Event) -> None:
		self._after_id = self.widget.after(300, self._show_tip)

	def _move_tip(self, event: tk.Event) -> None:
		if self.tipwindow:
			x = event.x_root + 16
			y = event.y_root + 16
			self.tipwindow.geometry(f"+{x}+{y}")

	def _show_tip(self) -> None:
		if self.tipwindow or not self.text:
			return
		x = self.widget.winfo_rootx() + 16
		y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
		self.tipwindow = tk.Toplevel(self.widget)
		self.tipwindow.wm_overrideredirect(True)
		self.tipwindow.wm_geometry(f"+{x}+{y}")
		label = tk.Label(
			self.tipwindow,
			text=self.text,
			justify="left",
			background="#111827",
			foreground="#f8fafc",
			relief="solid",
			borderwidth=1,
			font=("Segoe UI", 9),
			padx=6,
			pady=4,
		)
		label.pack()

	def _hide_tip(self, _event: tk.Event | None = None) -> None:
		if self._after_id is not None:
			self.widget.after_cancel(self._after_id)
			self._after_id = None
		if self.tipwindow:
			self.tipwindow.destroy()
			self.tipwindow = None


class MinecraftRedubApp:
	def __init__(self, root: tk.Tk, index_path: Path, assets_root: Path, objects_root: Path) -> None:
		self.root = root
		self.index_path = index_path
		self.assets_root = assets_root
		self.objects_root = objects_root

		self.items = load_audio_items(index_path)
		self.current_index = 0
		self.current_item: AudioItem | None = None

		self.original_audio = np.zeros(0, dtype=np.float32)
		self.original_rate = DEFAULT_SAMPLE_RATE
		self.recorded_audio = np.zeros(0, dtype=np.float32)
		self.recorded_rate = DEFAULT_SAMPLE_RATE
		self.trimmed_audio = np.zeros(0, dtype=np.float32)
		self.aligned_audio = np.zeros(0, dtype=np.float32)
		self.auto_lead_ms = 0.0
		self.auto_tail_ms = 0.0
		self._waveform_reference_peak = 1.0
		self.trim_too_long = False

		self.record_delay_ms = int(DEFAULT_RECORD_DELAY_SECONDS * 1000)
		self.recording = False
		self.recording_ready = False
		self._recording_thread: threading.Thread | None = None
		self._recording_stream: sd.InputStream | None = None
		self._recorded_chunks: list[np.ndarray] = []
		self._recording_lock = threading.Lock()
		self._playback_lock = threading.Lock()
		self._adjusting_silence = False
		self._adjusting_trim = False
		self._status_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()

		self.lead_trim_var = tk.DoubleVar(value=0.0)
		self.tail_trim_var = tk.DoubleVar(value=0.0)
		self.pre_silence_var = tk.DoubleVar(value=0.0)
		self.post_silence_var = tk.DoubleVar(value=0.0)
		self.status_var = tk.StringVar(value="Loading assets...")
		self.warning_var = tk.StringVar(value="")
		self.progress_var = tk.StringVar(value="")
		self.item_var = tk.StringVar(value="")
		self.original_duration_var = tk.StringVar(value="Original: --")
		self.recorded_duration_var = tk.StringVar(value="Recorded: --")
		self.trimmed_duration_var = tk.StringVar(value="Trimmed: --")
		self.aligned_duration_var = tk.StringVar(value="Final: --")

		self._build_ui()
		self._update_trim_slider_limits()
		self.root.after(100, self._drain_status_queue)
		self.root.after(120, self._refresh_canvases)
		self.root.after(150, self._load_next_item)

	def _build_ui(self) -> None:
		self.root.title(APP_TITLE)
		self.root.geometry("1180x860")
		self.root.minsize(1080, 780)
		self.root.configure(background="#0f172a")

		style = ttk.Style(self.root)
		style.theme_use("clam")
		style.configure("TFrame", background="#0f172a")
		style.configure("Card.TFrame", background="#111827")
		style.configure("TLabel", background="#0f172a", foreground="#e5e7eb", font=("Segoe UI", 10))
		style.configure("Muted.TLabel", background="#0f172a", foreground="#94a3b8", font=("Segoe UI", 9))
		style.configure("Card.TLabel", background="#111827", foreground="#e5e7eb", font=("Segoe UI", 10))
		style.configure("Warning.TLabel", background="#111827", foreground="#f87171", font=("Segoe UI", 9, "bold"))
		style.configure("Title.TLabel", background="#0f172a", foreground="#f8fafc", font=("Segoe UI Semibold", 18))
		style.configure("TButton", padding=(12, 8), font=("Segoe UI", 10))
		style.configure("Accent.TButton", padding=(12, 8), font=("Segoe UI", 10, "bold"), foreground="#0f172a")
		style.map("Accent.TButton", foreground=[("active", "#0f172a")], background=[("active", "#fbbf24")])

		outer = ttk.Frame(self.root, padding=16)
		outer.pack(fill="both", expand=True)

		header = ttk.Frame(outer)
		header.pack(fill="x", pady=(0, 12))
		title_row = ttk.Frame(header)
		title_row.pack(fill="x")
		ttk.Label(title_row, text="minecraft redub", style="Title.TLabel").pack(side="left", anchor="w")
		header_actions = ttk.Frame(title_row)
		header_actions.pack(side="right", anchor="e")
		tk.Button(header_actions, text="Export Zip", command=self.export_zip).pack(side="right", padx=(8, 0))
		tk.Button(header_actions, text="Check Completion", command=self.check_completion).pack(side="right", padx=(8, 0))
		tk.Button(header_actions, text="Open Index File", command=self.choose_index_file).pack(side="right", padx=(8, 0))
		ttk.Label(header, text="redub the 'craft w/ a resource pack", style="Muted.TLabel").pack(side="top", anchor="w", pady=(4, 0), padx=(12, 0))

		status_bar = ttk.Frame(outer, style="Card.TFrame", padding=12)
		status_bar.pack(fill="x", pady=(0, 12))
		ttk.Label(status_bar, textvariable=self.item_var, style="Card.TLabel").pack(anchor="w")
		ttk.Label(status_bar, textvariable=self.progress_var, style="Card.TLabel").pack(anchor="w", pady=(2, 0))
		ttk.Label(status_bar, textvariable=self.status_var, style="Card.TLabel").pack(anchor="w", pady=(2, 0))
		self.warning_label = ttk.Label(status_bar, textvariable=self.warning_var, style="Warning.TLabel")
		self.warning_label.pack(anchor="w", pady=(2, 0))

		controls = ttk.Frame(outer)
		controls.pack(fill="x", pady=(0, 12))

		left_controls = ttk.Frame(controls, style="Card.TFrame", padding=12)
		left_controls.pack(side="left", fill="both", expand=True, padx=(0, 6))
		right_controls = ttk.Frame(controls, style="Card.TFrame", padding=12)
		right_controls.pack(side="left", fill="both", expand=True, padx=(6, 0))

		ttk.Label(left_controls, text="Source Audio", style="Card.TLabel", font=("Segoe UI", 11, "bold")).pack(anchor="w")
		ttk.Label(left_controls, textvariable=self.original_duration_var, style="Card.TLabel").pack(anchor="w", pady=(2, 0))
		ttk.Button(left_controls, text="Play Original", command=self.play_original, style="Accent.TButton").pack(anchor="w", pady=(10, 0))
		# Move the Start Recording button to the left controls area (replacing the old Open Index placement)
		self.record_button = ttk.Button(left_controls, text="Start Recording", command=self.toggle_recording, style="Accent.TButton")
		self.record_button.pack(anchor="w", pady=(8, 0))

		ttk.Label(right_controls, text="Recording Workflow", style="Card.TLabel", font=("Segoe UI", 11, "bold")).pack(anchor="w")
		ttk.Label(right_controls, textvariable=self.recorded_duration_var, style="Card.TLabel").pack(anchor="w", pady=(2, 0))
		ttk.Label(right_controls, textvariable=self.trimmed_duration_var, style="Card.TLabel").pack(anchor="w", pady=(2, 0))
		ttk.Label(right_controls, textvariable=self.aligned_duration_var, style="Card.TLabel").pack(anchor="w", pady=(2, 0))

		buttons = ttk.Frame(right_controls, style="Card.TFrame")
		buttons.pack(anchor="w", pady=(10, 0), fill="x")
		ttk.Button(buttons, text="Previous", command=self.load_previous_item, style="Accent.TButton").pack(side="left", padx=(0, 8))
		ttk.Button(buttons, text="Play Recorded", command=self.play_recorded, style="Accent.TButton").pack(side="left", padx=(0, 8))
		# Place 'Next Without Saving' immediately to the right of 'Play Recorded'
		ttk.Button(buttons, text="Next But Dont Save", command=self.next_without_saving, style="Accent.TButton").pack(side="left", padx=(0, 8))
		# Keep Save on the far right
		self.save_button = ttk.Button(buttons, text="Save & Next", command=self.save_and_next, style="Accent.TButton")
		self.save_button.pack(side="right")

		trim_panel = ttk.Frame(outer, style="Card.TFrame", padding=12)
		trim_panel.pack(fill="x", pady=(0, 12))
		ttk.Label(trim_panel, text="Trim Controls", style="Card.TLabel", font=("Segoe UI", 11, "bold")).pack(anchor="w")

		# Create a single grid container for all trim sliders so they align globally
		trim_grid = ttk.Frame(trim_panel, style="Card.TFrame")
		trim_grid.pack(fill="x", pady=(8, 0))
		trim_grid.columnconfigure(1, weight=1)
		
		ttk.Label(trim_grid, text="Lead Trim (ms)", style="Card.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))
		self.lead_scale = ttk.Scale(trim_grid, from_=0, to=MAX_TRIM_PADDING_MS, variable=self.lead_trim_var, command=self._on_lead_trim_changed)
		self.lead_scale.grid(row=0, column=1, sticky="ew", padx=(12, 8), pady=(0, 4))
		self.lead_value_label = ttk.Label(trim_grid, text="0", style="Card.TLabel")
		self.lead_value_label.grid(row=0, column=2, sticky="e", pady=(0, 4))

		ttk.Label(trim_grid, text="Tail Trim (ms)", style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 4))
		self.tail_scale = ttk.Scale(trim_grid, from_=0, to=MAX_TRIM_PADDING_MS, variable=self.tail_trim_var, command=self._on_tail_trim_changed)
		self.tail_scale.grid(row=1, column=1, sticky="ew", padx=(12, 8), pady=(0, 4))
		self.tail_value_label = ttk.Label(trim_grid, text="0", style="Card.TLabel")
		self.tail_value_label.grid(row=1, column=2, sticky="e", pady=(0, 4))

		align_label = ttk.Label(trim_grid, text="Silence Placement (ms)", style="Card.TLabel", font=("Segoe UI", 10, "bold"))
		align_label.grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 4))

		ttk.Label(trim_grid, text="Pre-Silence (ms)", style="Card.TLabel").grid(row=3, column=0, sticky="w", pady=(0, 4))
		self.pre_scale = ttk.Scale(trim_grid, from_=0, to=MAX_ALIGN_PADDING_MS, variable=self.pre_silence_var, command=self._on_pre_silence_changed)
		self.pre_scale.grid(row=3, column=1, sticky="ew", padx=(12, 8), pady=(0, 4))
		self.pre_value_label = ttk.Label(trim_grid, text="0", style="Card.TLabel")
		self.pre_value_label.grid(row=3, column=2, sticky="e", pady=(0, 4))

		ttk.Label(trim_grid, text="Post-Silence (ms)", style="Card.TLabel").grid(row=4, column=0, sticky="w")
		self.post_scale = ttk.Scale(trim_grid, from_=0, to=MAX_ALIGN_PADDING_MS, variable=self.post_silence_var, command=self._on_post_silence_changed)
		self.post_scale.grid(row=4, column=1, sticky="ew", padx=(12, 8))
		self.post_value_label = ttk.Label(trim_grid, text="0", style="Card.TLabel")
		self.post_value_label.grid(row=4, column=2, sticky="e")

		waveforms = ttk.Frame(outer)
		waveforms.pack(fill="both", expand=True)
		self.original_canvas = WaveformCanvas(waveforms, "Original Waveform", "#38bdf8")
		self.original_canvas.pack(side="left", fill="both", expand=True, padx=(0, 6))
		self.recorded_canvas = WaveformCanvas(waveforms, "Recorded Waveform", "#f59e0b")
		self.recorded_canvas.pack(side="left", fill="both", expand=True, padx=(6, 0))

		footer = ttk.Frame(outer)
		footer.pack(fill="x", pady=(12, 0))
		footer_row = ttk.Frame(footer)
		footer_row.pack(fill="x")
		footer_label = ttk.Label(
			footer_row,
			text=(
				"Recording starts after a short delay so the key or click used to trigger it is not captured. "
				"Adjust trim if needed, then hit save."
			),
			style="Muted.TLabel",
			wraplength=940,
			justify="left",
		)
		footer_label.pack(side="left", fill="x", expand=True)
		hint_label = ttk.Label(footer_row, text="Keyboard shortcuts", style="Muted.TLabel", cursor="question_arrow")
		hint_label.pack(side="right")
		Tooltip(
			hint_label,
			"Left / A: Previous\n"
			"Right / D: Next but don't save\n"
			"Down / S: Play recorded\n"
			"Space: Start/Stop recording\n"
			"Enter: Save & Next\n"
			"P: Play original",
		)

		self.root.bind("<Configure>", lambda _event: self.root.after_idle(self._refresh_canvases))
		self._bind_keyboard_shortcuts()

	def _bind_keyboard_shortcuts(self) -> None:
		for sequence in ("<Left>", "<Key-a>"):
			self.root.bind(sequence, lambda event: self._invoke_action(self.load_previous_item, event))
		for sequence in ("<Right>", "<Key-d>"):
			self.root.bind(sequence, lambda event: self._invoke_action(self.next_without_saving, event))
		for sequence in ("<Down>", "<Key-s>"):
			self.root.bind(sequence, lambda event: self._invoke_action(self.play_recorded, event))
		self.root.bind("<space>", lambda event: self._invoke_action(self.toggle_recording, event))
		self.root.bind("<Return>", lambda event: self._invoke_action(self.save_and_next, event))
		self.root.bind("<Key-p>", lambda event: self._invoke_action(self.play_original, event))

	def _invoke_action(self, action: callable[[], None], event: tk.Event) -> str:
		event.widget.focus_set()
		action()
		return "break"

	def choose_index_file(self) -> None:
		selected = filedialog.askopenfilename(title="Choose index file", initialdir=str(self.index_path.parent), filetypes=[("JSON files", "*.json"), ("All files", "*")])
		if not selected:
			return

		self.index_path = Path(selected)
		self.items = load_audio_items(self.index_path)
		self.current_index = 0
		self.status_var.set(f"Loaded index file: {self.index_path}")
		self._load_next_item()

	def _drain_status_queue(self) -> None:
		try:
			while True:
				kind, message = self._status_queue.get_nowait()
				if kind == "status" and message is not None:
					self.status_var.set(message)
				elif kind == "advance" and message is not None:
					self.status_var.set(message)
		except queue.Empty:
			pass
		finally:
			self.root.after(120, self._drain_status_queue)

	def _enqueue_status(self, kind: str, message: str | None) -> None:
		self._status_queue.put((kind, message))

	def _set_progress(self) -> None:
		total = len(self.items)
		remaining = max(0, total - self.current_index)
		self.progress_var.set(f"Progress: {self.current_index}/{total} complete, {remaining} remaining")

	def _find_first_unrecorded_in_range(self, start: int, end: int) -> int | None:
		#this that binary shiz
		lo = start
		hi = max(start, end)
		if lo >= hi:
			return None

		def any_missing(up_to: int) -> bool:
			for i in range(start, up_to + 1):
				if not self.items[i].target_path(self.assets_root).exists():
					return True
			return False

		if not any_missing(end - 1):
			return None

		while lo < hi:
			mid = (lo + hi) // 2
			if any_missing(mid):
				hi = mid
			else:
				lo = mid + 1

		return lo if lo < end and not self.items[lo].target_path(self.assets_root).exists() else None

	def _load_next_item(self) -> None:
		idx = self._find_first_unrecorded_in_range(self.current_index, len(self.items))
		if idx is None:
			self.current_item = None
			self.item_var.set("All audio files in the index have already been created in compressContents/assets.")
			self.status_var.set("Nothing left to record.")
			self._set_progress()
			self.record_button.configure(state="disabled")
			return

		self.current_index = idx
		self.current_item = self.items[self.current_index]
		self._load_current_item()

	def _load_current_item(self) -> None:
		item = self.current_item
		if item is None:
			return

		source_path = item.source_path(self.objects_root)
		if not source_path.exists():
			self.status_var.set(f"Source missing for {item.relative_path}: {source_path}")
			self.record_button.configure(state="disabled")
			return

		try:
			self.original_audio, self.original_rate = read_audio(source_path)
		except Exception as exc:
			self.status_var.set(f"Could not load source audio: {exc}")
			return

		self._waveform_reference_peak = float(np.max(np.abs(self.original_audio))) if self.original_audio.size else 1.0
		if self._waveform_reference_peak <= 0:
			self._waveform_reference_peak = 1.0

		target_path = item.target_path(self.assets_root)
		if target_path.exists():
			try:
				loaded, rate = read_audio(target_path)
			except Exception:
				loaded = np.zeros(0, dtype=np.float32)
				rate = self.original_rate
			self.recorded_audio = loaded.copy()
			self.recorded_rate = rate
			self.status_var.set(f"Loaded existing take for {item.relative_path.as_posix()}")
		else:
			self.recorded_audio = np.zeros(0, dtype=np.float32)
		self.trimmed_audio = np.zeros(0, dtype=np.float32)
		self.aligned_audio = np.zeros(0, dtype=np.float32)
		self.auto_lead_ms = 0.0
		self.auto_tail_ms = 0.0
		self.lead_trim_var.set(0.0)
		self.tail_trim_var.set(0.0)
		self.pre_silence_var.set(0.0)
		self.post_silence_var.set(0.0)
		self._update_trim_value_labels()
		self.original_canvas.set_audio(self.original_audio, self.original_rate, reference_peak=self._waveform_reference_peak)
		self._refresh_trimmed_preview()
		self._update_trim_markers()
		self._render_recorded_preview()
		self._refresh_durations()
		self._set_progress()
		self.item_var.set(f"Current file: {item.relative_path.as_posix()}")
		self.status_var.set(f"Ready to record {item.relative_path.as_posix()}")
		self.record_button.configure(text="Start Recording", state="normal")

	def _refresh_durations(self) -> None:
		self.original_duration_var.set(f"Original: {self.original_audio.size / self.original_rate:.2f}s")
		if self.recorded_audio.size:
			self.recorded_duration_var.set(f"Recorded: {self.recorded_audio.size / self.recorded_rate:.2f}s")
		else:
			self.recorded_duration_var.set("Recorded: --")
		if self.trimmed_audio.size:
			self.trimmed_duration_var.set(f"Trimmed: {self.trimmed_audio.size / self.recorded_rate:.2f}s")
		else:
			self.trimmed_duration_var.set("Trimmed: --")
		if self.trim_too_long:
			self.aligned_duration_var.set("Final: exceeds target")
		elif self.aligned_audio.size:
			self.aligned_duration_var.set(f"Final: {self.aligned_audio.size / self.recorded_rate:.2f}s")
		elif self.original_audio.size:
			self.aligned_duration_var.set(f"Final: {self.original_audio.size / self.original_rate:.2f}s (target)")
		else:
			self.aligned_duration_var.set("Final: --")

	def _update_trim_value_labels(self) -> None:
		self.lead_value_label.configure(text=f"{self.lead_trim_var.get():.0f}")
		self.tail_value_label.configure(text=f"{self.tail_trim_var.get():.0f}")
		self.pre_value_label.configure(text=f"{self.pre_silence_var.get():.0f}")
		self.post_value_label.configure(text=f"{self.post_silence_var.get():.0f}")

	def _update_trim_slider_limits(self) -> None:
		if not hasattr(self, "lead_scale") or not hasattr(self, "tail_scale"):
			return

		if self.recorded_audio.size == 0:
			self.lead_scale.configure(to=MAX_TRIM_PADDING_MS)
			self.tail_scale.configure(to=MAX_TRIM_PADDING_MS)
			return

		duration_ms = (self.recorded_audio.size / float(max(1, self.recorded_rate))) * 1000.0
		current_lead = float(self.lead_trim_var.get())
		current_tail = float(self.tail_trim_var.get())
		lead_max = max(0.0, duration_ms - current_tail)
		tail_max = max(0.0, duration_ms - current_lead)
		self.lead_scale.configure(to=lead_max)
		self.tail_scale.configure(to=tail_max)
		self._adjusting_trim = True
		try:
			if current_lead > lead_max:
				self.lead_trim_var.set(lead_max)
			if current_tail > tail_max:
				self.tail_trim_var.set(tail_max)
		finally:
			self._adjusting_trim = False

	def _on_lead_trim_changed(self, _value: str | None = None) -> None:
		if self._adjusting_trim:
			return
		self._constrain_trim_pair(changed="lead")
		self.apply_trim_from_sliders()

	def _on_tail_trim_changed(self, _value: str | None = None) -> None:
		if self._adjusting_trim:
			return
		self._constrain_trim_pair(changed="tail")
		self.apply_trim_from_sliders()

	def _constrain_trim_pair(self, changed: str | None = None) -> None:
		if self.recorded_audio.size == 0:
			return

		duration_ms = (self.recorded_audio.size / float(max(1, self.recorded_rate))) * 1000.0
		lead = max(0.0, float(self.lead_trim_var.get()))
		tail = max(0.0, float(self.tail_trim_var.get()))

		if lead + tail <= duration_ms:
			self._update_trim_slider_limits()
			return

		if changed == "lead":
			lead = min(lead, duration_ms)
			tail = max(0.0, duration_ms - lead)
		elif changed == "tail":
			tail = min(tail, duration_ms)
			lead = max(0.0, duration_ms - tail)
		else:
			lead = min(lead, duration_ms)
			tail = max(0.0, duration_ms - lead)

		self._adjusting_trim = True
		try:
			self.lead_trim_var.set(lead)
			self.tail_trim_var.set(tail)
		finally:
			self._adjusting_trim = False

		self._update_trim_slider_limits()

	def _update_validation_state(self) -> None:
		target_len = self._target_output_length()
		trimmed_len = int(self.trimmed_audio.size)
		self.trim_too_long = bool(target_len > 0 and trimmed_len > target_len)
		if self.trim_too_long:
			target_seconds = target_len / float(max(1, self.recorded_rate))
			trimmed_seconds = trimmed_len / float(max(1, self.recorded_rate))
			self.warning_var.set(
				f"Trimmed clip is {trimmed_seconds:.2f}s, longer than the target {target_seconds:.2f}s. Reduce the crop before saving."
			)
		else:
			self.warning_var.set("")

		save_enabled = self.current_item is not None and self.recorded_audio.size > 0 and not self.trim_too_long
		self.save_button.configure(state="normal" if save_enabled else "disabled")

	def _target_output_length(self) -> int:
		if self.original_audio.size == 0:
			return 0
		target_seconds = float(self.original_audio.size) / float(max(1, self.original_rate))
		return int(round(target_seconds * float(max(1, self.recorded_rate))))

	def _on_pre_silence_changed(self, _value: str | None = None) -> None:
		if self._adjusting_silence:
			return
		self.apply_trim_from_sliders(changed="pre")

	def _on_post_silence_changed(self, _value: str | None = None) -> None:
		if self._adjusting_silence:
			return
		self.apply_trim_from_sliders(changed="post")

	def _enforce_silence_distribution(self, changed: str | None = None) -> None:
		target_len = self._target_output_length()
		trimmed_len = int(self.trimmed_audio.size)
		gap = max(0, target_len - max(0, trimmed_len))

		pre = max(0, int(self.recorded_rate * float(self.pre_silence_var.get()) / 1000.0))
		post = max(0, int(self.recorded_rate * float(self.post_silence_var.get()) / 1000.0))

		if gap <= 0:
			pre = 0
			post = 0
		elif changed == "pre":
			pre = min(pre, gap)
			post = gap - pre
		elif changed == "post":
			post = min(post, gap)
			pre = gap - post
		else:
			pre = min(pre, gap)
			post = gap - pre

		self._adjusting_silence = True
		try:
			self.pre_silence_var.set((pre / float(max(1, self.recorded_rate))) * 1000.0)
			self.post_silence_var.set((post / float(max(1, self.recorded_rate))) * 1000.0)
		finally:
			self._adjusting_silence = False

	def _refresh_canvases(self) -> None:
		self.original_canvas.redraw()
		self.recorded_canvas.redraw()

	def play_original(self) -> None:
		if self.original_audio.size == 0:
			return
		self._play_audio(self.original_audio, self.original_rate)

	def play_recorded(self) -> None:
		audio = self.aligned_audio if self.aligned_audio.size else (self.trimmed_audio if self.trimmed_audio.size else self.recorded_audio)
		rate = self.recorded_rate if audio.size else DEFAULT_SAMPLE_RATE
		if audio.size == 0:
			return
		self._play_audio(audio, rate)

	def _play_audio(self, audio: np.ndarray, sample_rate: int) -> None:
		def worker() -> None:
			with self._playback_lock:
				try:
					self._enqueue_status("status", "Playing audio...")
					sd.play(audio, sample_rate)
					sd.wait()
				except Exception as exc:
					self._enqueue_status("status", f"Playback failed: {exc}")
				else:
					self._enqueue_status("status", "Playback finished.")

		threading.Thread(target=worker, daemon=True).start()

	def toggle_recording(self) -> None:
		if self.recording:
			self.stop_recording()
		else:
			self.start_recording()

	def start_recording(self) -> None:
		if self.current_item is None:
			return
		if self.recording or self._recording_thread is not None:
			return

		self.recording_ready = False
		self.record_button.configure(text="Stop Recording")
		self.status_var.set(f"Recording will start in {self.record_delay_ms / 1000:.2f}s. Release the trigger now.")

		def worker() -> None:
			try:
				time.sleep(self.record_delay_ms / 1000.0)
				self._begin_input_stream()
			except Exception as exc:
				self._enqueue_status("status", f"Recording failed to start: {exc}")
				self._recording_thread = None
				self.root.after(0, lambda: self.record_button.configure(text="Start Recording"))

		self._recording_thread = threading.Thread(target=worker, daemon=True)
		self._recording_thread.start()

	def _begin_input_stream(self) -> None:
		with self._recording_lock:
			if self.recording:
				return

			self._recorded_chunks = []
			self.recording = True

			def callback(indata, _frames, _time_info, status) -> None:
				if status:
					self._enqueue_status("status", f"Recording warning: {status}")
				self._recorded_chunks.append(indata.copy())

			self._recording_stream = sd.InputStream(
				samplerate=max(self.original_rate, DEFAULT_SAMPLE_RATE),
				channels=1,
				dtype="float32",
				callback=callback,
			)
			self._recording_stream.start()
			self.recording_ready = True
			self._enqueue_status("status", "Recording started. Click Stop Recording when finished.")

	def stop_recording(self) -> None:
		if not self.recording and self._recording_thread is not None:
			self.status_var.set("Waiting for the delayed recording start to finish...")
			return

		if not self.recording:
			return

		with self._recording_lock:
			stream = self._recording_stream
			self._recording_stream = None
			self.recording = False
			self.recording_ready = False

		if stream is not None:
			try:
				stream.stop()
			finally:
				stream.close()

		chunks = self._recorded_chunks
		self._recorded_chunks = []
		self._recording_thread = None
		self.record_button.configure(text="Start Recording")

		if not chunks:
			self.status_var.set("No audio captured. Try recording again.")
			return

		recorded = np.concatenate(chunks, axis=0).reshape(-1)
		self.recorded_audio = np.asarray(recorded, dtype=np.float32)
		self.recorded_rate = max(self.original_rate, DEFAULT_SAMPLE_RATE)
		trimmed, lead_samples, tail_samples = auto_trim(self.recorded_audio, sample_rate=self.recorded_rate)
		self.recorded_audio = self.recorded_audio.copy()
		self.trimmed_audio = trimmed.copy()
		self.auto_lead_ms = (lead_samples / self.recorded_rate) * 1000.0
		self.auto_tail_ms = (tail_samples / self.recorded_rate) * 1000.0
		self.lead_trim_var.set(min(MAX_TRIM_PADDING_MS, self.auto_lead_ms))
		self.tail_trim_var.set(min(MAX_TRIM_PADDING_MS, self.auto_tail_ms))
		self._update_trim_value_labels()
		self.apply_trim_from_sliders()
		self.status_var.set("Recording captured. Review the waveform, adjust trim, or retry the recording.")

	def _refresh_trimmed_preview(self) -> None:
		if self.recorded_audio.size == 0:
			self.trimmed_audio = np.zeros(0, dtype=np.float32)
			self.aligned_audio = np.zeros(0, dtype=np.float32)
			self._update_validation_state()
			return

		lead_ms = float(self.lead_trim_var.get())
		tail_ms = float(self.tail_trim_var.get())
		self.trimmed_audio = apply_manual_trim(self.recorded_audio, self.recorded_rate, lead_ms, tail_ms)
		self._update_validation_state()
		if self.trim_too_long:
			self.aligned_audio = np.zeros(0, dtype=np.float32)
		else:
			self.aligned_audio = self._build_aligned_audio(self.trimmed_audio)

	def _build_aligned_audio(self, cropped_audio: np.ndarray) -> np.ndarray:
		if self.original_audio.size == 0:
			return np.asarray(cropped_audio, dtype=np.float32)

		target_len = self._target_output_length()
		if target_len <= 0:
			return np.asarray(cropped_audio, dtype=np.float32)

		cropped = np.asarray(cropped_audio, dtype=np.float32)
		if cropped.size >= target_len:
			return cropped[:target_len]

		pre_samples = max(0, int(self.recorded_rate * float(self.pre_silence_var.get()) / 1000.0))
		post_samples = max(0, int(self.recorded_rate * float(self.post_silence_var.get()) / 1000.0))
		max_gap = max(0, target_len - int(cropped.size))
		if pre_samples + post_samples != max_gap:
			pre_samples = min(pre_samples, max_gap)
			post_samples = max_gap - pre_samples

		out = np.zeros(target_len, dtype=np.float32)
		start = pre_samples
		stop_limit = target_len - post_samples
		copy_len = min(int(cropped.size), max(0, stop_limit - start))
		if copy_len > 0:
			out[start:start + copy_len] = cropped[:copy_len]
		return out

	def _render_recorded_preview(self) -> None:
		if self.trim_too_long:
			preview_audio = self.trimmed_audio if self.trimmed_audio.size else self.recorded_audio
			self.recorded_canvas.set_audio(preview_audio if preview_audio.size else None, self.recorded_rate, reference_peak=self._waveform_reference_peak)
			self.recorded_canvas.set_markers(None)
			return

		if self.aligned_audio.size:
			preview_audio = self.aligned_audio
		else:
			preview_audio = self.trimmed_audio if self.trimmed_audio.size else self.recorded_audio

		# to make sure that the waveform's y axis is scaled to the original audio's peak y val 
		self.recorded_canvas.set_audio(preview_audio if preview_audio.size else None, self.recorded_rate, reference_peak=self._waveform_reference_peak)
		self.recorded_canvas.set_markers(self._aligned_clip_markers() if self.aligned_audio.size else None)

	def _aligned_clip_markers(self) -> list[float] | None:
		if self.trimmed_audio.size == 0:
			return None
		target_len = self._target_output_length()
		if target_len <= 0:
			return None
		pre_samples = max(0, int(self.recorded_rate * float(self.pre_silence_var.get()) / 1000.0))
		start = min(target_len, pre_samples) / target_len
		end = min(target_len, pre_samples + int(self.trimmed_audio.size)) / target_len
		return [start, end]

	def _update_trim_markers(self) -> None:
		if self.recorded_audio.size == 0:
			self.recorded_canvas.set_markers(None)
			return

		record_len = int(self.recorded_audio.size)
		lead_samples = max(0, int(self.recorded_rate * float(self.lead_trim_var.get()) / 1000.0))
		tail_samples = max(0, int(self.recorded_rate * float(self.tail_trim_var.get()) / 1000.0))
		left = min(record_len, lead_samples) / record_len
		right = max(0, record_len - tail_samples) / record_len
		self.recorded_canvas.set_markers([left, right])

	def apply_trim_from_sliders(self, changed: str | None = None) -> None:
		self._update_trim_slider_limits()
		self._enforce_silence_distribution(changed=changed)
		self._update_trim_value_labels()
		self._refresh_trimmed_preview()
		self._update_trim_value_labels()
		self._update_trim_markers()
		self._render_recorded_preview()
		self._refresh_durations()
		if self.recorded_audio.size:
			self.status_var.set("Trim/placement updated. Preview again or save the file.")

	def retry_recording(self) -> None:
		# idk if this is even used anymore haha
		if self.recording:
			self.stop_recording()
		self.recorded_audio = np.zeros(0, dtype=np.float32)
		self.trimmed_audio = np.zeros(0, dtype=np.float32)
		self.aligned_audio = np.zeros(0, dtype=np.float32)
		self.recorded_rate = self.original_rate
		self.recorded_canvas.set_audio(None, reference_peak=self._waveform_reference_peak)
		self.recorded_canvas.set_markers(None)
		self.lead_trim_var.set(0.0)
		self.tail_trim_var.set(0.0)
		self.pre_silence_var.set(0.0)
		self.post_silence_var.set(0.0)
		self.apply_trim_from_sliders()
		self.status_var.set("Recording cleared. You can try again.")

	def load_previous_item(self) -> None:
		# to match the "next" button, this will just move down by 1 and load the previous item, no saving or anything
		if self.current_index <= 0:
			self.status_var.set("No previous item.")
			return
		self.current_index -= 1
		self.current_item = self.items[self.current_index]
		self._load_current_item()

	def finalize_audio_for_save(self, audio: np.ndarray) -> np.ndarray:
		# audio lengths NEED to match or else
		if self.original_audio.size and audio.size:
			target_seconds = float(self.original_audio.size) / float(max(1, self.original_rate))
			target_len = int(round(target_seconds * float(max(1, self.recorded_rate))))
			if audio.size == target_len:
				return audio
			if audio.size > target_len:
				return audio[:target_len]
			# pad with nothin to match length
			pad = np.zeros(target_len - audio.size, dtype=np.float32)
			return np.concatenate((audio, pad))
		return audio

	def check_completion(self) -> None:
		# this is more "just in case" and lowkey ill probably remove it later. i could also make it actually open all these files to make sure the files are valid 
		missing = [it.relative_path.as_posix() for it in self.items if not it.target_path(self.assets_root).exists()]
		if not missing:
			messagebox.showinfo(APP_TITLE, "All items are present — no missing audio files.")
		else:
			messagebox.showinfo(APP_TITLE, f"Missing {len(missing)} files. Example: {missing[:10]}")

	def export_zip(self) -> None:
		# idk if this works, if this comment is still here by the time its tested in a stable state, then it probably does
		base = Path(__file__).resolve().parent
		src = base / "compressContents"
		if not src.exists():
			messagebox.showerror(APP_TITLE, f"Could not find compressContents to export: {src}")
			return
		selected = filedialog.asksaveasfilename(title="Export resource pack as zip", defaultextension=".zip", filetypes=[("Zip files", "*.zip")])
		if not selected:
			return
		import shutil
		try:
			shutil.make_archive(Path(selected).with_suffix("").as_posix(), 'zip', root_dir=src)
		except Exception as exc:
			messagebox.showerror(APP_TITLE, f"Failed to create zip: {exc}")
			return
		messagebox.showinfo(APP_TITLE, f"Exported pack to {selected}")

	def save_and_next(self) -> None:
		try:
			if self.current_item is None:
				return
			if self.recorded_audio.size == 0:
				messagebox.showwarning(APP_TITLE, "Record audio before saving.")
				return
			if self.trim_too_long:
				self.status_var.set(self.warning_var.get() or "Trimmed clip is too long to save yet.")
				return

			audio_to_save = self.aligned_audio if self.aligned_audio.size else (self.trimmed_audio if self.trimmed_audio.size else self.recorded_audio)
			if audio_to_save.size == 0:
				messagebox.showwarning(APP_TITLE, "The trimmed audio is empty. Adjust the trim or retry the recording.")
				return

			# match audio length
			audio_to_save = self.finalize_audio_for_save(audio_to_save)

			target_path = self.current_item.target_path(self.assets_root)
			ensure_parent_dir(target_path)

			try:
    			# normalize type / remove NaNs/infs to avoid libsndfile crashes
				# this part sucked cause randomly some files would not save, turned out because what we were using to save into .ogg didn't handle super big khz
				# so we resample to 48k if the recorded rate is higher than that, which isn't the max but is lowkey good enough
				audio_to_save = np.asarray(audio_to_save, dtype=np.float32)
				audio_to_save = np.nan_to_num(audio_to_save, nan=0.0, posinf=0.0, neginf=0.0)
				disk_rate = int(round(self.recorded_rate))
				audio_for_disk = audio_to_save
				if disk_rate > 48000:
					audio_for_disk = resample_audio(audio_to_save, disk_rate, 48000)
					disk_rate = 48000
				audio_for_disk = np.ascontiguousarray(audio_for_disk, dtype=np.float32)
				sf.write(target_path, audio_for_disk, disk_rate, format="OGG", subtype="VORBIS")
			except Exception as exc:
				messagebox.showerror(APP_TITLE, f"Failed to save {target_path.as_posix()}\n\n{exc}")
				return

			self.status_var.set(f"Saved {target_path.as_posix()}")
			self._advance_to_next_item(clear_saved_take=True)
			
		except Exception as exc:
			# if theres a save error we should see it
			messagebox.showerror(APP_TITLE, f"Unexpected error while saving: {exc}")
			self.status_var.set(f"Save failed: {exc}")
			try:
				self.save_button.configure(state="disabled")
			except Exception:
				pass

	def next_without_saving(self) -> None:
		# move up by 1, mirror logic for "previous" and still don't save anything
		if self.current_index >= len(self.items) - 1:
			self.status_var.set("No next item.")
			return
		self.current_index += 1
		self.current_item = self.items[self.current_index]
		self._load_current_item()

	def _advance_to_next_item(self, clear_saved_take: bool) -> None:
		# this is for the next item without audio, should probably rename
		self.current_index += 1
		if clear_saved_take:
			self.recorded_audio = np.zeros(0, dtype=np.float32)
			self.trimmed_audio = np.zeros(0, dtype=np.float32)
			self.aligned_audio = np.zeros(0, dtype=np.float32)
			self.recorded_canvas.set_audio(None, reference_peak=self._waveform_reference_peak)
			self.recorded_canvas.set_markers(None)
			self._refresh_durations()
			self._set_progress()
		try:
			self._load_next_item()
		except Exception as exc:
			self.status_var.set(f"Could not load next item: {exc}")
			self.save_button.configure(state="disabled")
			self.record_button.configure(state="disabled")


def build_default_paths() -> tuple[Path, Path, Path]:
	base = Path(__file__).resolve().parent
	return base / DEFAULT_INDEX_FILE, base / DEFAULT_ASSETS_ROOT, base / DEFAULT_OBJECTS_ROOT


def main() -> None:
	index_path, assets_root, objects_root = build_default_paths()
	if not index_path.exists():
		raise FileNotFoundError(f"Could not find index file: {index_path}")
	if not objects_root.exists():
		raise FileNotFoundError(f"Could not find objects directory: {objects_root}")

	root = tk.Tk()
	app = MinecraftRedubApp(root, index_path=index_path, assets_root=assets_root, objects_root=objects_root)
	root.mainloop()


if __name__ == "__main__":
	main()
