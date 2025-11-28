#!/usr/bin/env python3
"""
Raspberry Pi Zero W Pool Controller - Master Version 2.3
Features:
- Pump (Master) and Cell_Bridge relays
- Manual ON/OFF with 8h auto-off
- Auto schedule (24 hours)
- Boost mode (3h)
- PWM on GPIO20 mirrored to GPIO21 with persistence
- Heartbeat LED on GPIO4
- Local time display with optional DST
- Web interface with colored indicators, timers, GPIO labels, and heartbeat
- Version displayed on web page
"""

import os
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from flask import Flask, render_template_string, request, jsonify, redirect, url_for

try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO_AVAILABLE = True
except Exception:
    GPIO_AVAILABLE = False
    class FakeGPIO:
        BCM = OUT = None
        LOW = 0
        HIGH = 1
        def setmode(self, m): pass
        def setup(self, pin, mode): pass
        def output(self, pin, val): print(f"[GPIO] {pin} -> {'HIGH' if val else 'LOW'}")
        def PWM(self, pin, freq):
            class DummyPWM:
                def start(self, duty): pass
                def ChangeDutyCycle(self, duty): pass
                def stop(self): pass
            return DummyPWM()
        def cleanup(self): pass
    GPIO = FakeGPIO()

# ----- Configuration -----
VERSION = "2.3"
PUMP_PIN = 17
CELL_BRIDGE1_PIN = 27
CELL_BRIDGE2_PIN = 22
PWM_PIN = 20
PWM_MIRROR_PIN = 21
HEARTBEAT_PIN = 2
SETTINGS_FILE = Path("settings.json")
TIMEZONE_OFFSET = 10  # AEST
APP_PORT = 5000
APP_HOST = "0.0.0.0"
PWM_FREQ = 1000  # Hz
MANUAL_AUTO_OFF_HOURS = 8
BOOST_HOURS = 3
# --------------------------

# Default settings
DEFAULT_SETTINGS = {
    "mode": "auto",
    "manual_state": False,
    "manual_on_until": None,
    "schedule": [False]*24,
    "boost_until": None,
    "pwm_duty": 0,
    "dst": False,
    "last_updated": None
}

# ----- Load / Save Settings -----
def load_settings():
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r") as f:
                data = json.load(f)
            for k,v in DEFAULT_SETTINGS.items():
                if k not in data:
                    data[k] = v
            if len(data.get("schedule", [])) != 24:
                data["schedule"] = [False]*24
            return data
        except Exception:
            pass
    return DEFAULT_SETTINGS.copy()

def save_settings(s):
    s["last_updated"] = datetime.utcnow().isoformat()
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s, f, indent=2)

state_lock = threading.Lock()
state = {
    "settings": load_settings(),
    "pump_on": False,
    "cell_on": False,
    "heartbeat_on": False
}

# ----- GPIO / PWM Setup -----
if GPIO_AVAILABLE:
    GPIO.setup(PUMP_PIN, GPIO.OUT)
    GPIO.setup(CELL_BRIDGE1_PIN, GPIO.OUT)
    GPIO.setup(CELL_BRIDGE2_PIN, GPIO.OUT)
    GPIO.setup(PWM_PIN, GPIO.OUT)
    GPIO.setup(PWM_MIRROR_PIN, GPIO.OUT)
    GPIO.setup(HEARTBEAT_PIN, GPIO.OUT)
    try:
        pwm_ctrl = GPIO.PWM(PWM_PIN, PWM_FREQ)
        pwm_ctrl.start(state["settings"].get("pwm_duty",0))
    except Exception as e:
        print("PWM init error:", e)
        pwm_ctrl = None
else:
    pwm_ctrl = None

def gpio_write(pin, state_val: bool):
    try:
        GPIO.output(pin, GPIO.HIGH if state_val else GPIO.LOW)
        if pin == PWM_MIRROR_PIN:
            GPIO.output(PWM_MIRROR_PIN, GPIO.HIGH if state_val else GPIO.LOW)
    except Exception as e:
        print("GPIO write error:", e)

def set_pwm(duty: int):
    duty = max(0, min(100, int(duty)))
    with state_lock:
        state["settings"]["pwm_duty"] = duty
        save_settings(state["settings"])
    if GPIO_AVAILABLE and pwm_ctrl:
        try:
            pwm_ctrl.ChangeDutyCycle(duty)
            GPIO.output(PWM_MIRROR_PIN, GPIO.HIGH if duty>0 else GPIO.LOW)
        except Exception as e:
            print("PWM write error:", e)

