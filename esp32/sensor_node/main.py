"""MicroPython firmware for ESP32 sensor node.

Reads DHT22 (temperature/humidity), PIR motion, and door magnetic sensor.
Publishes JSON to MQTT every 30 seconds.
"""

import json
import time

import machine
import network
from umqtt.simple import MQTTClient

# ── Configuration ────────────────────────────────────────────────────
WIFI_SSID = "YOUR_WIFI_SSID"
WIFI_PASSWORD = "YOUR_WIFI_PASSWORD"

MQTT_BROKER = "192.168.1.100"
MQTT_PORT = 1883
NODE_ID = "sensor_01"
MQTT_TOPIC = f"jarvis/sensors/{NODE_ID}/state"

DHT_PIN = 4
PIR_PIN = 14
DOOR_PIN = 27
LED_PIN = 2  # Onboard LED on most ESP32 boards

READ_INTERVAL_S = 30


# ── Hardware setup ───────────────────────────────────────────────────

def setup_hardware():
    """Initialise GPIO pins and return (dht_sensor, pir_pin, door_pin, led_pin)."""
    import dht

    dht_sensor = dht.DHT22(machine.Pin(DHT_PIN))
    pir = machine.Pin(PIR_PIN, machine.Pin.IN)
    door = machine.Pin(DOOR_PIN, machine.Pin.IN, machine.Pin.PULL_UP)
    led = machine.Pin(LED_PIN, machine.Pin.OUT)
    return dht_sensor, pir, door, led


# ── Wi-Fi ────────────────────────────────────────────────────────────

def connect_wifi():
    """Connect to Wi-Fi and block until an IP is obtained."""
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected():
        print("Wi-Fi already connected:", wlan.ifconfig())
        return wlan

    print(f"Connecting to Wi-Fi '{WIFI_SSID}'...")
    wlan.connect(WIFI_SSID, WIFI_PASSWORD)

    retries = 0
    while not wlan.isconnected():
        time.sleep(1)
        retries += 1
        if retries > 30:
            print("Wi-Fi connection timed out. Resetting...")
            machine.reset()

    print("Wi-Fi connected:", wlan.ifconfig())
    return wlan


# ── MQTT ─────────────────────────────────────────────────────────────

def connect_mqtt():
    """Connect to the MQTT broker and return the client."""
    client = MQTTClient(NODE_ID, MQTT_BROKER, port=MQTT_PORT)
    client.connect()
    print(f"MQTT connected to {MQTT_BROKER}:{MQTT_PORT}")
    return client


def blink_led(led, times=1, on_ms=100, off_ms=100):
    """Blink the onboard LED to signal a successful publish."""
    for _ in range(times):
        led.value(1)
        time.sleep_ms(on_ms)
        led.value(0)
        time.sleep_ms(off_ms)


# ── Sensor reading ───────────────────────────────────────────────────

def read_sensors(dht_sensor, pir, door):
    """Read all sensors and return a JSON-serialisable dict."""
    temperature = None
    humidity = None
    try:
        dht_sensor.measure()
        temperature = dht_sensor.temperature()
        humidity = dht_sensor.humidity()
    except OSError as exc:
        print("DHT22 read error:", exc)

    motion = bool(pir.value())
    door_open = not bool(door.value())  # Pull-up: LOW = closed, HIGH = open

    return {
        "node_id": NODE_ID,
        "temperature": temperature,
        "humidity": humidity,
        "motion": motion,
        "door_open": door_open,
        "uptime_s": time.ticks_ms() // 1000,
    }


# ── Main loop ────────────────────────────────────────────────────────

def main():
    dht_sensor, pir, door, led = setup_hardware()
    connect_wifi()
    client = connect_mqtt()

    print(f"Publishing to '{MQTT_TOPIC}' every {READ_INTERVAL_S}s")

    while True:
        try:
            payload = read_sensors(dht_sensor, pir, door)
            message = json.dumps(payload)
            client.publish(MQTT_TOPIC, message)
            print("Published:", message)
            blink_led(led, times=2)
        except OSError:
            print("MQTT publish failed. Reconnecting...")
            try:
                client = connect_mqtt()
            except OSError:
                print("Reconnect failed. Retrying next cycle.")

        time.sleep(READ_INTERVAL_S)


if __name__ == "__main__":
    main()
