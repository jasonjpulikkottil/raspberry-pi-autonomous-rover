import sys
import time
import cv2
import numpy as np
from gpiozero import DistanceSensor, Robot
import smbus
import threading
import datetime
import os
import subprocess
from flask import Flask, Response, jsonify, render_template_string, send_from_directory, redirect, url_for

# ==========================================
# Configuration & Storage Setup
# ==========================================
MODEL_PATH = "model.onnx"
LABELS_PATH = "labels.txt"
INPUT_SIZE = (224, 224)
OBSTACLE_THRESHOLD = 0.20
CONFIDENCE_THRESHOLD = 0.70
SAVE_DIR = "Rover"

os.makedirs(SAVE_DIR, exist_ok=True)

# ==========================================
# Native I2C LCD Driver
# ==========================================
I2C_ADDR = 0x3F # Change to 0x27 if required
I2C_PORT = 1

LCD_CLEARDISPLAY = 0x01
LCD_SETDDRAMADDR = 0x80
LCD_BACKLIGHT = 0x08


class SimpleI2CLCD:
    def __init__(self, addr=I2C_ADDR, port=I2C_PORT):
        self.addr = addr
        self.bus = smbus.SMBus(port)
        self.backlightval = LCD_BACKLIGHT
        try:
            self.send_command(0x33)
            self.send_command(0x32)
            self.send_command(0x06)
            self.send_command(0x0C)
            self.clear()
        except Exception as e:
            print(f"LCD I2C Communication Error: {e}")
            sys.exit(1)

    def strobe(self, data):
        self.bus.write_byte(self.addr, data | 0x04 | self.backlightval)
        time.sleep(0.0005)
        self.bus.write_byte(self.addr, (~0x04 & data) | self.backlightval)
        time.sleep(0.0001)

    def write_four_bits(self, data):
        self.bus.write_byte(self.addr, data | self.backlightval)
        self.strobe(data)

    def send_command(self, cmd):
        self.write_four_bits(cmd & 0xF0)
        self.write_four_bits((cmd << 4) & 0xF0)

    def send_data(self, data):
        self.write_four_bits((data & 0xF0) | 0x01)
        self.write_four_bits(((data << 4) & 0xF0) | 0x01)

    def clear(self):
        self.send_command(LCD_CLEARDISPLAY)
        time.sleep(0.002)

    def set_cursor(self, col, row):
        row_offsets = [0x00, 0x40, 0x14, 0x54]
        if row >= len(row_offsets):
            row = len(row_offsets) - 1
        self.send_command(LCD_SETDDRAMADDR | (col + row_offsets[row]))

    def write_string(self, message):
        for char in message:
            self.send_data(ord(char))


# ==========================================
# Web Portal State & Flask Setup
# ==========================================
app = Flask(__name__)
lock = threading.Lock()
shared_frame = None
telemetry = {
    "action": "Booting...",
    "label": "None",
    "confidence": 0.0,
    "distance": 0.0
}
is_recording = False
rover_active = False  # Rover starts in STANDBY mode
video_writer = None

