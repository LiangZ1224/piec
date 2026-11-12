"""ST-FMR GUI using the same MeasurementApp framework as Measurements/AMR.

Physical RF control requires an RFSource-compatible PIEC driver; at the time of
writing the target repository includes a virtual RFSource but not a verified
model-specific Anritsu driver. Start in VIRTUAL mode for software testing.
"""

import threading
import tkinter as tk
from tkinter import ttk, messagebox

import matplotlib.pyplot as plt
import numpy as np

from piec.measurement.gui_utils import MeasurementApp
from piec.measurement.stfmr import STFMR, load_stfmr_data


DEFAULTS = {
    "fields_start": "0.7", "fields_stop": "0", "field_points": "50",
    "include_endpoint": "False", "field_unit": "calibration units",
    "rf_frequencies_ghz": "3.0", "rf_powers_dbm": "10",
    "lockin_frequencies_hz": "17.37", "angles_deg": "0",
    "lockin_amplitude": "0.4", "lockin_sensitivity": "",
    "measure_time": "3", "field_settle_s": "1", "rf_settle_s": "0",
    "angle_settle_s": "1", "sample_interval": "0.1",
    "calibration_file": "", "linear_volts_per_field_unit": "1.0",
    "motor_steps_per_180_deg": "380", "forward_direction": "0",
}

LABELS = {
    "fields_start": "First field (calibration units)",
    "fields_stop": "Last field (calibration units)",
    "field_points": "Number of field points",
    "include_endpoint": "Include last field (True/False)",
    "field_unit": "Magnetic field units (metadata)",
    "rf_frequencies_ghz": "RF frequencies (GHz, comma separated)",
    "rf_powers_dbm": "RF powers (dBm, comma separated)",
    "lockin_frequencies_hz": "Lock-in frequencies (Hz, comma separated)",
    "angles_deg": "Relative angles (deg, comma separated)",
    "lockin_amplitude": "Lock-in oscillator amplitude (V)",
    "lockin_sensitivity": "Lock-in sensitivity (optional)",
    "measure_time": "Averaging duration per point (s)",
    "field_settle_s": "Field settling delay (s)",
    "rf_settle_s": "RF settling delay (s)",
    "angle_settle_s": "Angle settling delay (s)",
    "sample_interval": "Sample interval (s)",
    "calibration_file": "Calibration .xlsx (required for real hardware)",
    "linear_volts_per_field_unit": "Linear conversion (virtual/verified only)",
    "motor_steps_per_180_deg": "Motor steps per 180°",
    "forward_direction": "Motor positive direction bit (0/1)",
}


def _parse_axis(text):
    """Read one or more comma-separated float values."""
    values = tuple(float(token.strip()) for token in text.split(",") if token.strip())
    if not values:
        raise ValueError("A scan axis cannot be empty")
    return values


