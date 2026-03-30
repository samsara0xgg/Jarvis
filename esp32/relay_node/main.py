"""MicroPython firmware for ESP32 relay node.

Subscribes to MQTT commands and toggles a relay GPIO.
Publishes relay state back to MQTT after each change.
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
NODE_ID = "relay_01"
COMMAND_TOPIC = f"jarvis/devices/{NODE_ID}/set"
STATE_TOPIC = f"jarvis/devices/{NODE_ID}/state"

RELAY_PIN = 26
LED_PIN = 2  # Onboard LED


# ── Hardware setup ───────────────────────────────────────────────────

def setup_hardware():
    """Initialise relay and LED pins."""
    relay = machine.Pin(RELAY_PIN, machine.Pin.OUT)
    relay.value(0)
    led = machine.Pin(LED_PIN, machine.Pin.OUT)
    return relay, led


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

def publish_state(client, relay):
    """Publish the current relay state to the state topic."""
    state = {
        "node_id": NODE_ID,
        "relay_on": bool(relay.value()),
        "uptime_s": time.ticks_ms() // 1000,
    }
    message = json.dumps(state)
    client.publish(STATE_TOPIC, message)
    print("State published:", message)


def make_callback(client, relay, led):
    """Return an MQTT message callback bound to the relay and LED."""

    def on_message(topic, msg):
        try:
            payload = json.loads(msg)
        except (ValueError, TypeError):
            print("Invalid JSON payload:", msg)
            return

        action = payload.get("action", "").lower()

        if action == "on":
            relay.value(1)
            print("Relay ON")
        elif action == "off":
            relay.value(0)
            print("Relay OFF")
        elif action == "toggle":
            relay.value(1 - relay.value())
            print("Relay toggled to", "ON" if relay.value() else "OFF")
        else:
            print("Unknown action:", action)
            return

        # Blink LED to acknowledge
        led.value(1)
        time.sleep_ms(100)
        led.value(0)

        publish_state(client, relay)

    return on_message


def connect_mqtt(relay, led):
    """Connect to the MQTT broker, subscribe, and return the client."""
    client = MQTTClient(NODE_ID, MQTT_BROKER, port=MQTT_PORT)
    client.set_callback(make_callback(client, relay, led))
    client.connect()
    client.subscribe(COMMAND_TOPIC)
    print(f"MQTT connected. Subscribed to '{COMMAND_TOPIC}'")
    return client


# ── Main loop ────────────────────────────────────────────────────────

def main():
    relay, led = setup_hardware()
    connect_wifi()
    client = connect_mqtt(relay, led)

    # Publish initial state
    publish_state(client, relay)

    print("Relay node ready. Waiting for commands...")

    while True:
        try:
            client.check_msg()
        except OSError:
            print("MQTT error. Reconnecting...")
            try:
                client = connect_mqtt(relay, led)
            except OSError:
                print("Reconnect failed. Retrying in 5s...")
                time.sleep(5)
                continue

        time.sleep_ms(100)


if __name__ == "__main__":
    main()
