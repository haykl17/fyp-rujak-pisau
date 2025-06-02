import os
import json
import platform
import subprocess
import time
import numpy as np
import cv2

# Use mock GPIO when not on Raspberry Pi
try:
    import RPi.GPIO as GPIO
except ImportError:
    from unittest import mock
    GPIO = mock.MagicMock()

import BlynkLib
import threading

from kivy.app import App
from kivy.core.window import Window
from kivy.clock import Clock
from kivy.graphics import Color, Rectangle, Line
from kivy.graphics.texture import Texture
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.slider import Slider
from kivy.uix.screenmanager import ScreenManager, Screen
from kivy.uix.image import Image
from kivy.uix.scrollview import ScrollView

# ───────────────────────────────────────────────────────────────
# HARDCODE YOUR BLYNK AUTH TOKEN HERE (no environment needed)
# ───────────────────────────────────────────────────────────────
blynk = BlynkLib.Blynk('eA9DH2EjNhHTJFCjN6NzHxvrlIs4Xgwj')

# ──────────────────────────────────────────────────────────────────────────────
# CONSTANTS & GPIO PINS
# ──────────────────────────────────────────────────────────────────────────────
SETTINGS_FILE = "motion_settings.json"
FRAME_WIDTH = 640
FRAME_HEIGHT = 480
CAMERA_FOURCC = cv2.VideoWriter_fourcc(*'MJPG')
MIN_HAND_AREA = 3000
MIN_MOTION_AREA = 1000
SKIN_LOWER_HSV = np.array([0, 20, 70], dtype=np.uint8)
SKIN_UPPER_HSV = np.array([20, 255, 255], dtype=np.uint8)

# GPIO (BCM) pins:
RPWM = 18          # PWM output for motor speed (1 kHz)
REN = 23           # Enable line for motor driver (HIGH=enabled, LOW=disabled)
MOTOR_SWITCH = 17  # Physical on/off lever switch for motor
PI_SWITCH = 27     # Physical lever switch to request Pi shutdown
BUZZER_PIN = 24    # Optional buzzer pin for audible alarms (if hardware added)

MAX_RPM = 2750        # 100% PWM → 2750 simulated RPM
BASELINE_TEMP = 27.0  # Malaysia average ambient temperature
BOTTOM_ROW_HEIGHT = 40  # Height of bottom row in UI

# Path to event log
EVENT_LOG = "pisauevents.log"
# Path to runtime counter
RUNTIME_FILE = "run_hours.json"
# ──────────────────────────────────────────────────────────────────────────────


class MotorSimulator:
    """
    Simulates a motor’s RPM and temperature, but drives real hardware on GPIO 18 and 23:
      - RPWM (GPIO 18) as 1 kHz PWM for speed control
      - REN (GPIO 23) as enable line
    """

    def __init__(self):
        # Internal “set” state that UI requests
        self.set_speed_percent = 0     # percent speed requested by UI
        self.set_rpm = 0
        self.current_rpm = 0
        self.temperature = BASELINE_TEMP

        # For “Dig Toggle” logic:
        self._last_nonzero_speed = 0   # remembers last nonzero speed
        self.digitally_toggled_off = False

        # For Emergency/Post‐Emergency:
        self.in_emergency = False
        self.post_emergency = False

        # For gradual slow‐down if high temperature:
        self.original_speed_percent = 0
        self.slow_down = False

        # Initialize GPIO for motor driver
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(RPWM, GPIO.OUT)
        self.pwm = GPIO.PWM(RPWM, 1000)  # 1 kHz
        self.pwm.start(0)                # Start at 0% duty
        GPIO.setup(REN, GPIO.OUT)
        GPIO.output(REN, GPIO.HIGH)
        self.enabled = True

        # Optional buzzer pin (for audible alarms)
        GPIO.setup(BUZZER_PIN, GPIO.OUT)
        GPIO.output(BUZZER_PIN, GPIO.LOW)
        self.alarm_on = False

    def _sound_alarm(self, on: bool):
        """
        Turn buzzer on/off. If no hardware, no effect.
        """
        GPIO.output(BUZZER_PIN, GPIO.HIGH if on else GPIO.LOW)
        self.alarm_on = on

    def set_speed(self, percent):
        """
        Called by the UI whenever we want to change speed. This will:
          1) Check if allowed to run (e.g. motor switch ON, not in post-emergency)
          2) Update self.set_speed_percent
          3) Drive the hardware PWM line if permitted
        """
        percent = max(0, min(100, percent))
        self.set_speed_percent = percent

        # Remember last nonzero speed for Dig Toggle
        if percent > 0:
            self._last_nonzero_speed = percent

        # Only actually drive PWM if:
        #  - driver enabled
        #  - not in emergency
        #  - not in post-emergency
        if self.enabled and not self.in_emergency and not self.post_emergency:
            self.pwm.ChangeDutyCycle(percent)
        else:
            self.pwm.ChangeDutyCycle(0)

    def enable_motor(self):
        """
        Called when physical Motor switch is ON (GPIO 17). Re-enable REN and
        restore requested PWM if not in any emergency.
        """
        if not self.enabled:
            GPIO.output(REN, GPIO.HIGH)
            self.enabled = True
        if not self.in_emergency and not self.post_emergency:
            self.pwm.ChangeDutyCycle(self.set_speed_percent)

    def disable_motor(self):
        """
        Called by Emergency Stop or physical switch OFF. Force PWM = 0, REN = LOW.
        """
        if self.enabled:
            self.pwm.ChangeDutyCycle(0)
            GPIO.output(REN, GPIO.LOW)
            self.enabled = False

    def toggle_digital(self):
        """
        “Dig Toggle” button: if currently running at > 0%, store that speed, then force 0%.
        If currently at 0% (and driver enabled, no emergency), restore last nonzero.
        """
        if self.set_speed_percent > 0 and not self.in_emergency and not self.post_emergency:
            self._last_nonzero_speed = self.set_speed_percent
            self.set_speed(0)
            self.digitally_toggled_off = True
        else:
            if self.enabled and not self.in_emergency and not self.post_emergency:
                restored = self._last_nonzero_speed or 30
                self.set_speed(restored)
            self.digitally_toggled_off = False

    def enter_emergency(self):
        """
        Called when Emergency Stop is pressed. Immediately disable motor and set in_emergency.
        """
        self.in_emergency = True
        self.post_emergency = False
        self.disable_motor()
        self._sound_alarm(True)  # optional audible alarm

    def exit_emergency(self):
        """
        Called after user taps post-2s. Enter Post-Emergency: still disabled until switch cycle.
        """
        self.in_emergency = False
        self.post_emergency = True
        self.disable_motor()
        self._sound_alarm(False)

    def complete_post_emergency(self):
        """
        Called when physical Motor switch cycles OFF→ON in post-emergency. Return to normal.
        """
        self.post_emergency = False
        if self.enabled:
            restored = self._last_nonzero_speed or 30
            self.set_speed(restored)

    def update(self, dt):
        """
        Called every 0.5 s to update simulated RPM & temperature, including slow-down logic.

        We’ve increased the ramp rate to 0.3 (instead of 0.1)
        and bumped the cap from 50 RPM/step up to 100 RPM/step.
        """
        # 1) Compute target RPM (0–2750)
        self.set_rpm = int(self.set_speed_percent * MAX_RPM / 100)

        # 2) Ramp current_rpm toward set_rpm more aggressively
        if self.current_rpm < self.set_rpm:
            delta = self.set_rpm - self.current_rpm
            # Use 30% of the error, capped at 100 RPM per step
            self.current_rpm += min(int(delta * 0.3), 100)
        elif self.current_rpm > self.set_rpm:
            delta = self.current_rpm - self.set_rpm
            self.current_rpm -= min(int(delta * 0.3), 100)

        # 3) Temperature update
        if self.set_speed_percent > 55:
            temp_increase_rate = 0.15 + (self.set_speed_percent / 100) * 0.2
        elif self.set_speed_percent < 40:
            temp_increase_rate = 0.01
        else:
            temp_increase_rate = 0.05
        self.temperature += temp_increase_rate

        # 4) Cooling logic
        if self.set_speed_percent <= 20:
            self.temperature -= 0.03
        elif self.set_speed_percent == 0:
            self.temperature -= 0.07
        else:
            if self.temperature > BASELINE_TEMP + 5:
                self.temperature = min(self.temperature, BASELINE_TEMP + 70)

        # 5) Clamp temperature
        if self.temperature < 0:
            self.temperature = 0.0
        if self.temperature > 70.0:
            self.temperature = 70.0

        # 6) Gradual slow-down if temperature ≥ 60 °C
        if self.temperature >= 60.0 and not self.slow_down:
            self.original_speed_percent = self.set_speed_percent
            self.slow_down = True
            log_event("TEMP_HIGH", f"{self.temperature:.1f}")

        if self.slow_down:
            if self.temperature >= 60.0:
                if self.set_speed_percent > 20:
                    new_speed = self.set_speed_percent - 1
                    if new_speed < 20:
                        new_speed = 20
                    self.set_speed(new_speed)
            else:
                self.slow_down = False

        # 7) Slight fluctuation when motor off & temp near baseline
        if (not self.slow_down
                and self.set_speed_percent == 0
                and abs(self.temperature - BASELINE_TEMP) < 0.01):
            self.temperature = BASELINE_TEMP + np.random.uniform(-1, 1)