HTML_DASHBOARD = """
<!DOCTYPE html>
<html>
<head>
    <title>Rover Dashboard</title>
    <style>
        body { font-family: Arial, sans-serif; background: #121212; color: #fff; text-align: center; margin: 0; padding: 20px; }
        .container { display: flex; flex-direction: column; align-items: center; }
        .video-feed { border: 3px solid #333; border-radius: 10px; max-width: 100%; width: 640px; margin-bottom: 20px; }
        .telemetry { background: #1e1e1e; padding: 20px; border-radius: 10px; width: 600px; display: flex; flex-wrap: wrap; justify-content: space-between; }
        .stat-box { width: 45%; background: #2a2a2a; margin: 5px; padding: 10px; border-radius: 5px; font-size: 1.2em; }
        .stat-label { font-size: 0.8em; color: #aaa; text-transform: uppercase; }
        .controls { margin-top: 20px; display: flex; flex-wrap: wrap; gap: 15px; justify-content: center; max-width: 650px; }
        button { padding: 10px 20px; font-size: 1em; cursor: pointer; border: none; border-radius: 5px; font-weight: bold; }
        
        .btn-rover-start { background: #28a745; color: white; }
        .btn-rover-stop { background: #dc3545; color: white; display: none; }
        .btn-photo { background: #007bff; color: white; }
        .btn-record { background: #17a2b8; color: white; }
        .btn-stop-record { background: #dc3545; color: white; display: none; }
        .btn-gallery { background: #ffc107; color: black; }
    </style>
</head>
<body>
    <div class="container">
        <h2>Autonomous Rover Live Feed</h2>
        <img src="/video_feed" class="video-feed" alt="Live Camera Feed">
        
        <div class="telemetry">
            <div class="stat-box"><div class="stat-label">Current Action</div><span id="t-action">-</span></div>
            <div class="stat-box"><div class="stat-label">Vision Detection</div><span id="t-label">-</span></div>
            <div class="stat-box"><div class="stat-label">Confidence</div><span id="t-conf">-</span>%</div>
            <div class="stat-box"><div class="stat-label">Obstacle Distance</div><span id="t-dist">-</span> cm</div>
        </div>
        <div class="controls">
            <button id="btn-rover-start" class="btn-rover-start" onclick="startRover()">Start Rover</button>
            <button id="btn-rover-stop" class="btn-rover-stop" onclick="stopRover()">Stop Rover</button>
            
            <button class="btn-photo" onclick="takePhoto()">Capture Photo</button>
            
            <button id="btn-start" class="btn-record" onclick="startRecording()">Start Recording</button>
            <button id="btn-stop" class="btn-stop-record" onclick="stopRecording()">Stop Recording</button>
            
            <button class="btn-gallery" onclick="window.location.href='/gallery'">Gallery</button>
        </div>
    </div>
    <script>
        function updateTelemetry() {
            fetch('/status').then(res => res.json()).then(data => {
                document.getElementById('t-action').innerText = data.action;
                document.getElementById('t-label').innerText = data.label;
                document.getElementById('t-conf').innerText = data.confidence.toFixed(1);
                document.getElementById('t-dist').innerText = data.distance.toFixed(1);
            });
        }
        setInterval(updateTelemetry, 500);
        
        function startRover() {
            fetch('/start_rover').then(() => {
                document.getElementById('btn-rover-start').style.display = 'none';
                document.getElementById('btn-rover-stop').style.display = 'block';
            });
        }
        
        function stopRover() {
            fetch('/stop_rover').then(() => {
                document.getElementById('btn-rover-stop').style.display = 'none';
                document.getElementById('btn-rover-start').style.display = 'block';
            });
        }
        
        function takePhoto() {
            fetch('/capture_photo').then(res => res.text()).then(msg => alert(msg));
        }
        
        function startRecording() {
            fetch('/start_recording').then(() => {
                document.getElementById('btn-start').style.display = 'none';
                document.getElementById('btn-stop').style.display = 'block';
            });
        }
        
        function stopRecording() {
            fetch('/stop_recording').then(() => {
                document.getElementById('btn-stop').style.display = 'none';
                document.getElementById('btn-start').style.display = 'block';
            });
        }
    </script>
</body>
</html>
"""

HTML_GALLERY = """
<!DOCTYPE html>
<html>
<head>
    <title>Rover Gallery</title>
    <style>
        body { font-family: Arial, sans-serif; background: #121212; color: #fff; text-align: center; padding: 20px; }
        h2 { margin-bottom: 5px; }
        .back-btn { display: inline-block; margin-bottom: 20px; padding: 10px 15px; background: #007bff; color: white; text-decoration: none; border-radius: 5px; font-weight: bold; }
        ul { list-style-type: none; padding: 0; max-width: 700px; margin: 0 auto; }
        li { background: #1e1e1e; margin: 5px 0; padding: 15px; border-radius: 5px; display: flex; justify-content: space-between; align-items: center; }
        .actions { display: flex; gap: 10px; }
        .download-btn { background: #28a745; color: white; text-decoration: none; padding: 8px 12px; border-radius: 3px; font-weight: bold; }
        .delete-btn { background: #dc3545; color: white; text-decoration: none; padding: 8px 12px; border-radius: 3px; font-weight: bold; }
        .empty-msg { color: #aaa; margin-top: 20px; }
    </style>
</head>
<body>
    <h2>Captured Files</h2>
    <a href="/" class="back-btn">Back to Dashboard</a>
    
    {% if files %}
    <ul>
        {% for file in files %}
        <li>
            <span>{{ file }}</span>
            <div class="actions">
                <a href="/download/{{ file }}" class="download-btn">Download</a>
                <a href="/delete/{{ file }}" class="delete-btn" onclick="return confirm('Are you sure you want to permanently delete this file?');">Delete</a>
            </div>
        </li>
        {% endfor %}
    </ul>
    {% else %}
    <p class="empty-msg">No photos or videos captured yet.</p>
    {% endif %}
</body>
</html>
"""


