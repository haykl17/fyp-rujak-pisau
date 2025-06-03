# P.I.S.A.U. (Programmable Interface for Saw Automation Unit)

Hello I.R.E.X. Attendee! 👋 Thank you for stopping by. This repository contains the code, schematics, and documentation for P.I.S.A.U., a portable, AI‐enabled smart hacksaw designed for small workshops and educational labs.

---

## 🎯 Project Overview

Traditional hacksaws rely on manual operation, resulting in variable cut quality, operator fatigue, and safety risks. **P.I.S.A.U.** bridges that gap by integrating:

- **AI‐driven safety detection** (OpenCV on Raspberry Pi 4B)  
- **24 V battery‐free motor control** with precise PWM speed regulation  
- **IoT telemetry** via Blynk (RPM, temperature, run hours, status text)  
- **Kivy touchscreen UI** for real‐time status and control  
- **Optimized airflow** for passive thermal management  

All components fit into a compact (~4 kg) frame, offering an affordable (≤ USD 500) Industry 4.0 solution for cutting plastics, wood, and thin metals.

---

## 🚀 Key Features

1. **AI Safety & Motor Control**  
   - Raspberry Pi 4B runs an OpenCV HSV‐thresholding algorithm to monitor a 200×200 pixel “inner zone” 30 cm above the blade.  
   - Motor shuts off within 100 ms when a hand or foreign object intrudes.  

2. **24 V‐Powered H-Bridge Motor Driver**  
   - Custom MOSFET H-bridge PCB driven by Pi’s GPIO (1 kHz PWM on GPIO 18, enable on GPIO 23).  
   - Brushless DC motor (0–2650 RPM) reduced to ~1375 RPM at the blade via 2:1 belt & pulley.  

3. **IoT Telemetry (Blynk Integration)**  
   - Streams four virtual pins to a smartphone dashboard with < 300 ms latency:  
     - V0 → Current RPM  
     - V1 → Temperature (°C)  
     - V2 → Cumulative Run Hours  
     - V6 → Status Text (“Ready,” “Emergency,” etc.)  

4. **Kivy Touchscreen Interface**  
   - Live 640×480 @ 15 FPS camera feed with overlaid “inner” (red) and “outer” (green) safety zones.  
   - Status labels: Set Speed, Set RPM, Current RPM, Temperature, Run Hours.  
   - Control buttons: Presets (Plastic, Wood, Thin Metal), Hold to Start, Manual Override (slider), and Emergency Stop.  

5. **Passive Thermal Management**  
   - Directed airflow channels around the motor, MOSFETs, and voltage regulators—no active fans required.  

---