def log_event(event_type: str, value: str = ""):
    """
    Append a timestamped event to the EVENT_LOG file.
    Format: YYYY-MM-DD HH:MM:SS | EVENT_TYPE | value
    """
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} | {event_type}"
    if value:
        line += f" | {value}"
    with open(EVENT_LOG, "a") as f:
        f.write(line + "\n")


def load_run_hours() -> float:
    """
    Load the stored run_hours from disk. Returns 0.0 if file missing or invalid.
    """
    try:
        with open(RUNTIME_FILE, "r") as f:
            data = json.load(f)
            return float(data.get("run_hours", 0.0))
    except Exception:
        return 0.0


def save_run_hours(hours: float):
    """
    Save the run_hours to disk (as a JSON file).
    """
    with open(RUNTIME_FILE, "w") as f:
        json.dump({"run_hours": hours}, f)


# ──────────────────────────────────────────────────────────────────────────────
# BLYNK HANDLERS (incoming commands from phone)
# ──────────────────────────────────────────────────────────────────────────────

@blynk.on("V5")
def handle_speed_slider(value):
    """
    V5 is the speed slider (0–100).  When Blynk sends a new value, we:
      1. Check physical Motor Switch
      2. Check not in emergency/post-emergency
      3. Directly set motor.set_speed(percent), skipping hold-to-start
    """
    if not value or len(value) == 0:
        return

    percent = int(value[0])
    app = App.get_running_app()
    live = app.root.get_screen("live")

    # Check physical Motor Switch state:
    if platform.system() == "Windows":
        motor_switch_on = not live.fake_motor_switch
    else:
        motor_switch_on = (GPIO.input(MOTOR_SWITCH) == GPIO.LOW)

    # If switch is OFF, show popup and do nothing:
    if not motor_switch_on:
        Clock.schedule_once(lambda dt: live.show_popup("Please Turn On The Motor Switch"))
        return

    # If emergency or post‐emergency, ignore:
    if live.motor.in_emergency or live.motor.post_emergency:
        return

    # DIRECTLY set speed on the motor simulator (bypass start_confirmed):
    live.motor.set_speed(percent)


@blynk.on("V4")
def handle_dig_toggle(value):
    """
    V4 is the Dig Toggle (SWITCH).  When Blynk sends '1':
      1. Check physical Motor Switch
      2. Check not in emergency/post-emergency
      3. Directly call motor.toggle_digital(), skipping hold-to-start
    """
    if not value or len(value) == 0:
        return

    if int(value[0]) != 1:
        return

    app = App.get_running_app()
    live = app.root.get_screen("live")

    # Check physical Motor Switch state:
    if platform.system() == "Windows":
        motor_switch_on = not live.fake_motor_switch
    else:
        motor_switch_on = (GPIO.input(MOTOR_SWITCH) == GPIO.LOW)

    if not motor_switch_on:
        Clock.schedule_once(lambda dt: live.show_popup("Please Turn On The Motor Switch"))
        return

    # If emergency or post‐emergency, ignore:
    if live.motor.in_emergency or live.motor.post_emergency:
        return

    # DIRECTLY toggle digital (bypass start_confirmed):
    live.motor.toggle_digital()


@blynk.on("V3")
def handle_emergency_button(value):
    """
    V3 is the Emergency Stop (SWITCH).  When Blynk sends '1', immediately
    trigger emergency—same as pressing the on-screen EMERGENCY button.
    """
    if not value or len(value) == 0:
        return

    if int(value[0]) == 1:
        app = App.get_running_app()
        live = app.root.get_screen("live")
        Clock.schedule_once(lambda dt: live.activate_emergency(None))


# ──────────────────────────────────────────────────────────────────────────────
# KIVY APP
# ──────────────────────────────────────────────────────────────────────────────
class SplashScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        layout = BoxLayout(orientation="vertical", padding=10)
        self.label = Label(text="Booting P.I.S.A.U", font_size="24sp")
        layout.add_widget(self.label)
        self.add_widget(layout)

        Clock.schedule_once(self.show_full_name, 1)
        Clock.schedule_once(self.show_greeting, 2)
        Clock.schedule_once(self.switch_to_live, 3)

    def show_full_name(self, dt):
        self.label.text = "Programmable Interface for Saw Automation Unit"

    def show_greeting(self, dt):
        self.label.text = "Welcome to P.I.S.A.U!"

    def switch_to_live(self, dt):
        self.manager.current = "live"


class LiveViewScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        # ───────────────────────────────────────────────────
        # Flags to track warnings/popups
        self._temp_warning_shown = False
        self._popup_active = False
        self._last_popup_text = ""
        # ───────────────────────────────────────────────────

        # ───────────────────────────────────────────────────
        # 1) HANDLE “FAKE” MOTOR SWITCH ON WINDOWS & KEYBOARD
        # ───────────────────────────────────────────────────
        self.fake_motor_switch = True  # True = OFF, False = ON
        # Bind Window key-down directly for Windows
        Window.bind(on_key_down=self._on_window_key_down)

        # ───────────────────────────────────────────────────
        # 2) GPIO SETUP FOR SWITCHES & BUZZER
        # ───────────────────────────────────────────────────
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(MOTOR_SWITCH, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(PI_SWITCH, GPIO.IN, pull_up_down=GPIO.PUD_UP)

        # ───────────────────────────────────────────────────
        # 3) BUILD UI OVERLAY
        # ───────────────────────────────────────────────────
        self.root_layout = FloatLayout()
        self.add_widget(self.root_layout)

        self.main_layout = BoxLayout(orientation="vertical")
        self.root_layout.add_widget(self.main_layout)

        # Motor simulator
        self.motor = App.get_running_app().motor

        # Runtime tracking
        self.run_hours = load_run_hours()
        self.run_alerted = False  # True if we've already shown the 30h popup

        # ───────────────────────────────────────────────────
        # 4) TOP STATUS BAR
        # ───────────────────────────────────────────────────
        self.status_bar = BoxLayout(
            orientation="horizontal", size_hint_y=None, height=30, spacing=10, padding=5
        )
        self.status_indicator_label = Label(
            text="Status: Motor Switch Off", font_size=14, size_hint_x=0.3
        )
        self.battery_label = Label(text="Battery: --%", font_size=14, size_hint_x=0.2)
        self.wifi_label = Label(text="WiFi: --", font_size=14, size_hint_x=0.3)
        self.time_label = Label(text="Time: --:--", font_size=14, size_hint_x=0.1)
        self.date_label = Label(text="Date: --/--/----", font_size=14, size_hint_x=0.2)

        self.status_bar.add_widget(self.status_indicator_label)
        self.status_bar.add_widget(self.battery_label)
        self.status_bar.add_widget(self.wifi_label)
        self.status_bar.add_widget(self.time_label)
        self.status_bar.add_widget(self.date_label)

        with self.root_layout.canvas.before:
            Color(0.2, 0.2, 0.2, 1)
            self.status_bar_bg = Rectangle(pos=(0, Window.height - 30), size=(Window.width, 30))
        self.bind(size=self._update_status_bar_bg, pos=self._update_status_bar_bg)
        self.main_layout.add_widget(self.status_bar)

        # ───────────────────────────────────────────────────
        # 5) PRESET BUTTONS + DIG TOGGLE
        # ───────────────────────────────────────────────────
        presets_layout = BoxLayout(size_hint_y=None, height=40, spacing=10, padding=5)

        # DIG TOGGLE
        self.btn_dig_toggle = Button(text="Dig Toggle")
        self.btn_dig_toggle.bind(on_press=self.on_dig_toggle)
        presets_layout.add_widget(self.btn_dig_toggle)

        self.btn_slow = Button(text="Slow")
        self.btn_slow.bind(on_press=lambda x: self.on_speed_button(20))
        self.btn_medium = Button(text="Medium")
        self.btn_medium.bind(on_press=lambda x: self.on_speed_button(50))
        self.btn_fast = Button(text="Fast")
        self.btn_fast.bind(on_press=lambda x: self.on_speed_button(90))
        self.btn_manual = Button(text="Manual Ovr")
        self.btn_manual.bind(on_press=self.open_manual_override)

        presets_layout.add_widget(self.btn_slow)
        presets_layout.add_widget(self.btn_medium)
        presets_layout.add_widget(self.btn_fast)
        presets_layout.add_widget(self.btn_manual)

        self.main_layout.add_widget(presets_layout)

        # ───────────────────────────────────────────────────
        # 6) MIDDLE AREA (CAMERA + SAW STATUS)
        # ───────────────────────────────────────────────────
        middle_layout = BoxLayout(orientation="horizontal")

        # Left: Camera preview
        self.img = Image(size_hint_x=0.7)
        middle_layout.add_widget(self.img)

        # Right: Saw Status Panel
        status_panel = BoxLayout(orientation="vertical", size_hint_x=0.3, padding=10, spacing=10)
        lbl_title = Label(text="Saw Status", font_size=20, size_hint_y=None, height=30)

        self.lbl_set_speed = Label(text="Set Speed: 0%", font_size=16, size_hint_y=None, height=25)
        self.lbl_set_rpm = Label(text="Set RPM: 0", font_size=16, size_hint_y=None, height=25)
        self.lbl_current_rpm = Label(
            text="Current RPM: 0", font_size=16, size_hint_y=None, height=25
        )
        self.lbl_temp = Label(text="Temperature: 0.0°C", font_size=16, size_hint_y=None, height=25)
        self.lbl_run_hours = Label(text="Run Hours: 0.0 h", font_size=16, size_hint_y=None, height=25)

        status_panel.add_widget(lbl_title)
        status_panel.add_widget(self.lbl_set_speed)
        status_panel.add_widget(self.lbl_set_rpm)
        status_panel.add_widget(self.lbl_current_rpm)
        status_panel.add_widget(self.lbl_temp)
        status_panel.add_widget(self.lbl_run_hours)
        status_panel.add_widget(Label())  # Spacer

        middle_layout.add_widget(status_panel)
        self.main_layout.add_widget(middle_layout)

        # ───────────────────────────────────────────────────
        # 7) CUTTING BOARD STATUS Label
        # ───────────────────────────────────────────────────
        self.status_label = Label(text="Cutting Board is Clear", font_size=20, size_hint_y=None, height=30)
        self.main_layout.add_widget(self.status_label)

        # ───────────────────────────────────────────────────
        # 8) BOTTOM ROW (EMERGENCY STOP, SETTINGS, HOLD TO START, TOGGLE BLYNK)
        # ───────────────────────────────────────────────────
        bottom_layout = BoxLayout(size_hint_y=None, height=BOTTOM_ROW_HEIGHT, spacing=10, padding=5)
        self.btn_emergency = Button(text="EMERGENCY STOP", background_color=(1, 0, 0, 1))
        self.btn_emergency.bind(on_press=self.activate_emergency)
        bottom_layout.add_widget(self.btn_emergency)

        self.btn_settings = Button(text="Settings", size_hint_x=0.2)
        self.btn_settings.bind(on_press=lambda x: setattr(self.manager, "current", "settings"))
        bottom_layout.add_widget(self.btn_settings)

        # “Hold To Start” button
        self.btn_hold_to_start = Button(text="Hold To Start", size_hint_x=0.2)
        bottom_layout.add_widget(self.btn_hold_to_start)

        # Toggle Blynk Integration button
        self.btn_toggle_blynk = Button(text="Blynk Off", size_hint_x=0.2)
        self.btn_toggle_blynk.bind(on_press=self.toggle_blynk)
        bottom_layout.add_widget(self.btn_toggle_blynk)

        self.main_layout.add_widget(bottom_layout)

        # ───────────────────────────────────────────────────
        # 9) CAMERA SETUP
        # ───────────────────────────────────────────────────
        if platform.system() == "Linux":
            self.capture = cv2.VideoCapture(0, cv2.CAP_V4L2)
        else:
            self.capture = cv2.VideoCapture(0)
        self.capture.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
        self.capture.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
        self.capture.set(cv2.CAP_PROP_FOURCC, CAMERA_FOURCC)
        if not self.capture.isOpened():
            print("[ERROR] Camera module failed to open.")

        # ───────────────────────────────────────────────────
        # 10) ZONE / DETECTION PARAMETERS
        # ───────────────────────────────────────────────────
        self.outer_width = 400
        self.outer_height = 300
        self.inner_width = 200
        self.inner_height = 200
        self.zone_offset_x = 0
        self.zone_offset_y = 0
        self.hatch_spacing = 20
        self.warning_duration = 3

        # For overlay hatching
        self.overlay_dirty = True
        self.static_overlay = None

        # Motion/hand warning
        self.last_motion_time = 0
        self.warning_text = ""
        self.flash_event = None
        self.flash_counter = 0

        # Frame differencing
        self.prev_frame = None

        # Manual override overlay
        self.manual_overlay = None

        # Emergency & Post-Emergency flags
        self.emergency_countdown_event = None
        self.emergency_touch_enabled = False

        # Frame-skip toggle (to reduce CPU load)
        self.frame_toggle = False

        # Temperature/RPM blink events (optional)
        self.temp_blink_event = None
        self.rpm_blink_event = None
        self.temp_blink_state = False
        self.rpm_blink_state = False

        # “Hold To Start” hold-down scheduling
        self._hold_event = None

        # Runtime accumulation
        self.last_runtime_update = time.time()

        # ───────────────────────────────────────────────────
        # 11) SCHEDULE CLOCK CALLBACKS
        # ───────────────────────────────────────────────────
        Clock.schedule_interval(self.update_frame, 1.0 / 15.0)    # Camera @ 15 FPS
        Clock.schedule_interval(self.update_status_bar, 1.0)      # Status bar @ 1 Hz
        Clock.schedule_interval(self.update_motor_status, 0.5)    # Motor/Temp logic @ 0.5 Hz
        Clock.schedule_interval(self.update_run_hours, 1.0)       # Runtime accumulation @ 1 Hz

        # Force initial update of status bar to reflect fake switch OFF:
        Clock.schedule_once(lambda dt: self.update_status_bar(0), 0)

        self.load_settings_from_file()

    # ──────────────────────────────────────────────────────────────────────────
    #  WINDOW KEYBOARD HANDLING FOR WINDOWS (and runtime debug)
    # ──────────────────────────────────────────────────────────────────────────
    def _on_window_key_down(self, window, key, scancode, codepoint, modifier):
        # We want the "1" key (not numpad) → toggles fake motor switch
        if codepoint == "1" and platform.system() == "Windows":
            self.fake_motor_switch = not self.fake_motor_switch
            # Immediately update the status bar if switch changed:
            self.update_status_bar(0)
        # For debugging: "2" adds 1 hour to runtime
        if codepoint == "2":
            self.run_hours += 1.0
            save_run_hours(self.run_hours)
            if self.run_hours >= 30.0 and not self.run_alerted:
                self.show_popup(f"Time for blade inspection / lubrication. Run hours: {int(self.run_hours)} h.")
                self.run_alerted = True

    # ──────────────────────────────────────────────────────────────────────────
    #  STATUS BAR BACKGROUND UPDATE (WINDOW RESIZE)
    # ──────────────────────────────────────────────────────────────────────────
    def _update_status_bar_bg(self, *args):
        # Keep the status‐bar background rectangle pinned to the top
        self.status_bar_bg.pos = (0, self.height - 30)
        self.status_bar_bg.size = (self.width, 30)

    # ──────────────────────────────────────────────────────────────────────────
    #  RUNTIME ACCUMULATION LOGIC
    # ──────────────────────────────────────────────────────────────────────────
    def update_run_hours(self, dt):
        """
        Increment run_hours by 1/3600 every second if motor is running.
        Show popup at 30h if not already shown.
        Also update the run-hours label and Blynk V2.
        """
        if self.motor.set_speed_percent > 0 and not (self.motor.in_emergency or self.motor.post_emergency):
            now = time.time()
            elapsed = now - self.last_runtime_update
            self.last_runtime_update = now
            self.run_hours += elapsed / 3600.0
            save_run_hours(self.run_hours)
            if self.run_hours >= 30.0 and not self.run_alerted:
                self.show_popup(f"Time for blade inspection / lubrication. Run hours: {int(self.run_hours)} h.")
                self.run_alerted = True
        else:
            self.last_runtime_update = time.time()

        # Update the bottom of Saw Status panel:
        self.lbl_run_hours.text = f"Run Hours: {self.run_hours:.1f} h"

        # Send run hours to Blynk V2 if enabled:
        if App.get_running_app().blynk_enabled:
            blynk.virtual_write(2, round(self.run_hours, 1))

    # ──────────────────────────────────────────────────────────────────────────
    #  UI CALLBACKS (Preset Buttons, Dig Toggle, Manual Slider, Hold To Start)
    # ──────────────────────────────────────────────────────────────────────────
    def on_speed_button(self, percent):
        """
        Called by Slow/Medium/Fast buttons. If triggered from Blynk Slider, percent is passed too.
        """
        # Determine physical or fake switch
        if platform.system() == "Windows":
            motor_switch_on = not self.fake_motor_switch
        else:
            motor_switch_on = (GPIO.input(MOTOR_SWITCH) == GPIO.LOW)

        if not motor_switch_on:
            self.show_popup("Please Turn On The Motor Switch")
            return

        if self.motor.post_emergency or self.motor.in_emergency:
            return

        # Require hold-to-start confirmation
        if not getattr(self, "start_confirmed", False):
            self.show_popup("Hold the 'Hold To Start' button to begin")
            return

        self.motor.set_speed(percent)

    def on_dig_toggle(self, instance):
        """
        Called when “Dig Toggle” is pressed (or via Blynk). Similar guard logic as presets.
        """
        if platform.system() == "Windows":
            motor_switch_on = not self.fake_motor_switch
        else:
            motor_switch_on = (GPIO.input(MOTOR_SWITCH) == GPIO.LOW)

        if not motor_switch_on:
            self.show_popup("Please Turn On The Motor Switch")
            return

        if self.motor.post_emergency or self.motor.in_emergency:
            return

        if not getattr(self, "start_confirmed", False):
            self.show_popup("Hold the 'Hold To Start' button to begin")
            return

        self.motor.toggle_digital()

    def open_manual_override(self, instance):
        """
        Show a popup overlay for manual speed if switch ON and start_confirmed.
        """
        if platform.system() == "Windows":
            motor_switch_on = not self.fake_motor_switch
        else:
            motor_switch_on = (GPIO.input(MOTOR_SWITCH) == GPIO.LOW)

        if not motor_switch_on:
            self.show_popup("Please Turn On The Motor Switch")
            return

        if self.motor.post_emergency or self.motor.in_emergency:
            return

        if not getattr(self, "start_confirmed", False):
            self.show_popup("Hold the 'Hold To Start' button to begin")
            return

        if self.manual_overlay:
            self.close_manual_override()

        overlay_height = Window.height - BOTTOM_ROW_HEIGHT
        overlay = FloatLayout(
            size=(Window.width, overlay_height),
            size_hint=(None, None),
            pos=(0, BOTTOM_ROW_HEIGHT)
        )

        with overlay.canvas:
            Color(0, 0, 0, 0.7)
            Rectangle(pos=(0, 0), size=(Window.width, overlay_height))

            box_width = Window.width * 0.6
            box_height = overlay_height * 0.4
            box_x = (Window.width - box_width) / 2
            box_y = (overlay_height - box_height) / 2

            Color(0.1, 0.1, 0.1, 0.9)
            Rectangle(pos=(box_x, box_y), size=(box_width, box_height))

            Color(1, 1, 1, 1)
            Line(rectangle=(box_x, box_y, box_width, box_height), width=2)

        content = BoxLayout(
            orientation="vertical",
            size=(box_width * 0.9, box_height * 0.7),
            size_hint=(None, None)
        )
        content.pos = (
            box_x + (box_width * 0.05),
            BOTTOM_ROW_HEIGHT + box_y + (box_height * 0.15)
        )
        content.spacing = 10
        content.padding = 10

        self.manual_title = Label(
            text="MANUAL SAW SPEED",
            font_size=20,
            size_hint_y=None,
            height=30
        )
        content.add_widget(self.manual_title)

        val = self.motor.set_speed_percent
        self.manual_label = Label(text=f"{val}%", font_size=24, size_hint_y=None, height=40)
        self.manual_slider = Slider(min=0, max=100, value=val, size_hint_y=None, height=40)
        self.manual_slider.bind(value=self.on_manual_slider)

        content.add_widget(self.manual_label)
        content.add_widget(self.manual_slider)

        done_btn = Button(text="Done", size_hint_y=None, height=40)
        done_btn.bind(on_press=lambda x: self.close_manual_override())
        content.add_widget(done_btn)

        overlay.add_widget(content, index=-1)
        self.manual_overlay = overlay
        self.root_layout.add_widget(overlay, index=-1)

    def on_manual_slider(self, slider, val):
        """
        Called as the Manual slider moves. Same guard logic as presets.
        """
        if platform.system() == "Windows":
            motor_switch_on = not self.fake_motor_switch
        else:
            motor_switch_on = (GPIO.input(MOTOR_SWITCH) == GPIO.LOW)

        if not motor_switch_on:
            return

        if self.motor.post_emergency or self.motor.in_emergency:
            return

        if not getattr(self, "start_confirmed", False):
            return

        v = int(val)
        self.motor.set_speed(v)
        self.manual_label.text = f"{v}%"

    def close_manual_override(self):
        if self.manual_overlay:
            self.root_layout.remove_widget(self.manual_overlay)
            self.manual_overlay = None

    # ──────────────────────────────────────────────────────────────────────────
    #  “HOLD TO START” BUTTON LOGIC
    # ──────────────────────────────────────────────────────────────────────────
    def on_touch_down(self, touch):
        """
        Capture touch_down on the “Hold To Start” button. If pressed there, schedule
        confirmation after 1 second.
        """
        if self.btn_hold_to_start.collide_point(*touch.pos):
            # Only allow hold if motor switch is ON and not in any emergency
            if platform.system() == "Windows":
                motor_switch_on = not self.fake_motor_switch
            else:
                motor_switch_on = (GPIO.input(MOTOR_SWITCH) == GPIO.LOW)

            if motor_switch_on and not (self.motor.in_emergency or self.motor.post_emergency):
                # Schedule confirmation in 1 second
                self._hold_event = Clock.schedule_once(self.confirm_start, 1.0)
            return True
        return super().on_touch_down(touch)

    def on_touch_up(self, touch):
        """
        If we release before 1 second, cancel the scheduled confirmation.
        """
        if self._hold_event:
            self._hold_event.cancel()
            self._hold_event = None
        return super().on_touch_up(touch)

    def confirm_start(self, dt):
        """
        Called after holding “Hold To Start” for 1 second. Mark start_confirmed = True
        and change button text briefly.
        """
        self.start_confirmed = True
        self.btn_hold_to_start.text = "Started"
        Clock.schedule_once(self.reset_hold_button, 1.0)

    def reset_hold_button(self, dt):
        """
        After a brief delay, revert “Hold To Start” button text back to normal.
        """
        self.btn_hold_to_start.text = "Hold To Start"

    # ──────────────────────────────────────────────────────────────────────────
    #  STATUS BAR & SWITCH LOGIC
    # ──────────────────────────────────────────────────────────────────────────
    def update_status_bar(self, dt):
        # A) HANDLE Pi SHUTDOWN SWITCH
        if GPIO.input(PI_SWITCH) == GPIO.LOW:
            Clock.schedule_once(lambda dt: os.system("sudo shutdown now"), 0)
            return

        # B) HANDLE Motor SWITCH
        if platform.system() == "Windows":
            motor_switch_on = not self.fake_motor_switch
        else:
            motor_switch_on = (GPIO.input(MOTOR_SWITCH) == GPIO.LOW)

        if not motor_switch_on:
            # Switch OFF → disable motor
            self.motor.disable_motor()
            if self.motor.post_emergency:
                self.status_indicator_label.text = "Status: Motor Switch Off"
            else:
                self.status_indicator_label.text = "Status: Motor Switch Off"
        else:
            # Switch ON
            if self.motor.post_emergency:
                self.motor.complete_post_emergency()
                self.status_indicator_label.text = "Status: Ready"
            elif not self.motor.in_emergency:
                self.motor.enable_motor()
                self.status_indicator_label.text = "Status: Ready"
            else:
                self.status_indicator_label.text = "Status: Emergency"

        # C) DATE / TIME / WiFi
        t = time.localtime()
        self.time_label.text = time.strftime("Time: %H:%M:%S", t)
        self.date_label.text = time.strftime("Date: %d/%m/%Y", t)

        ssid = "--"
        if platform.system() == "Linux":
            try:
                ssid = subprocess.check_output(["iwgetid", "-r"]).decode().strip()
                if not ssid:
                    ssid = "--"
            except subprocess.CalledProcessError:
                ssid = "--"
        self.wifi_label.text = f"WiFi: {ssid}"

        # Send status text to Blynk V6 if enabled:
        if App.get_running_app().blynk_enabled:
            status_text = self.status_indicator_label.text.replace("Status: ", "")
            blynk.virtual_write(6, status_text)

    # ──────────────────────────────────────────────────────────────────────────
    #  MOTOR & TEMPERATURE UPDATES (every 0.5 s)
    # ──────────────────────────────────────────────────────────────────────────
    def update_motor_status(self, dt):
        if self.motor.in_emergency or self.motor.post_emergency:
            self.motor.update(dt)
            return

        # 1) Run motor simulation
        self.motor.update(dt)

        # 2) Refresh Saw Status labels
        sp = self.motor.set_speed_percent
        sr = self.motor.set_rpm
        cr = int(self.motor.current_rpm)
        temp = self.motor.temperature

        self.lbl_set_speed.text = f"Set Speed: {sp}%"
        self.lbl_set_rpm.text = f"Set RPM: {sr}"
        self.lbl_current_rpm.text = f"Current RPM: {cr}"
        self.lbl_temp.text = f"Temperature: {temp:.1f}°C"
        # lbl_run_hours is updated separately in update_run_hours()

        # Send current RPM to Blynk V0 and temperature to V1 if enabled:
        if App.get_running_app().blynk_enabled:
            blynk.virtual_write(0, cr)
            blynk.virtual_write(1, int(temp))

        # 3) High-temperature warning (≥ 60 °C), only show once per over-temp event
        if temp >= 60.0 and not self.motor.slow_down and not (
            self._popup_active and self._last_popup_text == "Slow Saw Down to lower machine temperature"
        ):
            self.show_popup("Slow Saw Down to lower machine temperature")
            self._last_popup_text = "Slow Saw Down to lower machine temperature"

        # 4) High-RPM warning (≥ 2700 RPM), only show once per over-RPM event
        if cr >= 2700 and not self.motor.slow_down and not (
            self._popup_active and self._last_popup_text == "Slow Saw Down to save machine health"
        ):
            self.show_popup("Slow Saw Down to save machine health")
            self._last_popup_text = "Slow Saw Down to save machine health"

        # 5) Reset the temperature‐warning flag once temperature drops below threshold
        if temp < 60.0 and self._last_popup_text == "Slow Saw Down to lower machine temperature":
            self._last_popup_text = ""

        # 6) Reset the RPM‐warning flag once RPM drops below threshold
        if cr < 2700 and self._last_popup_text == "Slow Saw Down to save machine health":
            self._last_popup_text = ""

    # ──────────────────────────────────────────────────────────────────────────
    #  CAMERA / MOTION & HAND DETECTION (every frame)
    # ──────────────────────────────────────────────────────────────────────────
    def compute_zones(self, frame_width, frame_height):
        cx = frame_width // 2 + self.zone_offset_x
        cy = frame_height // 2 + self.zone_offset_y

        top_left = (int(cx - self.outer_width / 2), int(cy + self.outer_height / 2))
        top_right = (int(cx + self.outer_width / 2), int(cy + self.outer_height / 2))
        bottom_left = (int(cx - self.outer_width / 4), int(cy - self.outer_height / 2))
        bottom_right = (int(cx + self.outer_width / 4), int(cy - self.outer_height / 2))
        self.outer_zone = np.array([top_left, top_right, bottom_right, bottom_left])

        inner_top_left = (int(cx - self.inner_width / 2), int(cy + self.inner_height / 2))
        inner_top_right = (int(cx + self.inner_width / 2), int(cy + self.inner_height / 2))
        inner_bottom_left = (int(cx - self.inner_width / 4), int(cy - self.inner_height / 2))
        inner_bottom_right = (int(cx + self.inner_width / 4), int(cy - self.inner_height / 2))
        self.inner_zone = np.array(
            [inner_top_left, inner_top_right, inner_bottom_right, inner_bottom_left]
        )

    def generate_static_overlay(self, frame_width, frame_height):
        overlay = np.zeros((frame_height, frame_width, 3), dtype=np.uint8)

        mask_outer = np.zeros((frame_height, frame_width), dtype=np.uint8)
        cv2.fillPoly(mask_outer, [self.outer_zone], 255)

        temp_overlay = np.zeros_like(overlay)
        for i in range(-frame_height, frame_width, self.hatch_spacing):
            cv2.line(temp_overlay, (i, 0), (i + frame_height, frame_height), (255, 255, 255), 1)

        hatched_outside = np.zeros_like(overlay)
        hatched_outside[mask_outer == 0] = temp_overlay[mask_outer == 0]

        cv2.polylines(hatched_outside, [self.outer_zone], True, (0, 255, 0), 2)
        cv2.polylines(hatched_outside, [self.inner_zone], True, (0, 0, 255), 2)

        self.static_overlay = hatched_outside
        self.overlay_dirty = False

    def update_frame(self, dt):
        if self.motor.in_emergency:
            return

        ret, frame = self.capture.read()
        if not ret:
            return

        frame_height, frame_width = frame.shape[:2]

        # MUST compute_zones BEFORE generate_static_overlay
        self.compute_zones(frame_width, frame_height)
        if self.overlay_dirty or self.static_overlay is None:
            self.generate_static_overlay(frame_width, frame_height)

        self.frame_toggle = not self.frame_toggle
        do_full_process = self.frame_toggle

        if do_full_process:
            draw_frame = frame.copy()
            if self.static_overlay is not None:
                mask_nonzero = np.any(self.static_overlay != [0, 0, 0], axis=2)
                draw_frame[mask_nonzero] = self.static_overlay[mask_nonzero]

            # 1) Hand detection
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            skin_mask = cv2.inRange(hsv, SKIN_LOWER_HSV, SKIN_UPPER_HSV)
            contours_skin, _ = cv2.findContours(
                skin_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )

            hand_detected = False
            for contour in contours_skin:
                area = cv2.contourArea(contour)
                if area < MIN_HAND_AREA:
                    continue
                M = cv2.moments(contour)
                if M["m00"] == 0:
                    continue
                cx = int(M["m10"] / M["m00"])
                cy = int(M["m01"] / M["m00"])
                if cv2.pointPolygonTest(self.inner_zone, (cx, cy), False) >= 0:
                    cv2.drawContours(draw_frame, [contour], -1, (0, 0, 255), 2)
                    if self.flash_event is None:
                        self.flash_counter = 6
                        self.flash_event = Clock.schedule_interval(self.flash_background, 0.2)
                    self.warning_text = "[color=ff0]WARNING: HAND DETECTED IN CUTTING AREA[/color]"
                    self.last_motion_time = time.time()
                    log_event("HAND_DETECTED", f"({cx},{cy})")
                    hand_detected = True
                    break

            # 2) Motion detection if no hand detected
            if not hand_detected:
                small = cv2.resize(frame, (320, 240))
                gray_small = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                blur_small = cv2.GaussianBlur(gray_small, (5, 5), 0)

                if self.prev_frame is None:
                    self.prev_frame = blur_small

                    buf = cv2.flip(draw_frame, 0).tobytes()
                    if not self.img.texture:
                        self.img.texture = Texture.create(size=(frame_width, frame_height), colorfmt='bgr')
                    self.img.texture.blit_buffer(buf, colorfmt='bgr', bufferfmt='ubyte')
                    return

                diff = cv2.absdiff(self.prev_frame, blur_small)
                _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
                dilated = cv2.dilate(thresh, None, iterations=2)
                contours_motion, _ = cv2.findContours(
                    dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
                )

                motion_warning = ""
                for contour in contours_motion:
                    area = cv2.contourArea(contour)
                    scaled_min_area = MIN_MOTION_AREA * (320 * 240) / (640 * 480)
                    if area < scaled_min_area:
                        continue
                    x_s, y_s, w_s, h_s = cv2.boundingRect(contour)
                    x = x_s * 2
                    y = y_s * 2
                    w = w_s * 2
                    h = h_s * 2
                    cx = x + w // 2
                    cy = y + h // 2

                    if cv2.pointPolygonTest(self.inner_zone, (cx, cy), False) >= 0:
                        cv2.rectangle(draw_frame, (x, y), (x + w, y + h), (0, 0, 255), 2)
                        motion_warning = "[color=ff0]Moving Item IN Cutting Board Area[/color]"
                        self.last_motion_time = time.time()
                        log_event("MOTION_INNER", f"({cx},{cy})")
                        break
                    elif cv2.pointPolygonTest(self.outer_zone, (cx, cy), False) >= 0:
                        cv2.rectangle(draw_frame, (x, y), (x + w, y + h), (0, 255, 255), 2)
                        motion_warning = "[color=ff0]Moving Item NEAR Cutting Board Area[/color]"
                        self.last_motion_time = time.time()
                        log_event("MOTION_OUTER", f"({cx},{cy})")
                        break
                    else:
                        cv2.rectangle(draw_frame, (x, y), (x + w, y + h), (255, 255, 255), 2)

                if motion_warning:
                    self.warning_text = motion_warning

                self.prev_frame = blur_small

            # Display on Kivy Image
            buf = cv2.flip(draw_frame, 0).tobytes()
            if not self.img.texture:
                self.img.texture = Texture.create(size=(frame_width, frame_height), colorfmt='bgr')
            self.img.texture.blit_buffer(buf, colorfmt='bgr', bufferfmt='ubyte')

        else:
            draw_frame = frame.copy()
            if self.static_overlay is not None:
                mask_nonzero = np.any(self.static_overlay != [0, 0, 0], axis=2)
                draw_frame[mask_nonzero] = self.static_overlay[mask_nonzero]

            buf = cv2.flip(draw_frame, 0).tobytes()
            if not self.img.texture:
                self.img.texture = Texture.create(size=(frame_width, frame_height), colorfmt='bgr')
            self.img.texture.blit_buffer(buf, colorfmt='bgr', bufferfmt='ubyte')

        # Update cutting board status label
        if time.time() - self.last_motion_time <= self.warning_duration:
            self.status_label.markup = True
            self.status_label.text = self.warning_text
            self.status_label.color = (1, 1, 0, 1)
            self.status_label.font_size = 22
        else:
            self.status_label.markup = False
            self.status_label.text = "Cutting Board is Clear"
            self.status_label.color = (1, 1, 1, 1)
            self.status_label.font_size = 20

    def flash_background(self, dt):
        if self.flash_counter > 0:
            intensity = self.flash_counter / 6.0
            with self.root_layout.canvas.before:
                Color(1, 0, 0, intensity)
                Rectangle(pos=self.pos, size=self.size)
            self.flash_counter -= 1
        else:
            if self.flash_event:
                self.flash_event.cancel()
                self.flash_event = None

    # ──────────────────────────────────────────────────────────────────────────
    #  EMERGENCY STOP HANDLING
    # ──────────────────────────────────────────────────────────────────────────
    def activate_emergency(self, instance):
        """
        1) Close manual overlay if open
        2) Immediately disable motor and enter emergency
        3) Show full-screen red panel with “EMERGENCY STOP INITIATED”
        4) After 2 s, show “Touch Anywhere to Reset” and enable taps
        """
        # 1) Close any manual popup
        if self.manual_overlay:
            self.close_manual_override()

        # 2) Enter emergency and disable motor
        self.motor.enter_emergency()
        self.in_emergency = True
        Clock.unschedule(self.update_frame)
        Clock.unschedule(self.update_status_bar)
        Clock.unschedule(self.update_motor_status)
        if self.flash_event:
            self.flash_event.cancel()

        # 3) Draw full-screen red background
        self.main_layout.clear_widgets()
        self.root_layout.clear_widgets()
        self.root_layout.canvas.before.clear()
        with self.root_layout.canvas.before:
            Color(1, 0, 0, 1)
            self.emergency_bg = Rectangle(pos=self.pos, size=self.size)
        self.bind(size=self._update_emergency_bg, pos=self._update_emergency_bg)

        # Create the layout that will hold labels
        self.emergency_layout = BoxLayout(orientation="vertical")
        stop_label = Label(
            text="[b][color=ffffff]EMERGENCY STOP INITIATED[/color][/b]",
            font_size=32,
            markup=True,
        )
        self.emergency_layout.add_widget(stop_label)
        self.root_layout.add_widget(self.emergency_layout, index=-1)

        # 4) After 2 seconds, show “Touch Anywhere” label & allow taps
        self.emergency_countdown_event = Clock.schedule_once(self.enable_emergency_touch, 2.0)
        log_event("EMERGENCY_STOP", f"set_speed={self.motor.set_speed_percent}")

    def _update_emergency_bg(self, *args):
        self.emergency_bg.pos = self.pos
        self.emergency_bg.size = self.size

    def enable_emergency_touch(self, dt):
        """
        After 2 seconds, add “Touch Anywhere to Reset” and bind a touch handler.
        """
        reset_label = Label(text="Touch Anywhere to Reset", font_size=20)
        self.emergency_layout.add_widget(reset_label)
        self.root_layout.bind(on_touch_down=self.reset_from_emergency)
        self.emergency_touch_enabled = True

    def reset_from_emergency(self, *args):
        """
        When user touches anywhere (after 2 s), unbind and begin Post-Emergency countdown.
        """
        if not self.emergency_touch_enabled:
            return

        self.root_layout.unbind(on_touch_down=self.reset_from_emergency)
        self.emergency_layout.clear_widgets()
        resetting_label = Label(text="Resetting, please wait...", font_size=24)
        self.emergency_layout.add_widget(resetting_label)

        Clock.schedule_once(self.do_reset_after_delay, 0.5)

    def do_reset_after_delay(self, dt):
        # 1) Release camera
        if self.capture.isOpened():
            self.capture.release()

        # 2) Clear UI and canvas
        self.root_layout.clear_widgets()
        self.root_layout.canvas.before.clear()

        # 3) Reset any per‐screen flags
        self.prev_frame = None
        self.overlay_dirty = True
        self.frame_toggle = False
        self.temp_blink_event = None
        self.rpm_blink_event = None
        self.temp_blink_state = False
        self.rpm_blink_state = False

        # 4) Enter Post-Emergency state
        self.motor.exit_emergency()
        self.status_indicator_label.text = "Status: Cycle Motor Switch"
        self.show_popup("Please turn off and on the saw machine switch")

        # 5) Replace this screen with a fresh instance
        fresh = LiveViewScreen(name="live")

        app = App.get_running_app()
        sm = app.root  # Assuming the ScreenManager is the root widget

        if sm:
            sm.remove_widget(self)
            sm.add_widget(fresh)
            sm.current = "live"
        else:
            print("ERROR: No ScreenManager found in app root.")

    # ──────────────────────────────────────────────────────────────────────────
    #  POPUP UTILITY
    # ──────────────────────────────────────────────────────────────────────────
    def show_popup(self, text):
        """
        Display a transient pop‐up for 3 s with the given text.
        If the same text is already active, do nothing.
        """
        # If a popup is already active with this same text, skip.
        if self._popup_active and text == self._last_popup_text:
            return

        # Create the overlay
        popup_overlay = FloatLayout(size=Window.size, size_hint=(None, None), pos=(0, 0))
        with popup_overlay.canvas:
            Color(0, 0, 0, 0.7)
            Rectangle(pos=(0, 0), size=Window.size)

        lbl_width = Window.width * 0.8
        lbl_height = 50
        lbl_x = (Window.width - lbl_width) / 2
        lbl_y = (Window.height - lbl_height) / 2

        with popup_overlay.canvas:
            Color(0.1, 0.1, 0.1, 0.9)
            Rectangle(pos=(lbl_x, lbl_y), size=(lbl_width, lbl_height))

        lbl = Label(
            text=text,
            font_size=20,
            size_hint=(None, None),
            size=(lbl_width, lbl_height),
            pos=(lbl_x, lbl_y),
            color=(1, 1, 1, 1),
            halign="center",
            valign="middle",
        )
        lbl.text_size = (lbl_width, lbl_height)
        popup_overlay.add_widget(lbl)

        # Remember this popup is active
        self._popup_active = True
        self._last_popup_text = text

        self.root_layout.add_widget(popup_overlay, index=-1)
        Clock.schedule_once(lambda dt: self._dismiss_popup(popup_overlay), 3.0)

    def _dismiss_popup(self, popup_overlay):
        if popup_overlay in self.root_layout.children:
            self.root_layout.remove_widget(popup_overlay)
        # Allow future popups, including the same text if needed.
        self._popup_active = False
        self._last_popup_text = ""

    # ──────────────────────────────────────────────────────────────────────────
    #  SETTINGS SCREEN LOGIC (UNCHANGED except Reset Run Hours)
    # ──────────────────────────────────────────────────────────────────────────
    def save_settings_to_file(self):
        settings = {
            "outer_width": self.outer_width,
            "outer_height": self.outer_height,
            "inner_width": self.inner_width,
            "inner_height": self.inner_height,
            "zone_offset_x": self.zone_offset_x,
            "zone_offset_y": self.zone_offset_y,
            "hatch_spacing": self.hatch_spacing,
            "warning_duration": self.warning_duration,
        }
        tmp_path = SETTINGS_FILE + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(settings, f)
        os.replace(tmp_path, SETTINGS_FILE)

    def load_settings_from_file(self):
        if not os.path.exists(SETTINGS_FILE):
            return
        try:
            with open(SETTINGS_FILE, "r") as f:
                settings = json.load(f)
        except json.JSONDecodeError:
            print("[WARNING] motion_settings.json is corrupted. Using defaults.")
            return
        self.outer_width = settings.get("outer_width", self.outer_width)
        self.outer_height = settings.get("outer_height", self.outer_height)
        self.inner_width = settings.get("inner_width", self.inner_width)
        self.inner_height = settings.get("inner_height", self.inner_height)
        self.zone_offset_x = settings.get("zone_offset_x", self.zone_offset_x)
        self.zone_offset_y = settings.get("zone_offset_y", self.zone_offset_y)
        self.hatch_spacing = settings.get("hatch_spacing", self.hatch_spacing)
        self.warning_duration = settings.get("warning_duration", self.warning_duration)

        self.overlay_dirty = True

    def stop_app(self, *args):
        if self.capture.isOpened():
            self.capture.release()
        App.get_running_app().stop()

    def on_stop(self):
        if self.capture.isOpened():
            self.capture.release()
        GPIO.cleanup()

    # ──────────────────────────────────────────────────────────────────────────
    #  BLYNK TOGGLE BUTTON HANDLER
    # ──────────────────────────────────────────────────────────────────────────
    def toggle_blynk(self, instance):
        """
        Toggle the global blynk_enabled flag in the App, and update button text.
        """
        app = App.get_running_app()
        app.blynk_enabled = not app.blynk_enabled
        if app.blynk_enabled:
            instance.text = "Blynk On"
        else:
            instance.text = "Blynk Off"


class SettingsScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        layout = BoxLayout(orientation="vertical")

        self.preview = Image(size_hint_y=0.5)
        layout.add_widget(self.preview)

        scroll = ScrollView()
        sliders_container = BoxLayout(orientation="vertical", size_hint_y=None)
        sliders_container.bind(minimum_height=sliders_container.setter("height"))

        self.sliders = {}

        def create_slider(label_text, min_val, max_val, default_val):
            h = BoxLayout(orientation="horizontal", size_hint_y=None, height=40, padding=5)
            lbl = Label(text=label_text, size_hint_x=0.4)
            slider = Slider(min=min_val, max=max_val, value=default_val, size_hint_x=0.4)
            val_lbl = Label(text=str(int(default_val)), size_hint_x=0.2)

            def on_val(inst, v):
                val_lbl.text = str(int(v))
                self.update_settings()

            slider.bind(value=on_val)
            h.add_widget(lbl)
            h.add_widget(slider)
            h.add_widget(val_lbl)
            sliders_container.add_widget(h)
            self.sliders[label_text] = slider

        live = self.manager.get_screen("live") if self.manager else None
        create_slider("Outer Width", 100, 600, live.outer_width if live else 400)
        create_slider("Outer Height", 100, 400, live.outer_height if live else 300)
        create_slider("Inner Width", 50, 400, live.inner_width if live else 200)
        create_slider("Inner Height", 50, 400, live.inner_height if live else 200)
        create_slider("Offset X", -200, 200, live.zone_offset_x if live else 0)
        create_slider("Offset Y", -200, 200, live.zone_offset_y if live else 0)
        create_slider("Hatch Spacing", 10, 100, live.hatch_spacing if live else 20)
        create_slider("Warning Duration", 1, 10, live.warning_duration if live else 3)

        scroll.add_widget(sliders_container)
        layout.add_widget(scroll)

        # Row: Save, Load, Reset Run Hours
        btn_row = BoxLayout(size_hint_y=None, height=40, spacing=10, padding=10)
        save_btn = Button(text="Save Settings")
        save_btn.bind(on_press=lambda x: self.manager.get_screen("live").save_settings_to_file())
        load_btn = Button(text="Load Settings")
        load_btn.bind(on_press=lambda x: self.load_from_live())
        reset_btn = Button(text="Reset Run Hours")
        reset_btn.bind(on_press=lambda x: self.reset_run_hours())

        btn_row.add_widget(save_btn)
        btn_row.add_widget(load_btn)
        btn_row.add_widget(reset_btn)
        layout.add_widget(btn_row)

        # Back to Live
        back_btn = Button(text="Back to Live View", size_hint_y=None, height=40)
        back_btn.bind(on_press=lambda x: setattr(self.manager, "current", "live"))
        layout.add_widget(back_btn)

        self.add_widget(layout)
        Clock.schedule_interval(self.update_preview, 1.0 / 15.0)

    def load_from_live(self):
        live = self.manager.get_screen("live")
        self.sliders["Outer Width"].value = live.outer_width
        self.sliders["Outer Height"].value = live.outer_height
        self.sliders["Inner Width"].value = live.inner_width
        self.sliders["Inner Height"].value = live.inner_height
        self.sliders["Offset X"].value = live.zone_offset_x
        self.sliders["Offset Y"].value = live.zone_offset_y
        self.sliders["Hatch Spacing"].value = live.hatch_spacing
        self.sliders["Warning Duration"].value = live.warning_duration

    def update_settings(self):
        live = self.manager.get_screen("live")
        live.outer_width = int(self.sliders["Outer Width"].value)
        live.outer_height = int(self.sliders["Outer Height"].value)
        live.inner_width = int(self.sliders["Inner Width"].value)
        live.inner_height = int(self.sliders["Inner Height"].value)
        live.zone_offset_x = int(self.sliders["Offset X"].value)
        live.zone_offset_y = int(self.sliders["Offset Y"].value)
        live.hatch_spacing = int(self.sliders["Hatch Spacing"].value)
        live.warning_duration = int(self.sliders["Warning Duration"].value)

        live.overlay_dirty = True

    def reset_run_hours(self):
        live = self.manager.get_screen("live")
        live.run_hours = 0.0
        save_run_hours(0.0)
        live.lbl_run_hours.text = "Run Hours: 0.0 h"

    def update_preview(self, dt):
        live = self.manager.get_screen("live")
        if not hasattr(live, "prev_frame") or live.prev_frame is None:
            return

        frame = live.prev_frame
        preview_frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        h, w = frame.shape[:2]
        live.compute_zones(w, h)
        preview_hatch = preview_frame.copy()

        mask_outer = np.zeros((h, w), dtype=np.uint8)
        cv2.fillPoly(mask_outer, [live.outer_zone], 255)
        for i in range(-h, w, live.hatch_spacing):
            x1, y1 = i, 0
            x2, y2 = i + h, h
            cv2.line(preview_hatch, (x1, y1), (x2, y2), (255, 255, 255), 1)
        preview_frame[mask_outer == 0] = preview_hatch[mask_outer == 0]

        cv2.polylines(preview_frame, [live.outer_zone], True, (0, 255, 0), 2)
        cv2.polylines(preview_frame, [live.inner_zone], True, (0, 0, 255), 2)

        buf = cv2.flip(preview_frame, 0).tobytes()
        if not self.preview.texture:
            self.preview.texture = Texture.create(size=(w, h), colorfmt="bgr")
        self.preview.texture.blit_buffer(buf, colorfmt='bgr', bufferfmt='ubyte')


class MotionApp(App):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.motor = MotorSimulator()
        self.fake_motor_switch = True  # for status checks on Windows

        # Blynk integration toggled off by default
        self.blynk_enabled = False

    def build(self):
        sm = ScreenManager()
        sm.add_widget(SplashScreen(name="splash"))
        sm.add_widget(LiveViewScreen(name="live"))
        sm.add_widget(SettingsScreen(name="settings"))

        # Spawn Blynk’s run() in a background thread so it doesn’t block Kivy:
        threading.Thread(target=self._run_blynk_loop, daemon=True).start()
        return sm

    def _run_blynk_loop(self):
        print(">> Blynk thread starting…")
        while True:
            try:
                blynk.run()
            except Exception:
                return

    def on_stop(self):
        live = self.root.get_screen("live")
        if hasattr(live, "capture") and live.capture.isOpened():
            live.capture.release()
        GPIO.cleanup()


if __name__ == "__main__":
    # Always run fullscreen
    Window.fullscreen = True

    # Hide cursor on non-Windows platforms
    if platform.system() != "Windows":
        Window.show_cursor = False

    MotionApp().run()
