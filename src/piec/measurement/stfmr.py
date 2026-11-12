"""Spin-torque ferromagnetic resonance (ST-FMR) acquisition for PIEC.

This module orchestrates existing DAQ, lock-in, RF-source and stepper drivers;
it does not implement a new physical instrument driver. Magnetic-field setpoints
use the units of the supplied calibration workbook. In particular, their units
are NOT assumed to be Oe or T.

The experiment uses the PIEC MagnetoTransport/Experiment metadata and CSV
pipeline but overrides its two-instrument linear magnet-control implementation:
the original ST-FMR setup uses one DAQ for analog output and input and an
empirically calibrated, potentially nonlinear field-to-voltage relationship.
"""

from __future__ import annotations

from itertools import product
from pathlib import Path
import math
import time
import warnings

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from piec.measurement.magneto_transport import MagnetoTransport
from piec.analysis.utilities import (
    create_measurement_filename,
    metadata_and_data_to_csv,
    standard_csv_to_metadata_and_data,
)


class FieldCalibration:
    """Interpolate output voltage (V) from a calibrated field setpoint.

    Parameters
    ----------
    workbook : str or Path
        Excel workbook with the original notebook's ``center field`` (magnetic
        field, **workbook units**) and ``side field`` (DAQ output, **V**) columns.

    Notes
    -----
    The original notebook does not specify its field unit. Calibration
    interpolation is bounded and does not silently extrapolate.
    """

    def __init__(self, workbook):
        self.workbook = str(workbook)
        table = pd.read_excel(workbook)
        required = ["center field", "side field"]
        if not set(required).issubset(table.columns):
            raise ValueError(f"Calibration file must contain columns: {required}")
        table = table[required].dropna().apply(pd.to_numeric, errors="raise")
        table = table.sort_values("center field")
        self.fields = table["center field"].to_numpy(dtype=float)
        self.voltages = table["side field"].to_numpy(dtype=float)
        if (len(self.fields) < 2 or np.any(np.diff(self.fields) <= 0)
                or not np.isfinite(self.fields).all()
                or not np.isfinite(self.voltages).all()):
            raise ValueError("Calibration needs two or more unique, finite field values")

    def voltage_for_field(self, field):
        """Return interpolated DAQ output voltage (V), rejecting extrapolation."""
        value = float(field)
        if not math.isfinite(value) or not self.fields[0] <= value <= self.fields[-1]:
            raise ValueError(
                f"Field {value} is outside calibrated interval "
                f"[{self.fields[0]}, {self.fields[-1]}]"
            )
        return float(np.interp(value, self.fields, self.voltages))