@app.route('/')
def index():
    return render_template_string(HTML_DASHBOARD)


@app.route('/gallery')
def gallery():
    try:
        files = os.listdir(SAVE_DIR)
        files.sort(reverse=True)  # Show newest files first
    except Exception:
        files = []
    return render_template_string(HTML_GALLERY, files=files)


@app.route('/download/<filename>')
def download(filename):
    return send_from_directory(SAVE_DIR, filename, as_attachment=True)


@app.route('/delete/<filename>')
def delete_file(filename):
    filepath = os.path.join(SAVE_DIR, filename)
    if os.path.exists(filepath):
        try:
            os.remove(filepath)
        except Exception as e:
            print(f"Error deleting file {filename}: {e}")
    return redirect(url_for('gallery'))


@app.route('/start_rover')
def start_rover():
    global rover_active
    with lock:
        rover_active = True
    return "Rover Started", 200


@app.route('/stop_rover')
def stop_rover():
    global rover_active
    with lock:
        rover_active = False
    return "Rover Stopped", 200


def generate_frames():
    global shared_frame
    while True:
        with lock:
            if shared_frame is None:
                continue
            ret, buffer = cv2.imencode('.jpg', shared_frame)
            frame = buffer.tobytes()
        
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
        time.sleep(0.05)


@app.route('/video_feed')
def video_feed():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/status')
def status():
    with lock:
        return jsonify(telemetry)


@app.route('/capture_photo')
def capture_photo():
    with lock:
        if shared_frame is not None:
            filename = f"photo_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
            filepath = os.path.join(SAVE_DIR, filename)
            cv2.imwrite(filepath, shared_frame)
            return "Photo saved to Gallery!", 200
    return "Failed to capture", 500


@app.route('/start_recording')
def start_recording():
    global is_recording, video_writer, shared_frame
    with lock:
        if shared_frame is not None and not is_recording:
            h, w = shared_frame.shape[:2]
            filename = f"video_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.avi"
            filepath = os.path.join(SAVE_DIR, filename)
            fourcc = cv2.VideoWriter_fourcc(*'XVID')
            video_writer = cv2.VideoWriter(filepath, fourcc, 10.0, (w, h))
            is_recording = True
            return "Recording started", 200
    return "Error starting recording", 500


@app.route('/stop_recording')
def stop_recording():
    global is_recording, video_writer
    with lock:
        if is_recording:
            is_recording = False
            if video_writer:
                video_writer.release()
                video_writer = None
            return "Recording stopped", 200
    return "Not recording", 400


# ==========================================
# Network IP Fetching Helper
# ==========================================
def get_ip_address():
    """Run hostname -I to get the IP address of the Pi."""
    try:
        ip_output = subprocess.check_output(['hostname', '-I']).decode('utf-8').strip()
        ips = ip_output.split()
        if ips:
            return ips[0]
    except Exception:
        pass
    return None


# ==========================================
# Hardware Setup
# ==========================================
robot = Robot(left=(17, 27), right=(22, 23))
ultrasonic = DistanceSensor(echo=25, trigger=24, max_distance=2.0)
lcd = SimpleI2CLCD()
lcd.clear()
lcd.set_cursor(0, 0)
lcd.write_string("Rover Booting...")

# Wait and fetch IP Address
lcd.clear()
lcd.set_cursor(0, 0)
lcd.write_string("Waiting for WiFi")
print("Waiting for network connection...")

pi_ip = None
for _ in range(30):  # Wait up to 30 seconds for Wi-Fi
    pi_ip = get_ip_address()
    if pi_ip:
        break
    time.sleep(1)

lcd.clear()
lcd.set_cursor(0, 0)
if pi_ip:
    print(f"WiFi Connected. IP: {pi_ip}")
    lcd.write_string("WiFi Connected!")
    lcd.set_cursor(0, 1)
    lcd.write_string(pi_ip[:16])
    time.sleep(10)  # Display the IP for 10 seconds
