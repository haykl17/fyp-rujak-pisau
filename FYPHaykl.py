import json
import os
import platform
import subprocess
import time

import numpy as np
import cv2

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

# Constants for settings
SETTINGS_FILE = "motion_settings.json"
FRAME_WIDTH = 640
FRAME_HEIGHT = 480
CAMERA_FOURCC = cv2.VideoWriter_fourcc(*'MJPG')
MIN_HAND_AREA = 3000
MIN_MOTION_AREA = 1000
SKIN_LOWER_HSV = np.array([0, 20, 70], dtype=np.uint8)
SKIN_UPPER_HSV = np.array([20, 255, 255], dtype=np.uint8)
MAX_RPM = 2750           # ### CHANGED: 100% now = 2750 RPM
BASELINE_TEMP = 27.0     # ### ADDED: Malaysia avg ambient temperature
BOTTOM_ROW_HEIGHT = 40   # Height of bottom row (Emergency Stop, etc.)


class MotorSimulator:
    """
    Simulates a motor with:
     - set_speed_percent (0–100)
     - set_rpm (0–2750)
     - current_rpm (ramps toward set_rpm)
     - temperature in °C
    """
    def __init__(self):
        self.set_speed_percent = 0
        self.set_rpm = 0
        self.current_rpm = 0
        # Start “no temperature” at baseline
        self.temperature = BASELINE_TEMP  # ### CHANGED: start at ambient baseline

    def update(self, dt):
        # 1. Compute new set_rpm (scaled linearly 0–2750)
        self.set_rpm = int(self.set_speed_percent * MAX_RPM / 100)

        # 2. Ramp current_rpm toward set_rpm
        if self.current_rpm < self.set_rpm:
            delta = self.set_rpm - self.current_rpm
            self.current_rpm += min(int(delta * 0.1), 50)
        elif self.current_rpm > self.set_rpm:
            delta = self.current_rpm - self.set_rpm
            self.current_rpm -= min(int(delta * 0.1), 50)

        # 3. Temperature update
        if self.current_rpm > 60:
            # Motor actively running: heat up
            rise_factor = self.current_rpm / MAX_RPM  # 0.0–1.0
            self.temperature += rise_factor * 0.5     # up to +0.5 °C per cycle at full‐speed
        else:
            # Motor idle or very low speed: cool toward baseline slowly
            if self.temperature > BASELINE_TEMP:
                self.temperature -= 0.2
                if self.temperature < BASELINE_TEMP:
                    self.temperature = BASELINE_TEMP
            elif self.temperature < BASELINE_TEMP:
                # If for some reason below baseline, slowly creep up
                self.temperature += 0.1
                if self.temperature > BASELINE_TEMP:
                    self.temperature = BASELINE_TEMP

            # Once at baseline and completely idle (RPM=0), add small ±1 °C fluctuation
            if self.current_rpm == 0 and abs(self.temperature - BASELINE_TEMP) < 0.01:
                self.temperature = BASELINE_TEMP + np.random.uniform(-1, 1)
                # Clamp to [baseline - 1, baseline + 1]
                if self.temperature < BASELINE_TEMP - 1:
                    self.temperature = BASELINE_TEMP - 1
                if self.temperature > BASELINE_TEMP + 1:
                    self.temperature = BASELINE_TEMP + 1

        # Always keep temperature ≥ 0
        if self.temperature < 0:
            self.temperature = 0.0
        if self.temperature > 70.0:
            self.temperature = 70.0


class SplashScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        layout = BoxLayout(orientation='vertical', padding=10)
        self.label = Label(text="Booting P.I.S.A.U", font_size='24sp')
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
        self.manager.current = 'live'


class LiveViewScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        # Use FloatLayout to allow overlay drawing
        self.root_layout = FloatLayout()
        self.add_widget(self.root_layout)

        # Underlying UI is a vertical BoxLayout
        self.main_layout = BoxLayout(orientation='vertical')
        self.root_layout.add_widget(self.main_layout)

        # Motor simulator
        self.motor = App.get_running_app().motor

        # ----------------------------
        # Top Status Bar (Height = 30)
        # ----------------------------
        self.status_bar = BoxLayout(
            orientation='horizontal', size_hint_y=None, height=30, spacing=10, padding=5
        )
        self.status_indicator_label = Label(text="Status: Ready", font_size=14, size_hint_x=0.3)
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
            self.status_bar_bg = Rectangle(pos=(0, Window.height - 30),
                                           size=(Window.width, 30))
        self.bind(size=self._update_status_bar_bg, pos=self._update_status_bar_bg)

        self.main_layout.add_widget(self.status_bar)

        # ----------------------------
        # Preset Buttons (Height = 40)
        # ----------------------------
        presets_layout = BoxLayout(size_hint_y=None, height=40, spacing=10, padding=5)
        self.btn_slow = Button(text="Slow")
        self.btn_slow.bind(on_press=lambda x: setattr(self.motor, 'set_speed_percent', 30))
        self.btn_medium = Button(text="Medium")
        self.btn_medium.bind(on_press=lambda x: setattr(self.motor, 'set_speed_percent', 60))
        self.btn_fast = Button(text="Fast")
        self.btn_fast.bind(on_press=lambda x: setattr(self.motor, 'set_speed_percent', 100))
        self.btn_manual = Button(text="Manual Ovr")
        self.btn_manual.bind(on_press=self.open_manual_override)

        presets_layout.add_widget(self.btn_slow)
        presets_layout.add_widget(self.btn_medium)
        presets_layout.add_widget(self.btn_fast)
        presets_layout.add_widget(self.btn_manual)

        self.main_layout.add_widget(presets_layout)

        # ----------------------------
        # Middle Area (Camera + Saw Status)
        # ----------------------------
        middle_layout = BoxLayout(orientation='horizontal')

        # Left: Camera preview (Image)
        self.img = Image(size_hint_x=0.7)
        middle_layout.add_widget(self.img)

        # Right: Saw Status Panel
        status_panel = BoxLayout(orientation='vertical', size_hint_x=0.3, padding=10, spacing=10)
        lbl_title = Label(text="Saw Status", font_size=20, size_hint_y=None, height=30)

        # Set Speed (display only)
        self.lbl_set_speed = Label(text="Set Speed: 0%", font_size=16, size_hint_y=None, height=25)

        # Set RPM (will blink if ≥2700 RPM > 10s)
        self.lbl_set_rpm = Label(text="Set RPM: 0", font_size=16, size_hint_y=None, height=25)

        # Current RPM (display only)
        self.lbl_current_rpm = Label(text="Current RPM: 0", font_size=16, size_hint_y=None, height=25)

        # Temperature (will blink if ≥50°C > 10s)
        self.lbl_temp = Label(text="Temperature: 0.0°C", font_size=16, size_hint_y=None, height=25)

        status_panel.add_widget(lbl_title)
        status_panel.add_widget(self.lbl_set_speed)
        status_panel.add_widget(self.lbl_set_rpm)
        status_panel.add_widget(self.lbl_current_rpm)
        status_panel.add_widget(self.lbl_temp)
        status_panel.add_widget(Label())  # Spacer

        middle_layout.add_widget(status_panel)
        self.main_layout.add_widget(middle_layout)

        # ----------------------------
        # Cutting Board Status Label (Height = 30)
        # ----------------------------
        self.status_label = Label(text="Cutting Board is Clear", font_size=20, size_hint_y=None, height=30)
        self.main_layout.add_widget(self.status_label)

        # ----------------------------
        # Bottom Row (Height = 40)
        # ----------------------------
        bottom_layout = BoxLayout(size_hint_y=None, height=BOTTOM_ROW_HEIGHT, spacing=10, padding=5)
        self.btn_emergency = Button(text="EMERGENCY STOP", background_color=(1, 0, 0, 1))
        self.btn_emergency.bind(on_press=self.activate_emergency)

        bottom_layout.add_widget(self.btn_emergency)

        # Spacer
        bottom_layout.add_widget(Label(size_hint_x=0.1))

        # Speed Display
        self.lbl_speed_display = Label(text="Speed: 0%", font_size=16, size_hint_x=0.2)
        bottom_layout.add_widget(self.lbl_speed_display)

        # Settings Button (for motion zone settings)
        self.btn_settings = Button(text="Settings", size_hint_x=0.2)
        self.btn_settings.bind(on_press=lambda x: setattr(self.manager, 'current', 'settings'))
        bottom_layout.add_widget(self.btn_settings)

        # Exit Button
        self.btn_exit = Button(text="Exit", size_hint_x=0.2)
        self.btn_exit.bind(on_press=self.stop_app)
        bottom_layout.add_widget(self.btn_exit)

        self.main_layout.add_widget(bottom_layout)

        # ----------------------------
        # Camera Setup
        # ----------------------------
        if platform.system() == 'Linux':
            # Attempt Linux Video4Linux2 backend
            self.capture = cv2.VideoCapture(0, cv2.CAP_V4L2)
        else:
            self.capture = cv2.VideoCapture(0)

        self.capture.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
        self.capture.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
        self.capture.set(cv2.CAP_PROP_FOURCC, CAMERA_FOURCC)
        if not self.capture.isOpened():
            print("[ERROR] Camera module failed to open.")

        # Zone / Detection Params (loaded from file)
        self.outer_width = 400
        self.outer_height = 300
        self.inner_width = 200
        self.inner_height = 200
        self.zone_offset_x = 0
        self.zone_offset_y = 0
        self.hatch_spacing = 20
        self.warning_duration = 3

        # Static overlay (hatches + outlines), recomputed only when zone params change
        self.overlay_dirty = True       # Mark that we need to build an overlay initially
        self.static_overlay = None      # Will hold a (H,W,3) BGR image

        # Warning timing
        self.last_motion_time = 0
        self.warning_text = ""
        self.flash_event = None
        self.flash_counter = 0

        # For frame differencing (downscaled)
        self.prev_frame = None

        # Keep track of manual override overlay so we can close it if needed
        self.manual_overlay = None

        # Track whether we’re currently in emergency‐stopped mode
        self.in_emergency = False

        # For half‐frame skip (to reduce CPU load)
        self.frame_toggle = False      ### ADDED: SKIP

        # Temperature & RPM “over‐count” timers
        self.temp_overcount = 0.0      ### ADDED
        self.rpm_overcount = 0.0       ### ADDED

        # Blink events
        self.temp_blink_event = None   ### ADDED
        self.rpm_blink_event = None    ### ADDED
        self.temp_blink_state = False  # False = white, True = red
        self.rpm_blink_state = False

        # Schedule updates at 15 FPS for camera + 1 Hz for status bar + 0.5 Hz for motor/status logic
        Clock.schedule_interval(self.update_frame, 1.0 / 15.0)
        Clock.schedule_interval(self.update_status_bar, 1.0)
        Clock.schedule_interval(self.update_motor_status, 0.5)

        self.load_settings_from_file()

    def _update_status_bar_bg(self, *args):
        # Keep status bar background rectangle in place
        self.status_bar_bg.pos = (0, self.height - 30)
        self.status_bar_bg.size = (self.width, 30)

    def update_status_bar(self, dt):
        # Update time & date
        t = time.localtime()
        self.time_label.text = time.strftime("Time: %H:%M:%S", t)
        self.date_label.text = time.strftime("Date: %d/%m/%Y", t)

        # Update WiFi SSID if on Linux
        ssid = "--"
        if platform.system() == 'Linux':
            try:
                ssid = subprocess.check_output(["iwgetid", "-r"]).decode().strip()
                if not ssid:
                    ssid = "--"
            except subprocess.CalledProcessError:
                ssid = "--"
        self.wifi_label.text = f"WiFi: {ssid}"

    def update_motor_status(self, dt):
        """
        Every 0.5 s:
         - Update motor simulator (rpm + temperature)
         - Update UI labels for speed/rpm/temperature
         - Check high‐temp & high‐rpm timers, start blinking/popups if needed
        """
        if self.in_emergency:
            # If in emergency state, we still want the motor logic to run so temperature can fluctuate,
            # but we don't do any UI warnings. Just call motor.update() and return.
            self.motor.update(dt)
            return

        # 1. Step motor simulator: adjusts current_rpm and temperature
        self.motor.update(dt)

        # 2. Update Saw Status Panel labels
        sp = self.motor.set_speed_percent
        sr = self.motor.set_rpm
        cr = int(self.motor.current_rpm)
        temp = self.motor.temperature

        self.lbl_set_speed.text = f"Set Speed: {sp}%"
        self.lbl_set_rpm.text = f"Set RPM: {sr}"
        self.lbl_current_rpm.text = f"Current RPM: {cr}"
        self.lbl_temp.text = f"Temperature: {temp:.1f}°C"
        self.lbl_speed_display.text = f"Speed: {sp}%"

        # 3. HIGH‐TEMPERATURE LOGIC (threshold ≥ 50 °C)
        if temp >= 50.0:
            self.temp_overcount += dt
            # At ≥ 10 s: start blinking and show pop‐up (if not already doing so)
            if self.temp_overcount >= 10.0 and self.temp_blink_event is None:
                # Start blinking the temperature label
                self.temp_blink_event = Clock.schedule_interval(self.blink_temp_label, 0.5)
                # Show pop‐up message once
                self.show_popup("Slow Saw Down to lower machine temperature")
            # At ≥ 30 s: force motor to 50% and show another pop‐up
            if self.temp_overcount >= 30.0:
                if self.motor.set_speed_percent != 50:
                    self.motor.set_speed_percent = 50
                    self.show_popup("Slowing Saw Down Due To Constant High Temperature")
        else:
            # If below 50 °C, reset counter and stop blinking
            self.temp_overcount = 0.0
            if self.temp_blink_event:
                self.temp_blink_event.cancel()
                self.temp_blink_event = None
                # Reset label color to white
                self.lbl_temp.color = (1, 1, 1, 1)

        # 4. HIGH‐RPM LOGIC (threshold ≥ 2700 RPM)
        if cr >= 2700:
            self.rpm_overcount += dt
            # At ≥ 10 s: start blinking RPM label and show pop‐up
            if self.rpm_overcount >= 10.0 and self.rpm_blink_event is None:
                self.rpm_blink_event = Clock.schedule_interval(self.blink_rpm_label, 0.5)
                self.show_popup("Slow Saw Down to save machine health")
            # At ≥ 30 s: force motor to 50% and show pop‐up
            if self.rpm_overcount >= 30.0:
                if self.motor.set_speed_percent != 50:
                    self.motor.set_speed_percent = 50
                    self.show_popup("Slowing Saw Down Due To Excess Speed Usage")
        else:
            # If below 2700 RPM, reset counter and stop blinking
            self.rpm_overcount = 0.0
            if self.rpm_blink_event:
                self.rpm_blink_event.cancel()
                self.rpm_blink_event = None
                # Reset label color to white
                self.lbl_set_rpm.color = (1, 1, 1, 1)

    def blink_temp_label(self, dt):
        """
        Toggle the temperature label’s color between white & red
        (called every 0.5 s while temp_blink_event is active).
        """
        if self.temp_blink_state:
            self.lbl_temp.color = (1, 1, 1, 1)  # white
        else:
            self.lbl_temp.color = (1, 0, 0, 1)  # red
        self.temp_blink_state = not self.temp_blink_state

    def blink_rpm_label(self, dt):
        """
        Toggle the RPM label’s color between white & red
        (called every 0.5 s while rpm_blink_event is active).
        """
        if self.rpm_blink_state:
            self.lbl_set_rpm.color = (1, 1, 1, 1)  # white
        else:
            self.lbl_set_rpm.color = (1, 0, 0, 1)  # red
        self.rpm_blink_state = not self.rpm_blink_state

    def show_popup(self, text):
        """
        Display a transient, centered pop-up with the given text.
        The pop-up auto-dismisses after 3 seconds.
        """
        # Create an overlay FloatLayout that covers the entire screen
        popup_overlay = FloatLayout(size=Window.size, size_hint=(None, None), pos=(0, 0))

        # Draw a semi‐transparent black background for the popup_overlay
        with popup_overlay.canvas:
            Color(0, 0, 0, 0.7)
            Rectangle(pos=(0, 0), size=Window.size)

        # Compute label rectangle dimensions
        lbl_width = Window.width * 0.8
        lbl_height = 50
        lbl_x = (Window.width - lbl_width) / 2
        lbl_y = (Window.height - lbl_height) / 2

        # Draw a dark gray rectangle behind where the label will go
        with popup_overlay.canvas:
            Color(0.1, 0.1, 0.1, 0.9)
            Rectangle(pos=(lbl_x, lbl_y), size=(lbl_width, lbl_height))

        # Now create the Label itself
        lbl = Label(
            text=text,
            font_size=20,
            size_hint=(None, None),
            size=(lbl_width, lbl_height),
            pos=(lbl_x, lbl_y),
            color=(1, 1, 1, 1),
            halign='center',
            valign='middle'
        )
        # Force the label text to be centered
        lbl.text_size = (lbl_width, lbl_height)
        popup_overlay.add_widget(lbl)

        # Add to root_layout
        self.root_layout.add_widget(popup_overlay)

        # Schedule its removal in 3 seconds
        Clock.schedule_once(lambda dt: self._dismiss_popup(popup_overlay), 3.0)

    def _dismiss_popup(self, popup_overlay):
        if popup_overlay in self.root_layout.children:
            self.root_layout.remove_widget(popup_overlay)

    def compute_zones(self, frame_width, frame_height):
        cx = frame_width // 2 + self.zone_offset_x
        cy = frame_height // 2 + self.zone_offset_y

        # Outer trapezoid
        top_left = (int(cx - self.outer_width / 2), int(cy + self.outer_height / 2))
        top_right = (int(cx + self.outer_width / 2), int(cy + self.outer_height / 2))
        bottom_left = (int(cx - self.outer_width / 4), int(cy - self.outer_height / 2))
        bottom_right = (int(cx + self.outer_width / 4), int(cy - self.outer_height / 2))
        self.outer_zone = np.array([top_left, top_right, bottom_right, bottom_left])

        # Inner trapezoid
        inner_top_left = (int(cx - self.inner_width / 2), int(cy + self.inner_height / 2))
        inner_top_right = (int(cx + self.inner_width / 2), int(cy + self.inner_height / 2))
        inner_bottom_left = (int(cx - self.inner_width / 4), int(cy - self.inner_height / 2))
        inner_bottom_right = (int(cx + self.inner_width / 4), int(cy - self.inner_height / 2))
        self.inner_zone = np.array(
            [inner_top_left, inner_top_right, inner_bottom_right, inner_bottom_left]
        )

    def generate_static_overlay(self, frame_width, frame_height):
        """
        Build (and cache) a single overlay image that contains:
        - Hatched lines *outside* the outer zone (inverted)
        - Green outline for the outer zone
        - Red outline for the inner zone
        """
        # 1. Recompute zone polygons
        self.compute_zones(frame_width, frame_height)

        # 2. Create a full‐size blank image (black background)
        overlay = np.zeros((frame_height, frame_width, 3), dtype=np.uint8)

        # 3. Create a mask for the outer zone (white inside outer, black outside)
        mask_outer = np.zeros((frame_height, frame_width), dtype=np.uint8)
        cv2.fillPoly(mask_outer, [self.outer_zone], 255)

        # 4. Draw hatched (diagonal) lines across entire image into temp_overlay
        temp_overlay = np.zeros_like(overlay)
        for i in range(-frame_height, frame_width, self.hatch_spacing):
            # Draw line from (i,0) to (i + frame_height, frame_height)
            cv2.line(temp_overlay, (i, 0), (i + frame_height, frame_height), (255, 255, 255), 1)

        # 5. Keep hatched lines where mask_outer == 0 (outside the outer zone)
        hatched_outside = np.zeros_like(overlay)
        hatched_outside[mask_outer == 0] = temp_overlay[mask_outer == 0]

        # 6. Draw the zone outlines onto hatched_outside
        cv2.polylines(hatched_outside, [self.outer_zone], True, (0, 255, 0), 2)   # green outer
        cv2.polylines(hatched_outside, [self.inner_zone], True, (0, 0, 255), 2)   # red inner

        # 7. Store this static overlay and mark dirty = False
        self.static_overlay = hatched_outside
        self.overlay_dirty = False

    def update_frame(self, dt):
        if self.in_emergency:
            # If we are in emergency state, skip camera updates entirely
            return

        ret, frame = self.capture.read()
        if not ret:
            return

        frame_height, frame_width = frame.shape[:2]

        # Re-build static overlay if zone parameters changed
        if self.overlay_dirty or self.static_overlay is None:
            self.generate_static_overlay(frame_width, frame_height)

        # SKIP LOGIC: toggle every frame so we only do full detection/overlay on alternate frames
        self.frame_toggle = not self.frame_toggle   ### ADDED: SKIP
        do_full_process = self.frame_toggle          ### ADDED: SKIP

        if do_full_process:
            draw_frame = frame.copy()
            # 1. Apply cached static overlay (inverted hatching + outlines)
            if self.static_overlay is not None:
                mask_nonzero = np.any(self.static_overlay != [0, 0, 0], axis=2)
                draw_frame[mask_nonzero] = self.static_overlay[mask_nonzero]

            # 2. HAND DETECTION (full resolution)
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
                    hand_detected = True
                    break

            # 3. MOTION DETECTION (downscaled to 320×240)
            if not hand_detected:
                small = cv2.resize(frame, (320, 240))
                gray_small = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                blur_small = cv2.GaussianBlur(gray_small, (5, 5), 0)   ### Smaller kernel

                if self.prev_frame is None:
                    self.prev_frame = blur_small
                    # Show the first frame (no detection bounding boxes yet)
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
                    # Scale MIN_MOTION_AREA by pixel ratio (320×240 vs. 640×480 = 1/4 area)
                    scaled_min_area = MIN_MOTION_AREA * (320 * 240) / (640 * 480)
                    if area < scaled_min_area:
                        continue
                    x_s, y_s, w_s, h_s = cv2.boundingRect(contour)
                    # Scale back up to full resolution: factor of 2 in each axis
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
                        break
                    elif cv2.pointPolygonTest(self.outer_zone, (cx, cy), False) >= 0:
                        cv2.rectangle(draw_frame, (x, y), (x + w, y + h), (0, 255, 255), 2)
                        motion_warning = "[color=ff0]Moving Item NEAR Cutting Board Area[/color]"
                        self.last_motion_time = time.time()
                        break
                    else:
                        cv2.rectangle(draw_frame, (x, y), (x + w, y + h), (255, 255, 255), 2)

                if motion_warning:
                    self.warning_text = motion_warning

                self.prev_frame = blur_small

            # 4. Display on Kivy Image
            buf = cv2.flip(draw_frame, 0).tobytes()
            if not self.img.texture:
                self.img.texture = Texture.create(size=(frame_width, frame_height), colorfmt='bgr')
            self.img.texture.blit_buffer(buf, colorfmt='bgr', bufferfmt='ubyte')

        else:
            # ### SKIP DETAILED PROCESSING: just display raw camera + overlay
            draw_frame = frame.copy()
            if self.static_overlay is not None:
                mask_nonzero = np.any(self.static_overlay != [0, 0, 0], axis=2)
                draw_frame[mask_nonzero] = self.static_overlay[mask_nonzero]
            buf = cv2.flip(draw_frame, 0).tobytes()
            if not self.img.texture:
                self.img.texture = Texture.create(size=(frame_width, frame_height), colorfmt='bgr')
            self.img.texture.blit_buffer(buf, colorfmt='bgr', bufferfmt='ubyte')
            # Note: we do NOT update prev_frame here; it remains from last full pass.

        # 5. Update cutting board status label
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

    def activate_emergency(self, instance):
        """
        When Emergency Stop is pressed:
         1. Close any manual override overlay
         2. Zero out motor speed and RPM
         3. Unschedule camera/status/motor updates
         4. Show full-screen red emergency screen
        """
        # 1. Close manual override if open
        if self.manual_overlay:
            self.close_manual_override()

        # 2. Immediately stop the motor
        self.motor.set_speed_percent = 0
        self.motor.current_rpm = 0

        # 3. Unschedule everything
        self.in_emergency = True
        Clock.unschedule(self.update_frame)
        Clock.unschedule(self.update_status_bar)
        Clock.unschedule(self.update_motor_status)
        if self.flash_event:
            self.flash_event.cancel()

        # 4. Clear main layout & root_layout
        self.main_layout.clear_widgets()
        self.root_layout.clear_widgets()
        self.root_layout.canvas.before.clear()

        # 5. Draw solid red background
        with self.root_layout.canvas.before:
            Color(1, 0, 0, 1)
            self.emergency_bg = Rectangle(pos=self.pos, size=self.size)
        self.bind(size=self._update_emergency_bg, pos=self._update_emergency_bg)

        # 6. Add Emergency labels
        emergency_layout = BoxLayout(orientation='vertical')
        stop_label = Label(
            text="[b][color=ffffff]EMERGENCY STOP INITIATED[/color][/b]",
            font_size=32,
            markup=True,
        )
        reset_label = Label(text="Touch Anywhere to Reset", font_size=20)
        emergency_layout.add_widget(stop_label)
        emergency_layout.add_widget(reset_label)
        self.root_layout.add_widget(emergency_layout)

        self.reset_label = reset_label
        self.root_layout.bind(on_touch_down=self.reset_from_emergency)

    def _update_emergency_bg(self, *args):
        self.emergency_bg.pos = self.pos
        self.emergency_bg.size = self.size

    def reset_from_emergency(self, *args):
        """
        When user touches anywhere, re‐initialize the UI:
         - Release camera
         - Clear everything
         - Call __init__() again
        """
        self.root_layout.unbind(on_touch_down=self.reset_from_emergency)
        self.reset_label.text = "Resetting, please wait..."
        Clock.schedule_once(self.do_reset_after_delay, 0.5)

    def do_reset_after_delay(self, dt):
        if self.capture.isOpened():
            self.capture.release()

        self.root_layout.clear_widgets()
        self.root_layout.canvas.before.clear()

        # Reset internal flags and counters
        self.prev_frame = None
        self.overlay_dirty = True
        self.in_emergency = False
        self.frame_toggle = False
        self.temp_overcount = 0.0
        self.rpm_overcount = 0.0
        if self.temp_blink_event:
            self.temp_blink_event.cancel()
            self.temp_blink_event = None
            self.lbl_temp.color = (1, 1, 1, 1)
        if self.rpm_blink_event:
            self.rpm_blink_event.cancel()
            self.rpm_blink_event = None
            self.lbl_set_rpm.color = (1, 1, 1, 1)

        # Re‐initialize the entire screen
        self.__init__()
        self.manager.current = 'live'

    def open_manual_override(self, instance):
        """
        Create a 'manual override' overlay that dims everything above the bottom bar (except the bottom bar),
        shows a white‐outlined box (60% width × 40% height) with:
         - “MANUAL SAW SPEED” label
         - A slider (0–100)
         - The current percentage below it
         - A “Done” button
        The bottom bar (EMERGENCY STOP, etc.) remains visible & clickable.
        """
        # 1. If in emergency, do nothing
        if self.in_emergency:
            return

        # 2. If overlay already exists, remove it
        if self.manual_overlay:
            self.close_manual_override()

        # 3. Compute overlay dims (cover full width, from y=BOTTOM_ROW_HEIGHT up)
        overlay_height = Window.height - BOTTOM_ROW_HEIGHT
        overlay = FloatLayout(
            size=(Window.width, overlay_height),
            size_hint=(None, None),
            pos=(0, BOTTOM_ROW_HEIGHT)
        )

        with overlay.canvas:
            # 4. Darken background
            Color(0, 0, 0, 0.7)
            Rectangle(pos=(0, 0), size=(Window.width, overlay_height))

            # 5. Draw dark gray interior (60% × 40%) + white border
            box_width = Window.width * 0.6       ### CHANGED: smaller
            box_height = overlay_height * 0.4    ### CHANGED: smaller
            box_x = (Window.width - box_width) / 2
            box_y = (overlay_height - box_height) / 2

            Color(0.1, 0.1, 0.1, 0.9)
            Rectangle(pos=(box_x, box_y), size=(box_width, box_height))

            Color(1, 1, 1, 1)
            Line(rectangle=(box_x, box_y, box_width, box_height), width=2)

        # 6. Place content (label + slider + percentage + done)
        content = BoxLayout(
            orientation='vertical',
            size=(box_width * 0.9, box_height * 0.7),
            size_hint=(None, None)
        )
        content.pos = (
            box_x + (box_width * 0.05),
            BOTTOM_ROW_HEIGHT + box_y + (box_height * 0.15)
        )
        content.spacing = 10
        content.padding = 10

        # 6a. “MANUAL SAW SPEED” label
        self.manual_title = Label(
            text="MANUAL SAW SPEED",
            font_size=20,
            size_hint_y=None,
            height=30
        )
        content.add_widget(self.manual_title)

        # 6b. Current percentage label
        val = self.motor.set_speed_percent
        self.manual_label = Label(text=f"{val}%", font_size=24, size_hint_y=None, height=40)

        # 6c. Slider
        self.manual_slider = Slider(min=0, max=100, value=val, size_hint_y=None, height=40)
        self.manual_slider.bind(value=self.on_manual_slider)

        content.add_widget(self.manual_label)
        content.add_widget(self.manual_slider)

        # 6d. Done button
        done_btn = Button(text="Done", size_hint_y=None, height=40)
        done_btn.bind(on_press=lambda x: self.close_manual_override())
        content.add_widget(done_btn)

        overlay.add_widget(content)

        # 7. Keep reference so we can remove it
        self.manual_overlay = overlay
        self.root_layout.add_widget(overlay)

    def on_manual_slider(self, slider, val):
        # Update motor speed & label in real time
        v = int(val)
        self.motor.set_speed_percent = v
        self.manual_label.text = f"{v}%"

    def close_manual_override(self):
        # Remove the overlay
        if self.manual_overlay:
            self.root_layout.remove_widget(self.manual_overlay)
            self.manual_overlay = None

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
        with open(tmp_path, 'w') as f:
            json.dump(settings, f)
        os.replace(tmp_path, SETTINGS_FILE)

    def load_settings_from_file(self):
        if not os.path.exists(SETTINGS_FILE):
            return
        try:
            with open(SETTINGS_FILE, 'r') as f:
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

        # Any time we load new settings, mark overlay_dirty so it re‐draws with new zones
        self.overlay_dirty = True

    def stop_app(self, *args):
        if self.capture.isOpened():
            self.capture.release()
        App.get_running_app().stop()

    def on_stop(self):
        if self.capture.isOpened():
            self.capture.release()


class SettingsScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        layout = BoxLayout(orientation='vertical')

        # Preview Image
        self.preview = Image(size_hint_y=0.5)
        layout.add_widget(self.preview)

        # Scrollable sliders
        scroll = ScrollView()
        sliders_container = BoxLayout(orientation='vertical', size_hint_y=None)
        sliders_container.bind(minimum_height=sliders_container.setter('height'))

        self.sliders = {}

        def create_slider(label_text, min_val, max_val, default_val):
            h = BoxLayout(orientation='horizontal', size_hint_y=None, height=40, padding=5)
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

        # Fetch current values from LiveViewScreen
        live = self.manager.get_screen('live') if self.manager else None
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

        btn_row = BoxLayout(size_hint_y=None, height=40, spacing=10, padding=10)
        save_btn = Button(text="Save Settings")
        save_btn.bind(on_press=lambda x: self.manager.get_screen('live').save_settings_to_file())
        load_btn = Button(text="Load Settings")
        load_btn.bind(on_press=lambda x: self.load_from_live())
        btn_row.add_widget(save_btn)
        btn_row.add_widget(load_btn)
        layout.add_widget(btn_row)

        back_btn = Button(text="Back to Live View", size_hint_y=None, height=40)
        back_btn.bind(on_press=lambda x: setattr(self.manager, 'current', 'live'))
        layout.add_widget(back_btn)

        self.add_widget(layout)
        Clock.schedule_interval(self.update_preview, 1.0 / 15.0)

    def load_from_live(self):
        live = self.manager.get_screen('live')
        self.sliders["Outer Width"].value = live.outer_width
        self.sliders["Outer Height"].value = live.outer_height
        self.sliders["Inner Width"].value = live.inner_width
        self.sliders["Inner Height"].value = live.inner_height
        self.sliders["Offset X"].value = live.zone_offset_x
        self.sliders["Offset Y"].value = live.zone_offset_y
        self.sliders["Hatch Spacing"].value = live.hatch_spacing
        self.sliders["Warning Duration"].value = live.warning_duration

    def update_settings(self):
        live = self.manager.get_screen('live')
        live.outer_width = int(self.sliders["Outer Width"].value)
        live.outer_height = int(self.sliders["Outer Height"].value)
        live.inner_width = int(self.sliders["Inner Width"].value)
        live.inner_height = int(self.sliders["Inner Height"].value)
        live.zone_offset_x = int(self.sliders["Offset X"].value)
        live.zone_offset_y = int(self.sliders["Offset Y"].value)
        live.hatch_spacing = int(self.sliders["Hatch Spacing"].value)
        live.warning_duration = int(self.sliders["Warning Duration"].value)

        # Mark overlay_dirty so LiveViewScreen knows to rebuild the static overlay
        live.overlay_dirty = True

    def update_preview(self, dt):
        live = self.manager.get_screen('live')
        frame = live.prev_frame if live.prev_frame is not None else None
        if frame is None:
            return
        preview_frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        h, w = frame.shape[:2]
        # Temporarily recompute zones so that we can draw a small preview
        live.compute_zones(w, h)
        preview_hatch = preview_frame.copy()

        # Draw hatched lines outside the outer zone for the preview
        mask_outer = np.zeros((h, w), dtype=np.uint8)
        cv2.fillPoly(mask_outer, [live.outer_zone], 255)
        for i in range(-h, w, live.hatch_spacing):
            x1, y1 = i, 0
            x2, y2 = i + h, h
            cv2.line(preview_hatch, (x1, y1), (x2, y2), (255, 255, 255), 1)
        preview_frame[mask_outer == 0] = preview_hatch[mask_outer == 0]

        # Draw zone outlines
        cv2.polylines(preview_frame, [live.outer_zone], True, (0, 255, 0), 2)
        cv2.polylines(preview_frame, [live.inner_zone], True, (0, 0, 255), 2)

        buf = cv2.flip(preview_frame, 0).tobytes()
        if not self.preview.texture:
            self.preview.texture = Texture.create(size=(w, h), colorfmt='bgr')
        self.preview.texture.blit_buffer(buf, colorfmt='bgr', bufferfmt='ubyte')


class MotionApp(App):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.motor = MotorSimulator()

    def build(self):
        # Schedule the motor simulator update every 0.5 s
        Clock.schedule_interval(self.motor.update, 0.5)
        sm = ScreenManager()
        sm.add_widget(SplashScreen(name='splash'))
        sm.add_widget(LiveViewScreen(name='live'))
        sm.add_widget(SettingsScreen(name='settings'))
        return sm

    def on_stop(self):
        live = self.root.get_screen('live')
        if hasattr(live, 'capture') and live.capture.isOpened():
            live.capture.release()


if __name__ == '__main__':
    Window.size = (800, 480)
    Window.fullscreen = False
    Window.borderless = False
    MotionApp().run()
