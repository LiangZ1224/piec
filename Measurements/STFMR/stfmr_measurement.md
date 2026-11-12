# ST-FMR Measurement Documentation

This document describes the spin-torque ferromagnetic resonance (ST-FMR) measurement ported from `STFMR_measurement.ipynb` into the PIEC measurement framework, following the existing `Measurements/AMR` layout.

## Overview

An ST-FMR experiment drives the sample with continuous-wave RF excitation and measures the lock-in voltage while stepping an applied magnetic field. The original notebook also supports RF frequency and power sweeps, sample rotation with an Arduino stepper, and combinations of these scans. **The source notebook does not contain a validated line-shape fitting model**, so the implementation saves and plots the measured lock-in X and Y rather than reporting unverified physical fit parameters.

## Hardware setup and original notebook mapping

| Role | Original notebook | PIEC-compatible interface | Units |
|---|---|---|---|
| Magnet supply voltage | `ekpy.control.instruments.digilent.v_out(0, 0, volts)` | `daq.write_AO(0, volts)` | V |
| Supply / sensor voltage readback | `digilent.v_in(0, 0)` | `daq.read_AI(0)` | V |
| Lock-in oscillator | `srs830.set_internal_amplitude`, `set_internal_frequency` | `lockin.set_amplitude`, `lockin.set_reference_frequency` | V, Hz |
| Lock-in measurement | `srs830.get_X_Y(lockin)` | `lockin.get_X_Y()` | typically V |
| CW RF source | VISA writes `:FREQ:CW`, `:POW:AMPL`, `:OUTP:STAT` | `rf_source.set_frequency`, `set_power`, `output` | Hz, dBm |
| Motor motion | `Arduino_Stepper.step(steps, direction)` | `arduino.step(steps, direction)` | motor steps |

The repository already includes `USB231` / `USB1208HS` DAQ, `SRS830` lock-in, and `Geos_Stepper` driver classes, plus virtual versions of the instrument categories. It includes a **virtual RF source but no verified model-specific Anritsu RF driver** in the branch inspected. A real source must be wrapped or implemented and verified using its programming manual in compliance with `adding_driver.md`. The notebook defaults to virtual mode.

### Magnetic field calibration

The original workbook `side_center_calibration.xlsx` contains the columns:

- `center field`: desired magnetic-field value in the calibration workbook's units
- `side field`: the DAQ output voltage (V) associated with that field

`FieldCalibration` sorts those rows, rejects duplicate/nonfinite values, performs bounded linear interpolation, and refuses **extrapolation**. This addresses the original notebook's inability to handle exact endpoints reliably. The field's physical unit is **not stated** in the notebook; confirm it from your workbook and supply `field_unit` before physical experiments.

For a **virtual demonstration only**, set `linear_volts_per_field_unit=1.0` in place of a workbook. It is not a calibrated physical field conversion. The optional `check_field_stability` compares consecutive DAQ analog input readings within a voltage tolerance, with a finite timeout. This is a *voltage stability check*, **not** a direct guarantee that the sample field equals the target setpoint.

### Stepper conversion

The notebook labels angles using `motor_steps * 180 / 380`. Thus the new `motor_steps_per_180_deg` defaults to `380`, and positive angle motions use direction bit `0`, reverse use `1`, to reproduce the notebook's conventions. PIEC's generic stepper may have a different steps-per-revolution setting; verify the physical motion and direction before applying a field or RF power. Angles in `STFMR` are **relative to the physical starting orientation**, which is not automatically homed.

## Software architecture

- **`src/piec/measurement/stfmr.py`** — measurement class `STFMR(MagnetoTransport)`, field-calibration helper, control sequence, data capture and analysis. It uses existing PIEC driver interfaces rather than embedding instrument transport commands.
- **`Measurements/STFMR/STFMR_testing.ipynb`** — setup, simulation and local hardware checklist, field, frequency/power, angle and combined scans, then data reading and visualization.
- **`Measurements/STFMR/stfmr_GUI.py`** — `MeasurementApp`-based GUI, parameter inputs, virtual/physical connection selectors, pause/stop, and live CSV plotting.
- **`Measurements/STFMR/stfmr_measurement.md`** — this document, matching `Measurements/AMR/amr_measurement.md`.

The two hooks named by `adding_measurement.rst` exist:

- `configure_awg(self)` — configures the **CW RF source** instead of an AWG because the source notebook has no AWG. It does *not* turn RF output on.
- `analyze(self)` — saves a PNG of raw averaged X/Y traces after CSV acquisition. No physical line-shape fitting is assumed.

