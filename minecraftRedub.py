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

	lead_samples = max(0, int(sample_rate * lead_ms / 1000.0))
	tail_samples = max(0, int(sample_rate * tail_ms / 1000.0))
	start = min(audio.size, lead_samples)
	end = max(start, audio.size - tail_samples)
	return audio[start:end]


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

	def set_audio(self, audio: np.ndarray | None, sample_rate: int | None = None, reference_peak: float | None = None) -> None:
		self._data = np.asarray(audio if audio is not None else np.zeros(0, dtype=np.float32), dtype=np.float32)
		self._reference_peak = max(0.0, float(reference_peak)) if reference_peak is not None else 0.0
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
		self.auto_lead_ms = 0.0
		self.auto_tail_ms = 0.0
		self._waveform_reference_peak = 1.0

		self.record_delay_ms = int(DEFAULT_RECORD_DELAY_SECONDS * 1000)
		self.recording = False
		self.recording_ready = False
		self._recording_thread: threading.Thread | None = None
		self._recording_stream: sd.InputStream | None = None
		self._recorded_chunks: list[np.ndarray] = []
		self._recording_lock = threading.Lock()
		self._playback_lock = threading.Lock()
		self._status_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()

		self.lead_trim_var = tk.DoubleVar(value=0.0)
		self.tail_trim_var = tk.DoubleVar(value=0.0)
		self.status_var = tk.StringVar(value="Loading assets...")
		self.progress_var = tk.StringVar(value="")
		self.item_var = tk.StringVar(value="")
		self.original_duration_var = tk.StringVar(value="Original: --")
		self.recorded_duration_var = tk.StringVar(value="Recorded: --")
		self.trimmed_duration_var = tk.StringVar(value="Trimmed: --")

		self._build_ui()
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
		style.configure("Title.TLabel", background="#0f172a", foreground="#f8fafc", font=("Segoe UI Semibold", 18))
		style.configure("TButton", padding=(12, 8), font=("Segoe UI", 10))
		style.configure("Accent.TButton", padding=(12, 8), font=("Segoe UI", 10, "bold"), foreground="#0f172a")
		style.map("Accent.TButton", foreground=[("active", "#0f172a")], background=[("active", "#fbbf24")])

		outer = ttk.Frame(self.root, padding=16)
		outer.pack(fill="both", expand=True)

		header = ttk.Frame(outer)
		header.pack(fill="x", pady=(0, 12))
		ttk.Label(header, text="Minecraft Redub", style="Title.TLabel").pack(anchor="w")
		ttk.Label(header, text="Record, trim, preview, and save redubbed sound assets in sequence.", style="Muted.TLabel").pack(anchor="w", pady=(3, 0))

		status_bar = ttk.Frame(outer, style="Card.TFrame", padding=12)
		status_bar.pack(fill="x", pady=(0, 12))
		ttk.Label(status_bar, textvariable=self.item_var, style="Card.TLabel").pack(anchor="w")
		ttk.Label(status_bar, textvariable=self.progress_var, style="Card.TLabel").pack(anchor="w", pady=(2, 0))
		ttk.Label(status_bar, textvariable=self.status_var, style="Card.TLabel").pack(anchor="w", pady=(2, 0))

		controls = ttk.Frame(outer)
		controls.pack(fill="x", pady=(0, 12))

		left_controls = ttk.Frame(controls, style="Card.TFrame", padding=12)
		left_controls.pack(side="left", fill="both", expand=True, padx=(0, 6))
		right_controls = ttk.Frame(controls, style="Card.TFrame", padding=12)
		right_controls.pack(side="left", fill="both", expand=True, padx=(6, 0))

		ttk.Label(left_controls, text="Source Audio", style="Card.TLabel", font=("Segoe UI", 11, "bold")).pack(anchor="w")
		ttk.Label(left_controls, textvariable=self.original_duration_var, style="Card.TLabel").pack(anchor="w", pady=(2, 0))
		ttk.Button(left_controls, text="Play Original", command=self.play_original, style="Accent.TButton").pack(anchor="w", pady=(10, 0))
		ttk.Button(left_controls, text="Open Index File", command=self.choose_index_file).pack(anchor="w", pady=(8, 0))

		ttk.Label(right_controls, text="Recording Workflow", style="Card.TLabel", font=("Segoe UI", 11, "bold")).pack(anchor="w")
		ttk.Label(right_controls, textvariable=self.recorded_duration_var, style="Card.TLabel").pack(anchor="w", pady=(2, 0))
		ttk.Label(right_controls, textvariable=self.trimmed_duration_var, style="Card.TLabel").pack(anchor="w", pady=(2, 0))

		buttons = ttk.Frame(right_controls, style="Card.TFrame")
		buttons.pack(anchor="w", pady=(10, 0), fill="x")
		self.record_button = ttk.Button(buttons, text="Start Recording", command=self.toggle_recording, style="Accent.TButton")
		self.record_button.pack(side="left")
		ttk.Button(buttons, text="Play Recorded", command=self.play_recorded).pack(side="left", padx=(8, 0))
		ttk.Button(buttons, text="Retry Recording", command=self.retry_recording).pack(side="left", padx=(8, 0))
		ttk.Button(buttons, text="Apply Trim", command=self.apply_trim_from_sliders).pack(side="left", padx=(8, 0))
		ttk.Button(buttons, text="Save & Next", command=self.save_and_next, style="Accent.TButton").pack(side="right")

		trim_panel = ttk.Frame(outer, style="Card.TFrame", padding=12)
		trim_panel.pack(fill="x", pady=(0, 12))
		ttk.Label(trim_panel, text="Trim Controls", style="Card.TLabel", font=("Segoe UI", 11, "bold")).pack(anchor="w")

		lead_row = ttk.Frame(trim_panel, style="Card.TFrame")
		lead_row.pack(fill="x", pady=(8, 4))
		ttk.Label(lead_row, text="Lead Trim (ms)", style="Card.TLabel").pack(side="left")
		lead_scale = ttk.Scale(lead_row, from_=0, to=MAX_TRIM_PADDING_MS, variable=self.lead_trim_var, command=lambda _value: self.apply_trim_from_sliders())
		lead_scale.pack(side="left", fill="x", expand=True, padx=(12, 8))
		self.lead_value_label = ttk.Label(lead_row, text="0", style="Card.TLabel")
		self.lead_value_label.pack(side="left")

		tail_row = ttk.Frame(trim_panel, style="Card.TFrame")
		tail_row.pack(fill="x", pady=(4, 0))
		ttk.Label(tail_row, text="Tail Trim (ms)", style="Card.TLabel").pack(side="left")
		tail_scale = ttk.Scale(tail_row, from_=0, to=MAX_TRIM_PADDING_MS, variable=self.tail_trim_var, command=lambda _value: self.apply_trim_from_sliders())
		tail_scale.pack(side="left", fill="x", expand=True, padx=(12, 8))
		self.tail_value_label = ttk.Label(tail_row, text="0", style="Card.TLabel")
		self.tail_value_label.pack(side="left")

		waveforms = ttk.Frame(outer)
		waveforms.pack(fill="both", expand=True)
		self.original_canvas = WaveformCanvas(waveforms, "Original Waveform", "#38bdf8")
		self.original_canvas.pack(side="left", fill="both", expand=True, padx=(0, 6))
		self.recorded_canvas = WaveformCanvas(waveforms, "Recorded Waveform", "#f59e0b")
		self.recorded_canvas.pack(side="left", fill="both", expand=True, padx=(6, 0))

		footer = ttk.Frame(outer)
		footer.pack(fill="x", pady=(12, 0))
		ttk.Label(
			footer,
			text=(
				"Recording starts after a short delay so the key or click used to trigger it is not captured. "
				"Adjust trim if needed, then save the sound into the mirrored asset path."
			),
			style="Muted.TLabel",
			wraplength=1100,
			justify="left",
		).pack(anchor="w")

		self.root.bind("<Configure>", lambda _event: self.root.after_idle(self._refresh_canvases))

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

	def _load_next_item(self) -> None:
		while self.current_index < len(self.items):
			item = self.items[self.current_index]
			target_path = item.target_path(self.assets_root)
			if not target_path.exists():
				self.current_item = item
				self._load_current_item()
				return
			self.current_index += 1

		self.current_item = None
		self.item_var.set("All audio files in the index have already been created in compressContents/assets.")
		self.status_var.set("Nothing left to record.")
		self._set_progress()
		self.record_button.configure(state="disabled")

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
		except Exception as exc:  # pragma: no cover - defensive UI path
			self.status_var.set(f"Could not load source audio: {exc}")
			return

		self._waveform_reference_peak = float(np.max(np.abs(self.original_audio))) if self.original_audio.size else 1.0
		if self._waveform_reference_peak <= 0:
			self._waveform_reference_peak = 1.0

		self.recorded_audio = np.zeros(0, dtype=np.float32)
		self.trimmed_audio = np.zeros(0, dtype=np.float32)
		self.auto_lead_ms = 0.0
		self.auto_tail_ms = 0.0
		self.lead_trim_var.set(0.0)
		self.tail_trim_var.set(0.0)
		self._update_trim_value_labels()
		self.original_canvas.set_audio(self.original_audio, self.original_rate, reference_peak=self._waveform_reference_peak)
		self.recorded_canvas.set_audio(None, reference_peak=self._waveform_reference_peak)
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

	def _update_trim_value_labels(self) -> None:
		self.lead_value_label.configure(text=f"{self.lead_trim_var.get():.0f}")
		self.tail_value_label.configure(text=f"{self.tail_trim_var.get():.0f}")

	def _refresh_canvases(self) -> None:
		self.original_canvas.redraw()
		self.recorded_canvas.redraw()

	def play_original(self) -> None:
		if self.original_audio.size == 0:
			return
		self._play_audio(self.original_audio, self.original_rate)

	def play_recorded(self) -> None:
		audio = self.trimmed_audio if self.trimmed_audio.size else self.recorded_audio
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
			except Exception as exc:  # pragma: no cover - defensive UI path
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

			def callback(indata, _frames, _time_info, status) -> None:  # pragma: no cover - hardware callback
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
		self._refresh_trimmed_preview()
		self.recorded_canvas.set_audio(self.trimmed_audio, self.recorded_rate, reference_peak=self._waveform_reference_peak)
		self._refresh_durations()
		self.status_var.set("Recording captured. Review the waveform, adjust trim, or retry the recording.")

	def _refresh_trimmed_preview(self) -> None:
		if self.recorded_audio.size == 0:
			self.trimmed_audio = np.zeros(0, dtype=np.float32)
			return

		lead_ms = float(self.lead_trim_var.get())
		tail_ms = float(self.tail_trim_var.get())
		self.trimmed_audio = apply_manual_trim(self.recorded_audio, self.recorded_rate, lead_ms, tail_ms)

	def apply_trim_from_sliders(self) -> None:
		self._update_trim_value_labels()
		self._refresh_trimmed_preview()
		if self.trimmed_audio.size:
			self.recorded_canvas.set_audio(self.trimmed_audio, self.recorded_rate, reference_peak=self._waveform_reference_peak)
		else:
			self.recorded_canvas.set_audio(self.recorded_audio if self.recorded_audio.size else None, self.recorded_rate, reference_peak=self._waveform_reference_peak)
		self._refresh_durations()
		if self.recorded_audio.size:
			self.status_var.set("Trim updated. Preview again or save the file.")

	def retry_recording(self) -> None:
		if self.recording:
			self.stop_recording()
		self.recorded_audio = np.zeros(0, dtype=np.float32)
		self.trimmed_audio = np.zeros(0, dtype=np.float32)
		self.recorded_rate = self.original_rate
		self.recorded_canvas.set_audio(None, reference_peak=self._waveform_reference_peak)
		self.lead_trim_var.set(0.0)
		self.tail_trim_var.set(0.0)
		self._update_trim_value_labels()
		self._refresh_durations()
		self.status_var.set("Recording cleared. You can try again.")

	def save_and_next(self) -> None:
		if self.current_item is None:
			return
		if self.recorded_audio.size == 0:
			messagebox.showwarning(APP_TITLE, "Record audio before saving.")
			return

		audio_to_save = self.trimmed_audio if self.trimmed_audio.size else self.recorded_audio
		if audio_to_save.size == 0:
			messagebox.showwarning(APP_TITLE, "The trimmed audio is empty. Adjust the trim or retry the recording.")
			return

		target_path = self.current_item.target_path(self.assets_root)
		ensure_parent_dir(target_path)

		try:
			sf.write(target_path, audio_to_save, self.recorded_rate, format="OGG", subtype="VORBIS")
		except Exception as exc:
			messagebox.showerror(APP_TITLE, f"Failed to save {target_path.as_posix()}\n\n{exc}")
			return

		self.status_var.set(f"Saved {target_path.as_posix()}")
		self.current_index += 1
		self.recorded_audio = np.zeros(0, dtype=np.float32)
		self.trimmed_audio = np.zeros(0, dtype=np.float32)
		self.recorded_canvas.set_audio(None, reference_peak=self._waveform_reference_peak)
		self._refresh_durations()
		self._set_progress()
		self._load_next_item()


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