# ----- Time -----
def now_local():
    offset = TIMEZONE_OFFSET + (1 if state["settings"].get("dst") else 0)
    return datetime.utcnow().replace(tzinfo=timezone.utc) + timedelta(hours=offset)

# ----- Relay Control -----
def set_pump(on):
    with state_lock:
        state["pump_on"] = bool(on)
    gpio_write(PUMP_PIN, on)

def set_cell(on):
    with state_lock:
        state["cell_on"] = bool(on)
    gpio_write(CELL_BRIDGE1_PIN, on)
    gpio_write(CELL_BRIDGE2_PIN, on)

def evaluate_auto_schedule():
    with state_lock:
        s = state["settings"].copy()
    now = now_local()
    if s["mode"] == "auto":
        set_pump(s["schedule"][now.hour])

def check_boost_timeout():
    with state_lock:
        s = state["settings"]
        boost_until = s.get("boost_until")
    if boost_until:
        try:
            if now_local() >= datetime.fromisoformat(boost_until):
                with state_lock:
                    s["boost_until"] = None
                    s["mode"] = "auto"
                    save_settings(s)
                print("Boost expired, back to Auto mode")
        except Exception:
            with state_lock:
                s["boost_until"] = None
                save_settings(s)

def check_manual_auto_off():
    with state_lock:
        s = state["settings"]
        manual_until = s.get("manual_on_until")
    if manual_until:
        try:
            if now_local() >= datetime.fromisoformat(manual_until):
                with state_lock:
                    s["manual_state"] = False
                    s["manual_on_until"] = None
                    s["mode"] = "auto"
                    save_settings(s)
                print("Manual ON auto-off expired, back to Auto mode")
        except Exception:
            with state_lock:
                s["manual_state"] = False
                s["manual_on_until"] = None
                save_settings(s)

def apply_mode_on_change():
    with state_lock:
        s = state["settings"].copy()
    mode = s["mode"]
    if mode == "manual":
        set_pump(s["manual_state"])
    elif mode == "auto":
        evaluate_auto_schedule()
    elif mode == "boost":
        set_pump(True)
    else:
        set_pump(False)