The actual `MagnetoTransport` parent on `master` calls `configure_lockin` and `capture_data` from `run_experiment`, *not* `configure_awg`. This class overrides `run_experiment` to call `configure_awg` as part of its safe RF workflow, and then uses the normal metadata, CSV, plot and history helpers.

## Scan modes

All experiments are expressed as a Cartesian grid. Any axis can have one or several entries:

- **Field sweep**: multiple `fields`, fixed `rf_frequencies_hz`, `rf_powers_dbm`, `lockin_frequencies_hz`, and `angles_deg=(0,)`.
- **RF frequency sweep**: one field, multiple RF frequencies.
- **RF power sweep**: one field, multiple RF powers.
- **Angle-dependent field sweep**: multiple fields and angles; DAQ AO is set to 0 V before any motor motion.
- **Combined sweep**: multiple RF frequencies, powers, lock-in frequencies, angles and fields. The field axis is the fastest-varying coordinate.

The original positive/negative field passes can be run as **two separate** measurements (`fields=np.linspace(0.7, 0, 50, endpoint=False)` and `np.linspace(-0.7, 0, 50, endpoint=False)`); this avoids conflating forward and reverse branches in a single dataset.

## Notebook quick start

1. Install PIEC from the checked-out repository using `pip install -e .` and use the provided ST-FMR notebook from a writable local working directory.
2. Start with all virtual instruments. For a demo, use `VirtualDaq`, `VirtualLockin`, `VirtualRFSource` and `VirtualStepper`, with `linear_volts_per_field_unit=1.0` (simulation only).
3. Define field, frequency, power, lock-in frequency and angle arrays, as well as averaging and settling times.
4. Construct `STFMR(daq=..., lockin=..., rf_source=..., arduino=..., ...)`.
5. Explicitly run `experiment.run_experiment()` in the dedicated execution cell; merely importing the notebook does not energize outputs.
6. Load results with `from piec.measurement.stfmr import load_stfmr_data` or the existing `standard_csv_to_metadata_and_data` function, and inspect the saved CSV and PNG.

On physical instruments, do not run until the calibration workbook, analog output limits, the RF source's actual command language, the motor motion ratio, and your sample's allowable current/power are verified. The notebook's old Anritsu SCPI commands are evidence of a previous setup, **not a verified driver manual**.

## GUI usage

Run from the repository root:

```bash
python Measurements/STFMR/stfmr_GUI.py
```

By default, all instrument selectors are `VIRTUAL`. Enter scan axes as comma-separated lists (RF frequencies in **GHz** in the GUI, converted to Hz for the backend); enter the magnetic field start/end and number of points. Select the output directory, then Run. GUI controls are Pause/Resume and Stop; the CSV is updated as each point completes, and the graph can display X or Y against field, frequency, power or angle. Physical mode requires a calibration workbook and verified physical drivers, including a real RF source. The GUI does not automatically identify MCC DAQ board numbers on the VISA bus.

## Output files and analysis

PIEC's `create_measurement_filename` generates indexed `stfmr_*.csv` names. `metadata_and_data_to_csv` stores a one-row metadata DataFrame, a separator row, and tabular measurements. Column names are:

`field`, `angle_deg`, `rf_frequency_hz`, `rf_power_dbm`, `lockin_frequency_hz`, `field_voltage_v`, `X`, `Y`, `timestamp`.

Fields use the calibration workbook's units; raw lock-in values use the instrument-reported units (normally volts). The class writes results **after each point** and attempts to shut off magnet analog output and RF output in a `finally` block even when interrupted or when acquisition raises an exception. It does not rewind or home the motor automatically. If hardware refuses an off command, a `RuntimeError` is raised; check the instruments immediately.

The PNG traces are plots of measured X and Y. **No Lorentzian fitting or spin-torque efficiency calculation is implied by these plots.**

## Contribution and validation

The guidance in `docs/source/contributing/` contains `index.rst`, `adding_driver.md`, and `adding_measurement.rst`. `index.rst` is the contribution/test overview; the other two are specific implementation guides. For this measurement-only contribution, no existing driver category or physical driver is changed. Driver contributions are a separate task.

Run from the repository root:

```bash
python -m compileall src/piec/measurement/stfmr.py Measurements/STFMR/stfmr_GUI.py
python -m pytest tests/test_measurement_pipeline.py -v
python -m pytest tests/ -v
```

For full Sphinx documentation integration, add a matching `docs/source/measurements/stfmr.rst` page and include `measurements/stfmr` in the `Measurements` toctree of `docs/source/index.rst`. Those changes are distinct from the four AMR-pattern files requested here.

**Verification boundary:** Static syntax and isolated fake-driver checks are possible without hardware. They do not prove physical Anritsu SCPI compatibility, actual magnet field calibration, or motor orientation. Physical bench verification remains required.