class STFMR(MagnetoTransport):
    """Acquire ST-FMR lock-in X/Y vs field, angle, RF frequency and power.

    The outer Cartesian grid is ``RF frequency × RF power × lock-in frequency ×
    sample angle``; field is the fastest varying coordinate. Use singleton arrays
    to perform field-only, fixed-field RF-power/frequency, angular, or combined
    sweeps. All quantities are independently recorded, even for singleton axes.

    Parameters
    ----------
    daq : piec DAQ driver
        Must implement ``write_AO(channel, volts)``, ``read_AI(channel)`` when
        readback is enabled, and ``idn()``. The old notebook used Digilent board
        0, AO channel 0 and AI channel 0.
    lockin : piec lock-in driver
        Must implement ``set_amplitude``, ``set_reference_frequency`` and
        ``get_X_Y`` (or ``quick_read``). SRS830 is supported by PIEC.
    rf_source : piec RFSource-compatible driver
        Must implement ``set_frequency(Hz)``, ``set_power(dBm)``,
        ``output(channel, on=bool)`` and ``idn()``. The repository currently
        provides a virtual RF source; physical Anritsu commands must be verified
        and implemented as a separate driver before hardware use.
    arduino : piec stepper driver, optional
        Needed only if more than one angle, or a nonzero angle, is requested.
    fields : sequence of float
        Magnetic-field setpoints in the calibration workbook's units. Preserve
        sweep order (e.g. ``np.linspace(0.7, 0, 50, endpoint=False)``).
    rf_frequencies_hz, rf_powers_dbm, lockin_frequencies_hz, angles_deg : sequences
        Scan axes, in Hz, dBm, Hz and degrees, respectively.
    calibration_file : str or Path, optional
        Excel workbook as above. Required for physical runs unless a separately
        verified linear coefficient is explicitly supplied.
    linear_volts_per_field_unit : float, optional
        An explicit field-to-output conversion for a verified linear setup, or
        **virtual demonstration only**. No unverified default is supplied.
    motor_steps_per_180_deg : float
        Default 380, taken from the *original notebook*, not the PIEC motor's
        generic 200-steps-per-revolution assumption. Bench-verify this ratio.
    forward_direction : int
        Stepper direction bit for positive angular change; original notebook
        used 0 for forward and 1 for reverse.
    """

    mtype = "stfmr"
    _instruments = ("daq", "lockin", "rf_source")
    COLUMNS = [
        "field", "angle_deg", "rf_frequency_hz", "rf_power_dbm",
        "lockin_frequency_hz", "field_voltage_v", "X", "Y", "timestamp",
    ]

    def __init__(
        self, *, daq, lockin, rf_source, arduino=None,
        fields=(0.0,), rf_frequencies_hz=(3e9,), rf_powers_dbm=(10.0,),
        lockin_frequencies_hz=(17.37,), angles_deg=(0.0,),
        calibration_file=None, linear_volts_per_field_unit=None,
        field_unit="calibration units", lockin_amplitude=0.4,
        lockin_sensitivity=None, measure_time=3.0, sample_interval=0.1,
        field_settle_s=1.0, rf_settle_s=0.0, angle_settle_s=1.0,
        check_field_stability=False, stability_tolerance_v=0.002,
        stability_timeout_s=30.0, ao_channel=0, ai_channel=0,
        rf_channel=1, motor_steps_per_180_deg=380.0, forward_direction=0,
        save_dir="scratch", live_plot=False,
    ):
        self.daq = daq
        self.rf_source = rf_source
        self.arduino = arduino
        self.fields = self._axis(fields, "fields")
        self.rf_frequencies_hz = self._axis(rf_frequencies_hz, "rf_frequencies_hz")
        self.rf_powers_dbm = self._axis(rf_powers_dbm, "rf_powers_dbm")
        self.lockin_frequencies_hz = self._axis(lockin_frequencies_hz, "lockin_frequencies_hz")
        self.angles_deg = self._axis(angles_deg, "angles_deg")
        if min(self.rf_frequencies_hz) <= 0 or min(self.lockin_frequencies_hz) <= 0:
            raise ValueError("RF and lock-in frequencies must be positive Hz")
        if not (0 <= min(self.angles_deg) and max(self.angles_deg) <= 360):
            raise ValueError("angles_deg must be within 0–360°, relative to the starting position")
        if (len(self.angles_deg) > 1 or self.angles_deg[0] != 0) and arduino is None:
            raise ValueError("A stepper (arduino) is required for nontrivial angle sweeps")
        if calibration_file is None and linear_volts_per_field_unit is None:
            raise ValueError("Supply a measured calibration_file, or explicit linear_volts_per_field_unit")
        if calibration_file is not None and linear_volts_per_field_unit is not None:
            raise ValueError("Use either calibration_file or linear_volts_per_field_unit, not both")
        self.calibration_file = str(calibration_file) if calibration_file is not None else None
        self._calibration = FieldCalibration(calibration_file) if calibration_file is not None else None
        self.linear_volts_per_field_unit = (
            float(linear_volts_per_field_unit) if linear_volts_per_field_unit is not None else None
        )
        if self.linear_volts_per_field_unit is not None and (
            not math.isfinite(self.linear_volts_per_field_unit)
            or self.linear_volts_per_field_unit == 0
        ):
            raise ValueError("linear_volts_per_field_unit must be finite and nonzero")
        self.field_unit = str(field_unit)
        self.lockin_amplitude = float(lockin_amplitude)
        self.lockin_sensitivity = lockin_sensitivity
        self.measure_time = float(measure_time)
        self.sample_interval = float(sample_interval)
        self.field_settle_s = float(field_settle_s)
        self.rf_settle_s = float(rf_settle_s)
        self.angle_settle_s = float(angle_settle_s)
        self.check_field_stability = bool(check_field_stability)
        self.stability_tolerance_v = float(stability_tolerance_v)
        self.stability_timeout_s = float(stability_timeout_s)
        self.ao_channel = int(ao_channel)
        self.ai_channel = int(ai_channel)
        self.rf_channel = int(rf_channel)
        self.motor_steps_per_180_deg = float(motor_steps_per_180_deg)
        self.forward_direction = int(forward_direction)
        self._motor_offset_steps = 0
        self._last_plot_path = None
        self._active = False
        self._completed = False
        for key in ("measure_time", "sample_interval", "field_settle_s", "rf_settle_s", "angle_settle_s"):
            if not math.isfinite(getattr(self, key)) or getattr(self, key) < 0:
                raise ValueError(f"{key} must be finite and nonnegative")
        if self.sample_interval <= 0 or self.stability_timeout_s <= 0 or self.stability_tolerance_v < 0:
            raise ValueError("Invalid sampling/stability settings")
        if self.motor_steps_per_180_deg <= 0 or self.forward_direction not in (0, 1):
            raise ValueError("Invalid motor calibration or direction")
        if self.lockin_amplitude < 0:
            raise ValueError("Lock-in amplitude must be nonnegative")
        self._current_rf = None
        self._current_li = None
        super().__init__(
            dmm=daq, calibrator=daq, arduino=arduino, lockin=lockin,
            field=self.fields[0], save_dir=str(save_dir),
            live_plot=live_plot, plot_config={"x": "field", "y": "X"},
        )
        self.filename = create_measurement_filename(
            self.save_dir, self.mtype,
            f"{len(self.fields)}field_{len(self.rf_frequencies_hz)}freq_{len(self.angles_deg)}angle",
        )
        self.data = pd.DataFrame(columns=self.COLUMNS)
        # Reject all unsafe/out-of-calibration field values before RF can be enabled.
        for requested_field in self.fields:
            self._check_ao_voltage(self._field_voltage(requested_field))

    @staticmethod
    def _axis(values, name):
        """Coerce an input scan axis to a nonempty tuple of finite floats."""
        try:
            result = tuple(float(value) for value in values)
        except (ValueError, TypeError) as error:
            raise ValueError(f"{name} must be a sequence of numbers") from error
        if not result or not all(math.isfinite(value) for value in result):
            raise ValueError(f"{name} must contain at least one finite value")
        return result

    def _update_metadata(self):
        """Use PIEC metadata but avoid storing legacy DAQ aliases as instruments."""
        super()._update_metadata()
        self.metadata.drop(columns=["dmm", "calibrator", "arduino"], inplace=True, errors="ignore")
        if self.arduino is not None:
            self.metadata["stepper"] = self.arduino.idn()
        self.metadata["motor_offset_steps"] = self._motor_offset_steps
        self.metadata["status"] = "completed" if self._completed else "in_progress"

    def initialize(self):
        """Verify instrument communication without resetting or energizing outputs."""
        for name in self._instruments:
            getattr(self, name).idn()
        if self.arduino is not None:
            self.arduino.idn()

    def configure_awg(self):
        """Guide-compatible setup hook: configure CW RF instead of an AWG.

        The original ST-FMR experiment has no arbitrary waveform generator;
        the RF source generates continuous-wave excitation.
        """
        self.rf_source.set_frequency(self.rf_frequencies_hz[0])
        self.rf_source.set_power(self.rf_powers_dbm[0])
        self._current_rf = (self.rf_frequencies_hz[0], self.rf_powers_dbm[0])

    def configure_lockin(self):
        """Configure SR830-compatible lock-in oscillator and optional sensitivity."""
        self.lockin.set_amplitude(self.lockin_amplitude)
        self.lockin.set_reference_frequency(self.lockin_frequencies_hz[0])
        if self.lockin_sensitivity is not None:
            self.lockin.set_sensitivity(self.lockin_sensitivity)
        self._current_li = self.lockin_frequencies_hz[0]

    def _field_voltage(self, field):
        if self._calibration is not None:
            return self._calibration.voltage_for_field(field)
        return float(field) * self.linear_volts_per_field_unit

    def _check_ao_voltage(self, volts):
        # Validate model-advertised range without guessing a DAQ hardware limit.
        ranges = getattr(self.daq, "ao_range", None)
        if ranges:
            if not any(lo <= volts <= hi for lo, hi in ranges if lo is not None and hi is not None):
                raise ValueError(f"Required {volts:g} V is outside DAQ AO range {ranges}")

    def _wait(self, seconds):
        """Sleep in short increments to honor GUI stop/pause requests."""
        remaining = float(seconds)
        while remaining > 0:
            if not self._check_controls():
                return
            interval = min(0.1, remaining)
            t0 = time.monotonic()
            time.sleep(interval)
            remaining -= time.monotonic() - t0

    def _check_controls(self):
        while self.pause_requested and not self.abort_requested:
            time.sleep(0.1)
        return not self.abort_requested

    def set_field(self, field=None):
        """Output calibrated field voltage and optionally check readback stability.

        ``read_AI`` is an *analog-voltage stability* check, not a proof that the
        magnetic field equals the requested setpoint; the original notebook's
        input-channel calibration was not supplied.
        """
        target = self.field if field is None else float(field)
        voltage = self._field_voltage(target)
        self._check_ao_voltage(voltage)
        self.daq.write_AO(self.ao_channel, voltage)
        self.field = target
        self._wait(self.field_settle_s)
        if not self.check_field_stability or self.abort_requested:
            return voltage
        start = time.monotonic()
        old = None
        stable_count = 0
        while time.monotonic() - start < self.stability_timeout_s:
            if not self._check_controls():
                return voltage
            current = float(self.daq.read_AI(self.ai_channel))
            if not math.isfinite(current):
                raise ValueError("DAQ readback is nonfinite")
            stable_count = (stable_count + 1) if (
                old is not None and abs(current - old) <= self.stability_tolerance_v
            ) else 0
            if stable_count >= 2:
                return voltage
            old = current
            self._wait(max(0.05, self.sample_interval))
        raise TimeoutError("DAQ analog readback did not stabilize before timeout")

    def _move_to_angle(self, angle):
        target_steps = int(round(float(angle) * self.motor_steps_per_180_deg / 180.0))
        delta = target_steps - self._motor_offset_steps
        if delta:
            if self.arduino is None:
                raise RuntimeError("An angle sweep requires a stepper")
            direction = self.forward_direction if delta > 0 else (1 - self.forward_direction)
            self.arduino.step(abs(delta), direction)
            self._motor_offset_steps = target_steps
            self._wait(self.angle_settle_s)

    def capture_data_point(self):
        """Average both lock-in X and Y (instrument-reported voltage units)."""
        xs, ys = [], []
        start = time.monotonic()
        while True:
            if not self._check_controls():
                break
            x, y = self.lockin.get_X_Y()
            xs.append(float(x))
            ys.append(float(y))
            if time.monotonic() - start >= self.measure_time:
                break
            self._wait(min(self.sample_interval, max(0, self.measure_time - (time.monotonic() - start))))
        if not xs:
            return None
        return float(np.mean(xs)), float(np.mean(ys))

    def save_data_point(self):
        """Persist metadata and all recorded rows in the standard PIEC CSV layout."""
        self._update_metadata()
        metadata_and_data_to_csv(self.metadata, self.data, self.filename)

    def capture_data(self):
        """Run the configured Cartesian parameter grid, saving after each point."""
        for rf_frequency, power, li_frequency, angle in product(
            self.rf_frequencies_hz, self.rf_powers_dbm,
            self.lockin_frequencies_hz, self.angles_deg,
        ):
            if not self._check_controls():
                break
            if self._current_rf != (rf_frequency, power):
                self.rf_source.set_frequency(rf_frequency)
                self.rf_source.set_power(power)
                self._current_rf = (rf_frequency, power)
                self._wait(self.rf_settle_s)
            if self._current_li != li_frequency:
                self.lockin.set_reference_frequency(li_frequency)
                self._current_li = li_frequency
            if self._motor_offset_steps != int(round(angle * self.motor_steps_per_180_deg / 180.0)):
                # Remove the magnetic drive before any mechanical rotation.
                self.daq.write_AO(self.ao_channel, 0.0)
                self._move_to_angle(angle)
            for field in self.fields:
                if not self._check_controls():
                    break
                voltage = self.set_field(field)
                if self.abort_requested:
                    break
                xy = self.capture_data_point()
                if xy is None:
                    break
                row = {
                    "field": field,
                    "angle_deg": angle,
                    "rf_frequency_hz": rf_frequency,
                    "rf_power_dbm": power,
                    "lockin_frequency_hz": li_frequency,
                    "field_voltage_v": voltage,
                    "X": xy[0], "Y": xy[1], "timestamp": time.time(),
                }
                self.data.loc[len(self.data)] = row
                self.save_data_point()
                self._update_live_plot()
            if self.abort_requested:
                break

    def analyze(self):
        """Plot captured X/Y traces and save a PNG next to the standard CSV.

        No Lorentzian line-shape model is imposed: the uploaded notebook only
        averaged lock-in signals, and a validated fitting model was not given.

        Returns
        -------
        str or None
            Absolute or relative path to PNG, or ``None`` if no rows exist.
        """
        if self.data is None or self.data.empty:
            return None
        varying = [column for column in (
            "field", "rf_frequency_hz", "rf_power_dbm", "lockin_frequency_hz", "angle_deg"
        ) if self.data[column].nunique() > 1]
        x_col = "field" if "field" in varying else (varying[0] if varying else "field")
        grouping = [col for col in (
            "rf_frequency_hz", "rf_power_dbm", "lockin_frequency_hz", "angle_deg", "field"
        ) if col != x_col and self.data[col].nunique() > 1]
        fig, axes = plt.subplots(2, 1, figsize=(9, 7), sharex=True)
        groups = [((), self.data)] if not grouping else list(self.data.groupby(grouping, sort=False))
        for key, group in groups[:20]:
            label = ", ".join(f"{k}={v:g}" for k, v in zip(grouping, key if isinstance(key, tuple) else (key,)))
            for ax, channel in zip(axes, ("X", "Y")):
                ax.plot(group[x_col], group[channel], "o-", markersize=3, label=label if label else None)
                ax.set_ylabel(f"Lock-in {channel} (V)")
                ax.grid(alpha=0.3)
        axes[1].set_xlabel(f"{x_col} ({self.field_unit if x_col == 'field' else 'Hz' if 'frequency' in x_col else 'dBm' if x_col == 'rf_power_dbm' else 'deg'})")
        if len(groups) > 1:
            axes[0].legend(fontsize=7, ncol=2)
        if len(groups) > 20:
            axes[0].set_title(f"ST-FMR (first 20 of {len(groups)} traces)")
        else:
            axes[0].set_title("ST-FMR")
        fig.tight_layout()
        path = str(Path(self.filename).with_suffix(".png"))
        fig.savefig(path, dpi=170)
        plt.close(fig)
        self._last_plot_path = path
        return path

    def shut_off(self):
        """Attempt *both* AO=0 V and RF off; report failures after attempting both."""
        errors = []
        for description, operation in (
            ("DAQ AO zero", lambda: self.daq.write_AO(self.ao_channel, 0.0)),
            ("RF output off", lambda: self.rf_source.output(self.rf_channel, on=False)),
        ):
            try:
                operation()
            except Exception as exc:
                errors.append(f"{description}: {exc}")
        self._active = False
        if errors:
            raise RuntimeError("Safety shutdown failed: " + "; ".join(errors))

    def safe_close(self):
        """Safely disable energized outputs (does not reset motor position)."""
        self.shut_off()

    def run_experiment(self, configure_lockin=True):
        """Connect, configure, capture, save, analyze, and switch outputs off.

        Returns
        -------
        str
            PIEC-style CSV data path. A PNG is written next to the CSV.

        Notes
        -----
        Physical output must never be enabled before checking calibration,
        connections, RF power limits and the motor's angular conversion.
        """
        self._completed = False
        try:
            self.initialize()
            self.configure_awg()
            if configure_lockin:
                self.configure_lockin()
            else:
                self._current_li = None
            self._init_live_plot()
            self._active = True
            self.rf_source.output(self.rf_channel, on=True)
            self._wait(self.rf_settle_s)
            if not self.abort_requested:
                self.capture_data()
            self._completed = not self.abort_requested
            if not self.data.empty:
                self.save_data_point()
        finally:
            # Even connection/configuration errors must attempt BOTH off commands.
            import sys
            had_exception = sys.exc_info()[0] is not None
            try:
                self.shut_off()
            except Exception as error:
                if had_exception:
                    warnings.warn(f"Shutdown also failed: {error}", RuntimeWarning)
                else:
                    raise
            finally:
                self._close_live_plot()
        self.analyze()
        self._update_history()
        return self.filename


def load_stfmr_data(filename):
    """Load a standard PIEC ST-FMR CSV as (metadata DataFrame, data DataFrame)."""
    return standard_csv_to_metadata_and_data(filename)


__all__ = ["FieldCalibration", "STFMR", "load_stfmr_data"]