# ----- Background Threads -----
def cell_task():
    last_state = None
    while True:
        with state_lock:
            pump = state["pump_on"]
        if pump:
            epoch_min = int(time.time() // 60)
            cell_state = (epoch_min // 15) % 2 == 0
        else:
            cell_state = False
        if last_state is None or cell_state != last_state:
            set_cell(cell_state)
            last_state = cell_state
        time.sleep(1)

def scheduler_task():
    while True:
        try:
            check_boost_timeout()
            check_manual_auto_off()
            with state_lock:
                mode = state["settings"]["mode"]
            if mode == "auto":
                evaluate_auto_schedule()
            elif mode in ("manual","boost"):
                apply_mode_on_change()
            time.sleep(10)
        except Exception as e:
            print("Scheduler error:", e)
            time.sleep(5)

# ----- Heartbeat Thread -----
def heartbeat_task():
    while True:
        if GPIO_AVAILABLE:
            GPIO.output(HEARTBEAT_PIN, GPIO.HIGH)
        with state_lock:
            state["heartbeat_on"] = True
        time.sleep(0.2)
        if GPIO_AVAILABLE:
            GPIO.output(HEARTBEAT_PIN, GPIO.LOW)
        with state_lock:
            state["heartbeat_on"] = False
        time.sleep(0.2)

threading.Thread(target=cell_task, daemon=True).start()
threading.Thread(target=scheduler_task, daemon=True).start()
threading.Thread(target=heartbeat_task, daemon=True).start()

# ----- Flask Web UI -----
app = Flask(__name__)

# Pass GPIO pin labels to template
GPIO_LABELS = {
    "pump_pin": PUMP_PIN,
    "cell1_pin": CELL_BRIDGE1_PIN,
    "cell2_pin": CELL_BRIDGE2_PIN,
    "pwm_pin": PWM_PIN,
    "pwm_mirror_pin": PWM_MIRROR_PIN,
    "heartbeat_pin": HEARTBEAT_PIN
}

# HTML template with heartbeat indicator
TEMPLATE = """<!doctype html>
<html>
<head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>VKTEK Pool Pump Controller</title>
<style>
body{font-family:sans-serif;margin:10px;}
button{padding:8px 12px;border:none;border-radius:6px;margin:4px;}
.btn-green{background:#4caf50;color:white;}
.btn-red{background:#d9534f;color:white;}
.btn-blue{background:#0275d8;color:white;}
.indicator {display:inline-block;padding:6px 10px;border-radius:8px;font-weight:600;}
.indicator.off {background:#ccc;color:black;}
.indicator.on {color:white;}
#mode-auto.on {background:#0275d8;} #mode-manual.on {background:#4caf50;} #mode-boost.on {background:#d9534f;}
#master.on {background:#4caf50;color:white;} #master.off {background:#ccc;color:black;}
#secondary1.on {background:#4caf50;color:white;} #secondary1.off {background:#ccc;color:black;}
#secondary2.on {background:#4caf50;color:white;} #secondary2.off {background:#ccc;color:black;}
#heartbeat.on {background:#ff9800;color:white;} #heartbeat.off {background:#ccc;color:black;}
.hour-box{width:36px;height:36px;margin:2px;display:inline-block;text-align:center;}
</style></head>
<body>
<h2>VKTEK Pool Pump Controller - v{{ version }}</h2>
<div><b>Local Time:</b> <span id='time'>--</span></div>
<div><b>DST:</b>
  <form method='POST' action='/set_dst' style='display:inline;'>
    <input type='checkbox' name='dst' {% if dst %}checked{% endif %} onchange='this.form.submit()'> Enable
  </form>
</div>
<div><b>Mode:</b> 
  <span id='mode-auto' class='indicator off'>Auto</span>
  <span id='mode-manual' class='indicator off'>Manual <span id="manual_timer"></span></span>
  <span id='mode-boost' class='indicator off'>Boost <span id="boost_timer"></span></span>
</div>

<div><b>Pump (GPIO{{ pump_pin }}):</b> <span id='master' class='indicator off'>OFF</span></div>
<div><b>Cell_Bridge1 (GPIO{{ cell1_pin }}):</b> <span id='secondary1' class='indicator off'>OFF</span></div>
<div><b>Cell_Bridge2 (GPIO{{ cell2_pin }}):</b> <span id='secondary2' class='indicator off'>OFF</span></div>
<div><b>Heartbeat (GPIO{{ heartbeat_pin }}):</b> <span id='heartbeat' class='indicator off'>OFF</span></div>

<form method='POST' action='/set'>
  <button class='btn-blue' name='mode' value='auto'>Auto</button>
  <button class='btn-green' name='mode' value='manual'>Manual</button>
  <button class='btn-red' name='mode' value='boost'>Boost (3h)</button>
</form>

<form method='POST' action='/manual'>
  <button class='btn-green' name='state' value='on'>Manual ON</button>
  <button class='btn-red' name='state' value='off'>Manual OFF</button>
</form>

<h3>24-Hour Schedule</h3>
<form method='POST' action='/save_schedule'>
{% for h in range(24) %}
<label class='hour-box'>
<input type='checkbox' name='h{{h}}' {% if schedule[h] %}checked{% endif %}>{{'%02d' % h}}
</label>
{% endfor %}
<br><button class='btn-blue'>Save Schedule</button>
</form>

<h3>PWM Control (GPIO{{ pwm_pin }}/{{ pwm_mirror_pin }})</h3>
<input type="range" id="pwmSlider" min="0" max="100" value="{{ pwm_duty }}">
<span id="pwmValue">{{ pwm_duty }}</span> %

<script>
async function update(){
  let r = await fetch('/status');
  let j = await r.json();
  document.getElementById('time').innerText=j.time;

  document.getElementById('mode-auto').className='indicator off';
  document.getElementById('mode-manual').className='indicator off';
  document.getElementById('mode-boost').className='indicator off';
  if(j.mode==='auto') document.getElementById('mode-auto').className='indicator on';
  else if(j.mode==='manual'){ if(j.pump_on) document.getElementById('mode-manual').className='indicator on'; }
  else if(j.mode==='boost') document.getElementById('mode-boost').className='indicator on';

  document.getElementById('boost_timer').innerText=j.boost_remaining?(' ('+j.boost_remaining+')'):'';
  document.getElementById('manual_timer').innerText=j.manual_remaining?(' ('+j.manual_remaining+')'):'';

  let m=document.getElementById('master');
  m.className='indicator '+(j.pump_on?'on':'off'); m.innerText=j.pump_on?'ON':'OFF';

  let s1=document.getElementById('secondary1');
  s1.className='indicator '+(j.cell_on?'on':'off'); s1.innerText=j.cell_on?'ON':'OFF';

  let s2=document.getElementById('secondary2');
  s2.className='indicator '+(j.cell_on?'on':'off'); s2.innerText=j.cell_on?'ON':'OFF';

  let hb=document.getElementById('heartbeat');
  hb.className='indicator '+(j.heartbeat_on?'on':'off'); hb.innerText=j.heartbeat_on?'ON':'OFF';

  document.getElementById('pwmSlider').value=j.pwm_duty;
  document.getElementById('pwmValue').innerText=j.pwm_duty;
}
setInterval(update,1000);
update();

document.getElementById('pwmSlider').addEventListener('input', async function(){
  let val=this.value;
  document.getElementById('pwmValue').innerText=val;
  await fetch('/pwm',{method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({duty:val})});
});
</script>
"""

@app.route('/')
def index():
    with state_lock:
        s = state["settings"]
        schedule = s.get("schedule", [False]*24)
        pwm_duty = s.get("pwm_duty", 0)
        dst = s.get("dst", False)
    return render_template_string(TEMPLATE,
                                  schedule=schedule,
                                  pwm_duty=pwm_duty,
                                  dst=dst,
                                  version=VERSION,
                                  **GPIO_LABELS)

@app.route('/status')
def status():
    with state_lock:
        s = state["settings"].copy()
        boost_remaining = None
        if s["mode"]=="boost" and s.get("boost_until"):
            delta = datetime.fromisoformat(s["boost_until"]) - now_local()
            seconds = int(delta.total_seconds())
            if seconds>0:
                h,r=divmod(seconds,3600)
                m,s_sec=divmod(r,60)
                boost_remaining=f"{h:02d}:{m:02d}:{s_sec:02d}"
            else:
                s["boost_until"]=None
                s["mode"]="auto"
                save_settings(s)
        manual_remaining = None
        if s.get("manual_state") and s.get("manual_on_until"):
            delta = datetime.fromisoformat(s["manual_on_until"]) - now_local()
            seconds = int(delta.total_seconds())
            if seconds>0:
                h,r=divmod(seconds,3600)
                m,s_sec=divmod(r,60)
                manual_remaining=f"{h:02d}:{m:02d}:{s_sec:02d}"
            else:
                s["manual_state"]=False
                s["manual_on_until"]=None
                s["mode"]="auto"
                save_settings(s)
        data = {
            "time": now_local().strftime("%Y-%m-%d %H:%M:%S"),
            "mode": s["mode"],
            "pump_on": state["pump_on"],
            "cell_on": state["cell_on"],
            "pwm_duty": s.get("pwm_duty",0),
            "heartbeat_on": state["heartbeat_on"],
            "boost_remaining": boost_remaining,
            "manual_remaining": manual_remaining
        }
    return jsonify(data)

@app.route('/set', methods=['POST'])
def set_mode():
    mode = request.form.get('mode')
    with state_lock:
        s = state["settings"]
        s["mode"] = mode
        if mode=="boost":
            s["boost_until"]=(now_local()+timedelta(hours=BOOST_HOURS)).isoformat()
        save_settings(s)
    apply_mode_on_change()
    return redirect(url_for('index'))

@app.route('/manual', methods=['POST'])
def manual_toggle():
    state_str = request.form.get('state')
    with state_lock:
        s = state["settings"]
        s["manual_state"]=state_str=='on'
        if s["manual_state"]:
            s["manual_on_until"]=(now_local()+timedelta(hours=MANUAL_AUTO_OFF_HOURS)).isoformat()
        else:
            s["manual_on_until"]=None
        s["mode"]="manual"
        save_settings(s)
    apply_mode_on_change()
    return redirect(url_for('index'))

@app.route('/save_schedule', methods=['POST'])
def save_schedule():
    with state_lock:
        s = state["settings"]
        for h in range(24):
            s["schedule"][h] = bool(request.form.get(f"h{h}"))
        save_settings(s)
    return redirect(url_for('index'))

@app.route('/pwm', methods=['POST'])
def pwm_route():
    data = request.json
    duty = int(data.get("duty",0))
    set_pwm(duty)
    return jsonify({"pwm_duty": duty})

@app.route('/set_dst', methods=['POST'])
def set_dst():
    with state_lock:
        s = state["settings"]
        s["dst"] = 'dst' in request.form
        save_settings(s)
    return redirect(url_for('index'))

if __name__ == "__main__":
    print("Starting Flask web server...")
    app.run(host=APP_HOST, port=APP_PORT, debug=False)
