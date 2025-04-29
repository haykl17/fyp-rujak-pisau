from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.image import Image
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.clock import Clock
from kivy.graphics.texture import Texture
from kivy.graphics import Color, Rectangle
from kivy.core.window import Window
from kivy.uix.screenmanager import ScreenManager, Screen, FadeTransition
from kivy.uix.slider import Slider
import cv2
import numpy as np
import time
import json
import os

shared_frame = None  # Shared buffer for camera frame
SETTINGS_FILE = "motion_settings.json"

class LiveViewScreen(Screen):
    flash_counter = 0
    flash_event = None
    def __init__(self, manager_ref, **kwargs):
        super().__init__(**kwargs)
        self.manager_ref = manager_ref
        self.layout = BoxLayout(orientation='vertical', padding=10, spacing=10)

        # Status bar (menu bar at the top)
        self.status_bar = BoxLayout(orientation='horizontal', size_hint_y=None, height=30, spacing=10, padding=5)
        self.machine_status = "Ready"
        self.status_indicator_label = Label(text="Status: Ready", font_size=14, size_hint_x=0.4)
        self.battery_label = Label(text="Battery: --%", font_size=14, size_hint_x=0.3)
        self.wifi_label = Label(text="WiFi: --", font_size=14, size_hint_x=0.3)
        self.time_label = Label(text="Time: --:--", font_size=14, size_hint_x=0.4)

        with self.status_bar.canvas.before:
            Color(0.15, 0.15, 0.15, 1)
            self.status_bar_bg = Rectangle(size=self.status_bar.size, pos=self.status_bar.pos)
            self.status_bar.bind(size=self._update_status_bar_bg, pos=self._update_status_bar_bg)
            self.status_bar.add_widget(self.status_indicator_label)
            self.status_bar.add_widget(self.battery_label)
            self.status_bar.add_widget(self.wifi_label)
            self.status_bar.add_widget(self.time_label)
        self.date_label = Label(text="Date: --/--/----", font_size=14, size_hint_x=0.5)
        self.status_bar.add_widget(self.date_label)

        # Top filler (empty)
        self.top_filler = BoxLayout(size_hint_y=0.7)

        # Middle row: camera view positioned bottom right
        self.middle_row = BoxLayout(orientation='horizontal', size_hint_y=0.25)
        self.middle_row.add_widget(Label(size_hint_x=0.75))  # Spacer to push camera right
        self.camera_view = Image(allow_stretch=True, keep_ratio=True, size_hint=(0.25, 1))
        self.middle_row.add_widget(self.camera_view)

        with self.canvas.before:
            self.bg_color_instruction = Color(0.1, 0.2, 0.3, 1)
            self.bg_rect = Rectangle(size=self.size, pos=self.pos)
        self.bind(size=self._update_rect, pos=self._update_rect)

        self.status_label = Label(text="Cutting Board is Clear", font_size=18, color=(1, 1, 1, 1), size_hint_y=None, height=40)
        self.button_panel = BoxLayout(size_hint_y=None, height=50, spacing=10, padding=5)

        self.emergency_btn = Button(text="EMERGENCY STOP", background_color=(1, 0, 0, 1), font_size=20, bold=True)
        self.emergency_btn.bind(on_press=self.activate_emergency)
        self.settings_btn = Button(text="Settings", background_color=(0.2, 0.6, 1, 1), bold=True)
        self.settings_btn.bind(on_press=self.go_to_settings)
        self.exit_btn = Button(text="Exit", background_color=(0.5, 0.5, 0.5, 1), on_press=self.stop_app)

        self.button_panel.add_widget(self.emergency_btn)
        self.button_panel.add_widget(Label())
        self.button_panel.add_widget(self.settings_btn)
        self.button_panel.add_widget(self.exit_btn)

        self.layout.add_widget(self.status_bar)
        self.layout.add_widget(self.top_filler)
        self.layout.add_widget(self.middle_row)
        self.layout.add_widget(self.status_label)
        self.layout.add_widget(self.button_panel)
        self.add_widget(self.layout)

                # Try normal USB camera first
        self.capture = cv2.VideoCapture(0)
        time.sleep(2)
        ret, frame = self.capture.read()
        if not ret or frame is None:
            # Try using GStreamer pipeline (for Pi Camera)
            self.capture = cv2.VideoCapture(
                "libcamerasrc ! video/x-raw,width=640,height=480,format=YUY2 ! videoconvert ! appsink",
                cv2.CAP_GSTREAMER
            )
            time.sleep(2)
            ret, frame = self.capture.read()
        ret, frame = self.capture.read()
        if ret and frame is not None:
            self.height, self.width, _ = frame.shape
        else:
            self.height = 480
            self.width = 640

        self.prev_frame = None
        self.last_motion_time = 0
        self.warning_duration = 3
        self.hatch_spacing = 20
        self.warning_text = ""
        self.text_color = (1, 1, 1, 1)

        self.outer_width = 400
        self.outer_height = 300
        self.inner_width = 200
        self.inner_height = 200
        self.zone_offset_y = 0
        self.zone_offset_x = 0

        self.load_settings_from_file()
        self.update_zones()
        Clock.schedule_interval(self.update_frame, 1.0 / 30)
        Clock.schedule_interval(self.update_status_bar, 1.0)

    def _update_status_bar_bg(self, *args):
        self.status_bar_bg.size = self.status_bar.size
        self.status_bar_bg.pos = self.status_bar.pos

    def _update_rect(self, instance, value):
        self.bg_rect.size = instance.size
        self.bg_rect.pos = instance.pos

    def go_to_settings(self, instance):
        self.manager_ref.current = 'settings'

    def update_status_bar(self, dt):
        current_time = time.strftime("%H:%M:%S")
        self.time_label.text = f"Time: {current_time}"
        current_date = time.strftime("%d/%m/%Y")
        self.date_label.text = f"Date: {current_date}"
        self.status_indicator_label.text = f"Status: {self.machine_status}"
        
        # Dummy updates for battery and WiFi (replace with real Raspberry Pi functions later)
        self.battery_label.text = "Battery: 100%"
        self.wifi_label.text = "WiFi: Connected"

    def stop_app(self, *args):
        self.save_settings_to_file()
        self.capture.release()
        App.get_running_app().stop()

    def update_zones(self):
        frame_width = 640
        frame_height = 480
        if self.capture.isOpened():
            ret, frame = self.capture.read()
            if ret and frame is not None:
                frame_height, frame_width, _ = frame.shape

        cx = frame_width // 2 + self.zone_offset_x
        cy = frame_height // 2 + self.zone_offset_y

        self.outer_zone = np.array([
            [cx - self.outer_width // 2, cy + self.outer_height // 2],
            [cx + self.outer_width // 2, cy + self.outer_height // 2],
            [cx + self.outer_width // 4, cy - self.outer_height // 2],
            [cx - self.outer_width // 4, cy - self.outer_height // 2]
        ], np.int32).reshape((-1, 1, 2))

        self.inner_zone = np.array([
            [cx - self.inner_width // 2, cy + self.inner_height // 2],
            [cx + self.inner_width // 2, cy + self.inner_height // 2],
            [cx + self.inner_width // 4, cy - self.inner_height // 2],
            [cx - self.inner_width // 4, cy - self.inner_height // 2]
        ], np.int32).reshape((-1, 1, 2))

    def draw_hatch_lines(self, frame, zone, spacing):
        mask = np.zeros(frame.shape[:2], dtype=np.uint8)
        cv2.fillPoly(mask, [zone], 255)
        hatch_frame = frame.copy()
        for i in range(-int(self.height), int(self.width), int(spacing)):
            pt1 = (int(i), 0)
            pt2 = (int(i + self.height), int(self.height))
            cv2.line(hatch_frame, pt1, pt2, (255, 255, 255), 1)
        hatch_frame[mask == 255] = frame[mask == 255]
        return hatch_frame

    def update_frame(self, dt):
        global shared_frame
        self.update_zones()
        ret, frame = self.capture.read()
        if not ret:
            return

        shared_frame = frame.copy()
        current_time = time.time()
        hand_detected = False

        # Hand Detection based on skin color
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        lower_skin = np.array([0, 20, 70], dtype=np.uint8)
        upper_skin = np.array([20, 255, 255], dtype=np.uint8)
        mask = cv2.inRange(hsv, lower_skin, upper_skin)
        contours_hand, _ = cv2.findContours(mask, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

        for cnt in contours_hand:
            if cv2.contourArea(cnt) < 3000:
                continue
            x, y, w, h = cv2.boundingRect(cnt)
            cx = x + w // 2
            cy = y + h // 2
            inside_red = cv2.pointPolygonTest(self.inner_zone, (cx, cy), False)
            if inside_red >= 0:
                hand_detected = True
                if self.flash_event is None:
                    self.flash_counter = 6
                    self.flash_event = Clock.schedule_interval(self.flash_background, 0.2)
                self.warning_text = "WARNING: HAND DETECTED IN CUTTING AREA"
                self.text_color = (1, 1, 0, 1)
                self.status_label.font_size = 30
                self.last_motion_time = current_time
                break

        if not hand_detected:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            blur = cv2.GaussianBlur(gray, (5, 5), 0)

            if self.prev_frame is None:
                self.prev_frame = blur
            else:
                diff = cv2.absdiff(self.prev_frame, blur)
                _, thresh = cv2.threshold(diff, 20, 255, cv2.THRESH_BINARY)
                dilated = cv2.dilate(thresh, None, iterations=3)
                contours, _ = cv2.findContours(dilated, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

                for contour in contours:
                    if cv2.contourArea(contour) < 1000:
                        continue
                    x, y, w, h = cv2.boundingRect(contour)
                    cx = x + w // 2
                    cy = y + h // 2

                    inside_red = cv2.pointPolygonTest(self.inner_zone, (cx, cy), False)
                    inside_yellow = cv2.pointPolygonTest(self.outer_zone, (cx, cy), False)

                    if inside_red >= 0:
                        self.warning_text = "WARNING: Moving Item IN Cutting Board Area"
                        self.text_color = (1, 0, 0, 1)
                        self.last_motion_time = time.time()
                        box_color = (0, 0, 255)
                    elif inside_yellow >= 0:
                        self.warning_text = "WARNING: Moving Item NEAR Cutting Board Area"
                        self.text_color = (1, 1, 0, 1)
                        self.last_motion_time = time.time()
                        box_color = (0, 255, 255)
                    else:
                        box_color = (255, 255, 255)

                    cv2.rectangle(frame, (x, y), (x + w, y + h), box_color, 2)

                self.prev_frame = blur

        # Draw hatchlines and zones
        frame = self.draw_hatch_lines(frame, self.outer_zone, self.hatch_spacing)
        cv2.polylines(frame, [self.outer_zone], isClosed=True, color=(0, 255, 255), thickness=2)
        cv2.polylines(frame, [self.inner_zone], isClosed=True, color=(0, 0, 255), thickness=2)

        # Flip and display frame
        buf = cv2.flip(frame, 0).tobytes()
        texture = Texture.create(size=(frame.shape[1], frame.shape[0]), colorfmt='bgr')
        texture.blit_buffer(buf, colorfmt='bgr', bufferfmt='ubyte')
        self.camera_view.texture = texture

        # Update status label
        if current_time - self.last_motion_time <= self.warning_duration:
            self.status_label.text = self.warning_text
            self.status_label.color = self.text_color
        else:
            self.status_label.text = "Cutting Board is Clear"
            self.status_label.color = (1, 1, 1, 1)
            self.status_label.font_size = 18

        


    def flash_background(self, dt):
        if self.flash_counter > 0:
            if self.flash_counter % 2 == 0:
                self.bg_color_instruction.rgba = (1, 0, 0, 1)  # Flash red
                self.status_label.color = (0, 0, 0, 1)
            else:
                self.bg_color_instruction.rgba = (0.1, 0.2, 0.3, 1)  # Normal blue
                self.status_label.color = (1, 1, 1, 1)
            self.flash_counter -= 1
        else:
            if self.flash_event:
                self.flash_event.cancel()
                self.flash_event = None
            self.bg_color_instruction.rgba = (0.1, 0.2, 0.3, 1)
            self.status_label.color = (1, 1, 1, 1)
            self.flash_counter -= 1
        


    def activate_emergency(self, instance):
        self.clear_widgets()

        with self.canvas.before:
            self.emergency_bg_color = Color(1, 0, 0, 1)
            self.emergency_bg_rect = Rectangle(size=self.size, pos=self.pos)

        self.bind(size=self._update_emergency_bg, pos=self._update_emergency_bg)

        self.emergency_layout = BoxLayout(orientation='vertical')
        self.emergency_label = Label(text="EMERGENCY STOP INITIATED", font_size=40, bold=True, color=(1, 1, 0, 1))
        self.reset_label = Label(text="Touch Anywhere to Reset", font_size=20, color=(1, 1, 0, 1))
        self.emergency_layout.add_widget(self.emergency_label)
        self.emergency_layout.add_widget(self.reset_label)
        self.add_widget(self.emergency_layout)
        self.bind(on_touch_down=self.reset_from_emergency)

    def _update_emergency_bg(self, *args):
        if hasattr(self, 'emergency_bg_rect'):
            self.emergency_bg_rect.size = self.size
            self.emergency_bg_rect.pos = self.pos

    def reset_from_emergency(self, *args):
        self.unbind(on_touch_down=self.reset_from_emergency)
        if hasattr(self, 'reset_label'):
            self.reset_label.text = "Resetting, please wait..."
        Clock.schedule_once(self.do_reset_after_delay, 0.5)

    def do_reset_after_delay(self, dt):
        self.clear_widgets()
        self.__init__(self.manager_ref)


    def save_settings_to_file(self):
        settings = {
            "outer_width": self.outer_width,
            "outer_height": self.outer_height,
            "inner_width": self.inner_width,
            "inner_height": self.inner_height,
            "zone_offset_x": self.zone_offset_x,
            "zone_offset_y": self.zone_offset_y,
            "hatch_spacing": self.hatch_spacing,
            "warning_duration": self.warning_duration
        }
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(settings, f)

    def load_settings_from_file(self):
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r') as f:
                settings = json.load(f)
                self.outer_width = settings.get("outer_width", 400)
                self.outer_height = settings.get("outer_height", 300)
                self.inner_width = settings.get("inner_width", 200)
                self.inner_height = settings.get("inner_height", 200)
                self.zone_offset_x = settings.get("zone_offset_x", 0)
                self.zone_offset_y = settings.get("zone_offset_y", 0)
                self.hatch_spacing = settings.get("hatch_spacing", 20)
                self.warning_duration = settings.get("warning_duration", 3)
class SettingsScreen(Screen):
    def __init__(self, manager_ref, **kwargs):
        super().__init__(**kwargs)
        self.manager_ref = manager_ref
        self.layout = BoxLayout(orientation='vertical')

        with self.canvas.before:
            Color(0.1, 0.2, 0.3, 1)
            self.bg_rect = Rectangle(size=self.size, pos=self.pos)
        self.bind(size=self._update_rect, pos=self._update_rect)

        self.preview = Image(size_hint_y=0.5)
        self.layout.add_widget(self.preview)

        self.sliders = {
            'Outer Width': Slider(min=100, max=600, value=400),
            'Outer Height': Slider(min=100, max=400, value=300),
            'Inner Width': Slider(min=50, max=400, value=200),
            'Inner Height': Slider(min=50, max=300, value=200),
            'Offset Y': Slider(min=-200, max=200, value=0),
            'Offset X': Slider(min=-300, max=300, value=0),
            'Hatch Spacing': Slider(min=5, max=100, value=20),
            'Warning Duration': Slider(min=1, max=10, value=3)
        }

        Clock.schedule_once(self.get_live_screen)
        Clock.schedule_interval(self.update_preview, 1.0 / 15)

        for label_text, slider in self.sliders.items():
            box = BoxLayout(orientation='horizontal', size_hint_y=None, height=40)
            label = Label(text=label_text, size_hint_x=0.4)
            value_label = Label(text=str(int(slider.value)), size_hint_x=0.2)
            slider.bind(value=lambda s, val, lbl=value_label: lbl.setter('text')(lbl, f"{int(val)}"))
            slider.bind(value=self.update_settings)
            box.add_widget(label)
            box.add_widget(slider)
            box.add_widget(value_label)
            self.layout.add_widget(box)

        button_row = BoxLayout(size_hint_y=None, height=50)
        save_btn = Button(text="Save Settings")
        save_btn.bind(on_press=self.save_settings)
        load_btn = Button(text="Load Settings")
        load_btn.bind(on_press=self.load_settings)
        button_row.add_widget(save_btn)
        button_row.add_widget(load_btn)
        self.layout.add_widget(button_row)

        back_btn = Button(text="Back to Live View", size_hint_y=None, height=50)
        back_btn.bind(on_press=self.go_to_live)
        self.layout.add_widget(back_btn)
        self.add_widget(self.layout)

    def _update_rect(self, instance, value):
        self.bg_rect.size = instance.size
        self.bg_rect.pos = instance.pos

    def go_to_live(self, instance):
        self.manager_ref.current = 'live'

    def get_live_screen(self, dt):
        self.live_screen = self.manager_ref.get_screen('live')

    def update_preview(self, dt):
        global shared_frame
        if hasattr(self, 'live_screen') and shared_frame is not None:
            frame = shared_frame.copy()
            self.live_screen.update_zones()
            preview = self.live_screen.draw_hatch_lines(frame.copy(), self.live_screen.outer_zone, self.live_screen.hatch_spacing)
            cv2.polylines(preview, [self.live_screen.outer_zone], isClosed=True, color=(0, 255, 255), thickness=2)
            cv2.polylines(preview, [self.live_screen.inner_zone], isClosed=True, color=(0, 0, 255), thickness=2)
            buf = cv2.flip(preview, 0).tobytes()
            texture = Texture.create(size=(preview.shape[1], preview.shape[0]), colorfmt='bgr')
            texture.blit_buffer(buf, colorfmt='bgr', bufferfmt='ubyte')
            self.preview.texture = texture

    def update_settings(self, instance, value):
        self.live_screen.outer_width = int(self.sliders['Outer Width'].value)
        self.live_screen.outer_height = int(self.sliders['Outer Height'].value)
        self.live_screen.inner_width = int(self.sliders['Inner Width'].value)
        self.live_screen.inner_height = int(self.sliders['Inner Height'].value)
        self.live_screen.zone_offset_y = int(self.sliders['Offset Y'].value)
        self.live_screen.zone_offset_x = int(self.sliders['Offset X'].value)
        self.live_screen.hatch_spacing = int(self.sliders['Hatch Spacing'].value)
        self.live_screen.warning_duration = int(self.sliders['Warning Duration'].value)

    def save_settings(self, instance):
        self.live_screen.save_settings_to_file()

    def load_settings(self, instance):
        self.live_screen.load_settings_from_file()
        self.sliders['Outer Width'].value = self.live_screen.outer_width
        self.sliders['Outer Height'].value = self.live_screen.outer_height
        self.sliders['Inner Width'].value = self.live_screen.inner_width
        self.sliders['Inner Height'].value = self.live_screen.inner_height
        self.sliders['Offset X'].value = self.live_screen.zone_offset_x
        self.sliders['Offset Y'].value = self.live_screen.zone_offset_y
        self.sliders['Hatch Spacing'].value = self.live_screen.hatch_spacing
        self.sliders['Warning Duration'].value = self.live_screen.warning_duration


class SplashScreen(Screen):
    def __init__(self, manager_ref, **kwargs):
        super().__init__(**kwargs)
        self.manager_ref = manager_ref
        self.layout = BoxLayout(orientation='vertical')

        with self.canvas.before:
            Color(0.1, 0.2, 0.3, 1)
            self.bg = Rectangle(size=self.size, pos=self.pos)
        self.bind(size=self._update_rect, pos=self._update_rect)

        self.label = Label(text="Booting P.I.S.A.U", font_size=24, color=(1, 1, 1, 1))
        self.layout.add_widget(self.label)
        self.add_widget(self.layout)

        Clock.schedule_once(self.phase_two, 5)

    def phase_two(self, dt):
        self.label.text = "Programmable Interface for Saw Automation Unit"
        Clock.schedule_once(self.phase_three, 3)

    def phase_three(self, dt):
        current_hour = time.localtime().tm_hour
        if 5 <= current_hour < 12:
            greeting = "Good Morning"
        elif 12 <= current_hour < 18:
            greeting = "Good Afternoon"
        else:
            greeting = "Good Evening"
        self.label.text = greeting
        Clock.schedule_once(lambda dt: setattr(self.manager_ref, 'current', 'live'), 2)

    def _update_rect(self, instance, value):
        self.bg.size = instance.size
        self.bg.pos = instance.pos


class MotionApp(App):
    def build(self):
        Window.clearcolor = (0.1, 0.2, 0.3, 1)
        Window.title = "P.I.S.A.U"
        Window.size = (800, 480)
        Window.fullscreen = 'auto'
        Window.borderless = True
        Window.clearcolor = (0.1, 0.2, 0.3, 1)
        Window.title = "P.I.S.A.U"
        sm = ScreenManager(transition=FadeTransition())
        splash = SplashScreen(manager_ref=sm, name='splash')
        live_view = LiveViewScreen(manager_ref=sm, name='live')
        settings = SettingsScreen(manager_ref=sm, name='settings')
        sm.add_widget(splash)
        sm.add_widget(live_view)
        sm.add_widget(settings)
        sm.current = 'splash'
        return sm

if __name__ == '__main__':
    MotionApp().run()   # <-- THIS WAS MISSING