class STFMRApp(MeasurementApp):
    """ST-FMR acquisition with live CSV plotting and pause/stop controls."""

    _instruments = ("daq", "lockin", "rf_source", "stepper_motor")

    def __init__(self, root):
        super().__init__(root, title="ST-FMR Measurement GUI", geometry="1600x900")
        self.save_dir_entry.insert(0, "scratch")
        self.dynamic_inputs = {}
        self.is_measuring = False
        self.paused = False
        self.measurement_thread = None
        self.worker_error = None
        self.setup_dynamic_inputs()
        print("ST-FMR ready: all instrument addresses default to VIRTUAL.")

    def setup_dynamic_inputs(self):
        self.dynamic_frame.config(text="ST-FMR MEASUREMENT INPUTS")
        canvas = tk.Canvas(self.dynamic_frame, height=445, bg="#2B2B2B", highlightthickness=0)
        scrollbar = ttk.Scrollbar(self.dynamic_frame, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        grid = ttk.Frame(canvas, style="Card.TFrame")
        canvas.create_window((0, 0), window=grid, anchor="nw")
        grid.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))

        for index, (name, value) in enumerate(DEFAULTS.items()):
            ttk.Label(grid, text=LABELS[name]).grid(row=index, column=0, sticky="w", pady=3)
            entry = ttk.Entry(grid, width=27)
            entry.grid(row=index, column=1, sticky="w", padx=4, pady=3)
            entry.insert(0, str(value))
            self.dynamic_inputs[name] = entry
        if hasattr(self, "_apply_saved_dynamic"):
            self._apply_saved_dynamic()
        ttk.Label(self.plot_config_frame, text="X axis:").grid(row=0, column=0)
        self.x_axis = ttk.Combobox(
            self.plot_config_frame,
            values=["field", "rf_frequency_hz", "rf_power_dbm", "angle_deg", "lockin_frequency_hz"],
            state="readonly", width=23,
        )
        self.x_axis.grid(row=0, column=1)
        self.x_axis.set("field")
        self.x_axis.bind("<<ComboboxSelected>>", self.plot_data)
        ttk.Label(self.plot_config_frame, text="Y axis:").grid(row=1, column=0)
        self.y_axis = ttk.Combobox(self.plot_config_frame, values=["X", "Y"], state="readonly")
        self.y_axis.grid(row=1, column=1)
        self.y_axis.set("X")
        self.y_axis.bind("<<ComboboxSelected>>", self.plot_data)

    def _get_parameters(self):
        params = {key: entry.get().strip() for key, entry in self.dynamic_inputs.items()}
        points = int(params["field_points"])
        if points < 1:
            raise ValueError("field_points must be >= 1")
        fields = np.linspace(
            float(params["fields_start"]), float(params["fields_stop"]), points,
            endpoint=params["include_endpoint"].lower() in ("true", "yes", "1"),
        )
        calibration_file = params["calibration_file"] or None
        linear = float(params["linear_volts_per_field_unit"]) if not calibration_file else None
        return dict(
            fields=fields,
            rf_frequencies_hz=tuple(v * 1e9 for v in _parse_axis(params["rf_frequencies_ghz"])),
            rf_powers_dbm=_parse_axis(params["rf_powers_dbm"]),
            lockin_frequencies_hz=_parse_axis(params["lockin_frequencies_hz"]),
            angles_deg=_parse_axis(params["angles_deg"]),
            field_unit=params["field_unit"],
            lockin_amplitude=float(params["lockin_amplitude"]),
            lockin_sensitivity=params["lockin_sensitivity"] or None,
            measure_time=float(params["measure_time"]),
            field_settle_s=float(params["field_settle_s"]),
            rf_settle_s=float(params["rf_settle_s"]),
            angle_settle_s=float(params["angle_settle_s"]),
            sample_interval=float(params["sample_interval"]),
            calibration_file=calibration_file,
            linear_volts_per_field_unit=linear,
            motor_steps_per_180_deg=float(params["motor_steps_per_180_deg"]),
            forward_direction=int(params["forward_direction"]),
            save_dir=self.save_dir_entry.get().strip() or "scratch",
            live_plot=False,
        )

    def run_measurement(self):
        if self.is_measuring:
            return
        try:
            params = self._get_parameters()
            real_hardware = any(
                entry.get().strip().upper() != "VIRTUAL"
                for entry in self.instrument_address_entries.values()
            )
            if real_hardware and not params["calibration_file"]:
                raise ValueError("Real hardware requires the measured calibration .xlsx")
            if real_hardware and not messagebox.askyesno(
                "Physical instrument safety",
                "Verify AO voltage limits, field calibration, RF power and stepper orientation. "
                "Continue with physical instrument outputs?",
                parent=self.root,
            ):
                return
            inst = self.connect_instruments()
            self.experiment = STFMR(
                daq=inst["daq"], lockin=inst["lockin"], rf_source=inst["rf_source"],
                arduino=inst["stepper_motor"], **params,
            )
        except Exception as error:
            print(f"Could not start ST-FMR: {error}")
            self.close_instruments()
            return
        self.worker_error = None
        self.paused = False
        self.is_measuring = True
        self.run_button.grid_remove()
        self.control_frame = ttk.Frame(self.right_panel, style="TFrame")
        self.control_frame.grid(row=1, column=0, pady=10)
        self.pause_button = ttk.Button(self.control_frame, text="PAUSE", command=self.toggle_pause)
        self.pause_button.pack(side="left", padx=5)
        self.stop_button = ttk.Button(self.control_frame, text="STOP", command=self.stop_measurement)
        self.stop_button.pack(side="left", padx=5)
        self.measurement_thread = threading.Thread(target=self._worker, daemon=True)
        self.measurement_thread.start()
        self.update_plot_loop()

    def _worker(self):
        try:
            self.experiment.run_experiment()
        except Exception as error:
            self.worker_error = error

    def toggle_pause(self):
        if self.is_measuring and self.experiment:
            self.paused = not self.paused
            self.experiment.pause_requested = self.paused
            self.pause_button.config(text="RESUME" if self.paused else "PAUSE")

    def stop_measurement(self):
        if self.is_measuring and self.experiment:
            self.experiment.abort_requested = True
            self.experiment.pause_requested = False
            self.stop_button.config(state="disabled")
            print("Stop requested; safe shutdown will be attempted.")

    def update_plot_loop(self):
        self.plot_data()
        if self.measurement_thread and self.measurement_thread.is_alive():
            self.root.after(1000, self.update_plot_loop)
            return
        self.is_measuring = False
        if hasattr(self, "control_frame") and self.control_frame.winfo_exists():
            self.control_frame.destroy()
        self.run_button.grid()
        self.plot_data()
        if self.worker_error is not None:
            print(f"ST-FMR measurement failed: {self.worker_error}")
        else:
            print("ST-FMR measurement stopped/completed. Check the saved CSV and PNG.")

    def plot_data(self, event=None):
        if self.experiment is None or not self.experiment.filename:
            return
        try:
            _, frame = load_stfmr_data(self.experiment.filename)
            if frame is None or frame.empty:
                return
            x, y = self.x_axis.get(), self.y_axis.get()
            if x not in frame or y not in frame:
                return
            self.ax.clear()
            self.ax.plot(frame[x], frame[y], "o-", markersize=3)
            self.ax.set_xlabel(x)
            self.ax.set_ylabel(y)
            self.ax.set_title("ST-FMR live acquisition")
            self.ax.grid(alpha=0.3)
            self.canvas.draw_idle()
        except (OSError, ValueError, KeyError):
            # Incremental writes may be partially visible while the worker saves.
            return

    def on_closing(self):
        if self.is_measuring and self.measurement_thread and self.measurement_thread.is_alive():
            self.stop_measurement()
            messagebox.showwarning(
                "Measurement active",
                "Stop requested. Close the window again after the worker has finished "
                "and outputs have been disabled.", parent=self.root,
            )
            return
        super().on_closing()


if __name__ == "__main__":
    root = tk.Tk()
    app = STFMRApp(root)
    root.mainloop()