else:
    print("No WiFi connection found.")
    lcd.write_string("No WiFi (Offline)")
    time.sleep(3)

# Load labels and model
try:
    with open(LABELS_PATH, "r") as f:
        classes = [line.strip() for line in f.readlines()]
except FileNotFoundError:
    print("labels.txt not found")
    sys.exit(1)

print("Loading ONNX model...")
lcd.clear()
lcd.set_cursor(0, 0)
lcd.write_string("Loading Model...")

try:
    net = cv2.dnn.readNetFromONNX(MODEL_PATH)
except Exception as e:
    print(e)
    sys.exit(1)

cap = cv2.VideoCapture(0, cv2.CAP_V4L2)
if not cap.isOpened():
    print("Cannot open camera")
    sys.exit(1)


# ==========================================
# Rover Control Loop (Background Thread)
# ==========================================
def preprocess(frame):
    img = cv2.resize(frame, INPUT_SIZE)
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    img = img.astype(np.float32)
    img = (img / 127.5) - 1.0
    img = np.expand_dims(img, axis=0)
    return img


def avoid_obstacle():
    robot.backward(0.6)
    time.sleep(0.8)
    robot.left(0.6)
    time.sleep(0.7)
    robot.stop()


def update_lcd(action_text, label_text, conf, dist):
    lcd.clear()
    lcd.set_cursor(0, 0)
    lcd.write_string(action_text[:16].ljust(16))
    lcd.set_cursor(0, 1)
    line2 = f"{label_text[:6]} {conf*100:.0f}% {dist*100:.0f}cm"
    lcd.write_string(line2[:16].ljust(16))


def rover_loop():
    global shared_frame, telemetry, is_recording, video_writer, rover_active
    print("Autonomous background thread started")
    
    while True:
        ret, frame = cap.read()
        if not ret:
            continue
        distance = ultrasonic.distance
        
        if not rover_active:
            robot.stop()
            action = "STANDBY"
            
            # Continue running inference to keep UI Telemetry updated
            blob = preprocess(frame)
            net.setInput(blob)
            output = net.forward()[0]
            class_id = int(np.argmax(output))
            confidence = float(output[class_id])
            label = classes[class_id] if class_id < len(classes) else str(class_id)
            
        else:
            # Hardware safety override
            if distance <= OBSTACLE_THRESHOLD:
                action = "ULTRASONIC"
                label = "OVERRIDE"
                confidence = 1.0
                avoid_obstacle()
            else:
                # Neural network inference
                blob = preprocess(frame)
                net.setInput(blob)
                output = net.forward()[0]
                class_id = int(np.argmax(output))
                confidence = float(output[class_id])
                label = classes[class_id] if class_id < len(classes) else str(class_id)
                
                if confidence < CONFIDENCE_THRESHOLD:
                    robot.stop()
                    action = "LOW CONFIDENCE"
                elif class_id == 1:
                    robot.forward(0.6)
                    action = "PATH -> FWD"
                elif class_id == 0:
                    avoid_obstacle()
                    action = "VISION OBSTACLE"
                else:
                    robot.stop()
                    action = "UNKNOWN"
        
        # Update Shared State for Web Portal
        with lock:
            shared_frame = frame.copy()
            telemetry["action"] = action
            telemetry["label"] = label
            telemetry["confidence"] = confidence * 100
            telemetry["distance"] = distance * 100
            
            # Record video frame if recording
            if is_recording and video_writer is not None:
                video_writer.write(frame)
        
        # Update Physical LCD
        update_lcd(telemetry["action"], telemetry["label"], telemetry["confidence"]/100, distance)
        time.sleep(0.05)


# ==========================================
# Main Execution
# ==========================================
if __name__ == '__main__':
    # Start the rover logic in a background daemon thread
    rover_thread = threading.Thread(target=rover_loop, daemon=True)
    rover_thread.start()
    
    try:
        # Start the Flask Web Server on port 5000
        print(f"Web Dashboard available at: http://{pi_ip if pi_ip else 'localhost'}:5000")
        app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        print("Shutting down...")
    finally:
        robot.stop()
        cap.release()
        if video_writer:
            video_writer.release()
        try:
            lcd.clear()
            lcd.set_cursor(0, 0)
            lcd.write_string("Shutdown Complete")
        except Exception:
            pass
        print("Shutdown complete")
