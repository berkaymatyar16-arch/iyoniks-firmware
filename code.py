import os
import time
import wifi
import socketpool
import adafruit_minimqtt.adafruit_minimqtt as MQTT
from digitalio import DigitalInOut, Direction
import adafruit_requests
import analogio
import board
import math
import ssl
import busio
from adafruit_bus_device import i2c_device
import pwmio
import microcontroller


# ---------- AYARLAR ----------
WIFI_SSID = ""
WIFI_PASS = ""
MQTT_BROKER = "429cfdf2bbc44476b16e3466dfaea8e1.s1.eu.hivemq.cloud"
MQTT_USER = "iyoniks"
MQTT_PASS = "Iyoniks123"
MQTT_PORT = 8883
CLIENT_ID = "pico_w_client"


OTA_URL = "https://raw.githubusercontent.com/argemirdev/OTA/main/code.py"


DEVICE_ID = "1"  

TOPIC_RELAY_COMMAND      = f"relaycommand{DEVICE_ID}"
TOPIC_RELAY_STATUS       = f"relaystatus{DEVICE_ID}"
TOPIC_SSR_COMMAND        = f"ssrcommand{DEVICE_ID}"
TOPIC_SSR_STATUS         = f"ssrstatus{DEVICE_ID}"
TOPIC_DIGITAL_INPUT      = f"inputstatus{DEVICE_ID}"
TOPIC_ANALOG_IN          = f"analoginstatus{DEVICE_ID}"
TOPIC_ANALOG_OUT_COMMAND = f"analogoutcommand{DEVICE_ID}"
TOPIC_ANALOG_OUT_STATUS  = f"analogoutstatus{DEVICE_ID}"
TOPIC_OTA_COMMAND        = f"otacommand{DEVICE_ID}"
TOPIC_OTA_STATUS         = f"otastatus{DEVICE_ID}"
TOPIC_NTC                = f"ntc{DEVICE_ID}"

# Yeniden bağlanma ayarları
WIFI_RETRY_DELAY = 2
MQTT_BASE_BACKOFF = 1
MQTT_MAX_BACKOFF = 60


class PCA9535:
    def __init__(self, i2c, address=0x24):
        self.i2c_device = i2c_device.I2CDevice(i2c, address)
        self.config_port0 = 0xFF
        self.config_port1 = 0xFF
        self.output_port0 = 0x00
        self.output_port1 = 0x00
        self.write_register(6, self.config_port0)
        self.write_register(7, self.config_port1)
        self.write_register(2, self.output_port0)
        self.write_register(3, self.output_port1)

    def write_register(self, reg, val):
        with self.i2c_device as i2c:
            i2c.write(bytes([reg, val]))

    def read_register(self, reg):
        buf = bytearray(1)
        with self.i2c_device as i2c:
            i2c.write_then_readinto(bytes([reg]), buf)
        return buf[0]

    def setup_input(self, pin):
        if pin < 8:
            self.config_port0 |= (1 << pin)
            self.write_register(6, self.config_port0)
        else:
            pin -= 8
            self.config_port1 |= (1 << pin)
            self.write_register(7, self.config_port1)

    def setup_output(self, pin):
        if pin < 8:
            self.config_port0 &= ~(1 << pin)
            self.write_register(6, self.config_port0)
        else:
            pin -= 8
            self.config_port1 &= ~(1 << pin)
            self.write_register(7, self.config_port1)

    def read_pin(self, pin):
        if pin < 8:
            val = self.read_register(0)
            return (val >> pin) & 1
        else:
            pin -= 8
            val = self.read_register(1)
            return (val >> pin) & 1

    def write_pin(self, pin, value):
        if pin < 8:
            if value:
                self.output_port0 |= (1 << pin)
            else:
                self.output_port0 &= ~(1 << pin)
            self.write_register(2, self.output_port0)
        else:
            pin -= 8
            if value:
                self.output_port1 |= (1 << pin)
            else:
                self.output_port1 &= ~(1 << pin)
            self.write_register(3, self.output_port1)


# ============================================
# DONANIM KURULUMU
# ============================================

i2c = busio.I2C(scl=board.GP21, sda=board.GP20)
pca_in  = PCA9535(i2c, address=0x24)
pca_out = PCA9535(i2c, address=0x26)

RELAY_PINS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
for relay_pin in RELAY_PINS:
    pca_out.setup_output(relay_pin)
    pca_out.write_pin(relay_pin, 0)

ssr0 = DigitalInOut(board.GP6)
ssr0.direction = Direction.OUTPUT
ssr0.value = False

ssr1 = DigitalInOut(board.GP7)
ssr1.direction = Direction.OUTPUT
ssr1.value = False

ssr2 = DigitalInOut(board.GP14)
ssr2.direction = Direction.OUTPUT
ssr2.value = False

ssr3 = DigitalInOut(board.GP15)
ssr3.direction = Direction.OUTPUT
ssr3.value = False

DIGITAL_INPUT_PINS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
for di_pin in DIGITAL_INPUT_PINS:
    pca_in.setup_input(di_pin)

from digitalio import Pull
i16 = DigitalInOut(board.GP0)
i16.direction = Direction.INPUT
i16.pull = Pull.DOWN

i17 = DigitalInOut(board.GP1)
i17.direction = Direction.INPUT
i17.pull = Pull.DOWN

i18 = DigitalInOut(board.GP2)
i18.direction = Direction.INPUT
i18.pull = Pull.DOWN

i19 = DigitalInOut(board.GP3)
i19.direction = Direction.INPUT
i19.pull = Pull.DOWN

analog_in0 = analogio.AnalogIn(board.GP28)
analog_in1 = analogio.AnalogIn(board.GP27)

analog_out0 = pwmio.PWMOut(board.GP11, frequency=1000, duty_cycle=0)
analog_out1 = pwmio.PWMOut(board.GP12, frequency=1000, duty_cycle=0)

pool = None
mqtt_client = None

NOMINAL_RESISTANCE = 10000
NOMINAL_TEMPERATURE = 25
B_VALUE = 3950
REFERENCE_RESISTANCE = 10000
ntc = analogio.AnalogIn(board.GP26)


def read_temperature():
    v = ntc.value * 3.3 / 65535
    if v <= 0.01:
        return 0
    r = REFERENCE_RESISTANCE * (3.3 / v - 1)
    t = 1 / ((1 / (NOMINAL_TEMPERATURE + 273.15)) + (1 / B_VALUE) * math.log(r / NOMINAL_RESISTANCE))
    return round(t - 273.15, 1)


def safe_print(*args, **kwargs):
    print(*args, **kwargs)


# ============================================
# OTA GÜNCELLEME FONKSİYONU
# ============================================

def ota_update(url):
    """
    GitHub'dan yeni code.py indir ve uygula.
    storage.remount() KULLANILMAZ — os.rename() ile atomik güncelleme yapılır.
    """
    safe_print("OTA başlıyor:", url)
    try:
        mqtt_client.publish(TOPIC_OTA_STATUS, "OTA:BASLIYOR", retain=False)

        # HTTPS için SSL context
        ssl_ctx = ssl.create_default_context()
        r = adafruit_requests.Session(pool, ssl_ctx)

        safe_print("Kod indiriliyor...")
        mqtt_client.publish(TOPIC_OTA_STATUS, "OTA:INDIRILIYOR", retain=False)

        response = r.get(url, timeout=30)

        if response.status_code != 200:
            mqtt_client.publish(TOPIC_OTA_STATUS, f"OTA:HATA:HTTP{response.status_code}", retain=False)
            safe_print("HTTP hatası:", response.status_code)
            return False

        new_code = response.text
        response.close()

        safe_print(f"İndirme tamam ({len(new_code)} byte), yazılıyor...")
        mqtt_client.publish(TOPIC_OTA_STATUS, f"OTA:YAZILIYOR:{len(new_code)}byte", retain=False)

        # 1) Önce geçici dosyaya yaz
        with open("/code_new.py", "w") as f:
            f.write(new_code)

        # 2) Mevcut code.py'yi yedekle
        try:
            os.rename("/code.py", "/code_bak.py")
        except Exception:
            pass

        # 3) Yeni dosyayı code.py yap (atomik)
        os.rename("/code_new.py", "/code.py")

        safe_print("Yazma tamam! Reboot ediliyor...")
        mqtt_client.publish(TOPIC_OTA_STATUS, "OTA:TAMAMLANDI:REBOOT", retain=False)
        time.sleep(2)

        microcontroller.reset()

    except Exception as e:
        safe_print("OTA hatası:", e)
        # Hata olursa yedeği geri yükle
        try:
            os.rename("/code_bak.py", "/code.py")
            safe_print("Yedek geri yüklendi!")
        except Exception:
            pass
        try:
            mqtt_client.publish(TOPIC_OTA_STATUS, f"OTA:HATA:{str(e)[:50]}", retain=False)
        except Exception:
            pass
        return False


# ============================================
# WİFİ / MQTT
# ============================================

def wifi_connect():
    try:
        if wifi.radio.ipv4_address:
            safe_print("Wi-Fi zaten bağlı, IP:", wifi.radio.ipv4_address)
            return True
    except Exception:
        pass

    safe_print("Wi-Fi'ye bağlanılıyor...")
    try:
        wifi.radio.connect(WIFI_SSID, WIFI_PASS)
        start = time.monotonic()
        while not wifi.radio.ipv4_address:
            if time.monotonic() - start > 15:
                safe_print("Wi-Fi IP alınamadı.")
                return False
            time.sleep(0.5)
        safe_print("Wi-Fi bağlı, IP:", wifi.radio.ipv4_address)
        return True
    except Exception as e:
        safe_print("Wi-Fi bağlanırken hata:", e)
        return False


def make_mqtt_client():
    global pool
    try:
        pool = socketpool.SocketPool(wifi.radio)
        ssl_context = ssl.create_default_context()
        client = MQTT.MQTT(
            broker=MQTT_BROKER,
            port=MQTT_PORT,
            username=MQTT_USER,
            password=MQTT_PASS,
            socket_pool=pool,
            client_id=CLIENT_ID,
            ssl_context=ssl_context,
            is_ssl=True
        )
        return client
    except Exception as e:
        safe_print("MQTT client oluşturulamadı:", e)
        return None


def set_analog_output(channel, voltage):
    if voltage < 0:
        voltage = 0
    elif voltage > 10:
        voltage = 10
    duty = int((voltage / 10.0) * 65535)
    if channel == 0:
        analog_out0.duty_cycle = duty
    elif channel == 1:
        analog_out1.duty_cycle = duty


def read_analog_input(channel):
    ain = analog_in0 if channel == 0 else analog_in1
    voltage = (ain.value / 65535) * 3.3
    real_voltage = voltage * (10.0 / 3.3)
    return round(real_voltage, 2)


def publish_analog_input():
    try:
        v0 = read_analog_input(0)
        v1 = read_analog_input(1)
        safe_print("Voltaj AI0:", v0, "AI1:", v1)
        mqtt_client.publish(TOPIC_ANALOG_IN, f"{v0},{v1}", retain=False)
    except Exception as e:
        safe_print("Analog giriş publish hatası:", e)


def publish_digital_inputs():
    try:
        input_states = []
        for pin in DIGITAL_INPUT_PINS:
            state = pca_in.read_pin(pin)
            input_states.append(str(state))
        input_states.append(str(int(i16.value)))
        input_states.append(str(int(i17.value)))
        input_states.append(str(int(i18.value)))
        input_states.append(str(int(i19.value)))
        status = ",".join(input_states)
        mqtt_client.publish(TOPIC_DIGITAL_INPUT, status, retain=False)
    except Exception as e:
        safe_print("Dijital giriş publish hatası:", e)


def publish_ssr_status():
    try:
        ssr_states = f"{int(ssr0.value)},{int(ssr1.value)},{int(ssr2.value)},{int(ssr3.value)}"
        mqtt_client.publish(TOPIC_SSR_STATUS, ssr_states, retain=True)
        safe_print(f"SSR durumu: {ssr_states}")
    except Exception as e:
        safe_print("SSR durum publish hatası:", e)


def publish_relay_status():
    try:
        relay_states = []
        for pin in RELAY_PINS:
            state = pca_out.read_pin(pin)
            relay_states.append(str(state))
        status = ",".join(relay_states)
        print(status)
        mqtt_client.publish(TOPIC_RELAY_STATUS, status, retain=True)
        safe_print(f"Röle durumu: {status}")
    except Exception as e:
        safe_print("Röle durum publish hatası:", e)


def subscribe_and_setup_callbacks(client):
    def on_message(client_inner, topic, message):
        if isinstance(message, (bytes, bytearray)):
            try:
                message = message.decode("utf-8")
            except Exception:
                message = repr(message)
        safe_print(f"Gelen mesaj: {topic} -> {message}")

        if topic == TOPIC_RELAY_COMMAND:
            try:
                parts = message.split(",")
                relay_no = int(parts[0])
                state = int(parts[1])
                if 0 <= relay_no < len(RELAY_PINS):
                    pca_out.write_pin(RELAY_PINS[relay_no], state)
                    safe_print(f"Röle {relay_no}: {'AÇIK' if state else 'KAPALI'}")
                    publish_relay_status()
            except Exception as e:
                safe_print("Röle komut hatası:", e)

        elif topic == TOPIC_SSR_COMMAND:
            try:
                parts = message.split(",")
                ssr_no = int(parts[0])
                state = int(parts[1])
                if ssr_no == 0:
                    ssr0.value = bool(state)
                elif ssr_no == 1:
                    ssr1.value = bool(state)
                elif ssr_no == 2:
                    ssr2.value = bool(state)
                elif ssr_no == 3:
                    ssr3.value = bool(state)
                safe_print(f"SSR {ssr_no}: {'AÇIK' if state else 'KAPALI'}")
                publish_ssr_status()
            except Exception as e:
                safe_print("SSR komut hatası:", e)

        elif topic == TOPIC_ANALOG_OUT_COMMAND:
            try:
                parts = message.split(",")
                channel = int(parts[0])
                voltage = float(parts[1])
                set_analog_output(channel, voltage)
                safe_print(f"Analog çıkış AQ{channel}: {voltage}V")
                mqtt_client.publish(TOPIC_ANALOG_OUT_STATUS, message, retain=True)
            except Exception as e:
                safe_print("Analog çıkış komut hatası:", e)

        elif topic == TOPIC_OTA_COMMAND:
            msg = message.strip()
            if msg == "UPDATE":
                safe_print("OTA komutu alındı! URL:", OTA_URL)
                ota_update(OTA_URL)
            elif msg.startswith("UPDATE:"):
                # Özel URL ile güncelleme: "UPDATE:https://..."
                custom_url = msg[7:].strip()
                safe_print("OTA özel URL:", custom_url)
                ota_update(custom_url)
            else:
                safe_print("Bilinmeyen OTA komutu:", msg)

        else:
            safe_print("Bilinmeyen mesaj:", message)

    client.on_message = on_message

    topics = [
        TOPIC_RELAY_COMMAND,
        TOPIC_SSR_COMMAND,
        TOPIC_ANALOG_OUT_COMMAND,
        TOPIC_OTA_COMMAND,
    ]
    for t in topics:
        try:
            client.subscribe(t)
            safe_print("Abone olundu:", t)
        except Exception as e:
            safe_print("Subscribe hatası:", t, e)


def try_connect_mqtt_with_backoff():
    global mqtt_client
    backoff = MQTT_BASE_BACKOFF
    while True:
        try:
            if mqtt_client is None:
                mqtt_client = make_mqtt_client()
                if mqtt_client is None:
                    safe_print("MQTT client yeniden oluşturulamadı; bekleniyor...")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, MQTT_MAX_BACKOFF)
                    continue

            safe_print("MQTT broker'a bağlanılıyor...")
            mqtt_client.connect()
            safe_print("MQTT'ye bağlandı.")
            subscribe_and_setup_callbacks(mqtt_client)
            return True
        except Exception as e:
            safe_print("MQTT connect hatası:", e)
            try:
                mqtt_client.disconnect()
            except Exception:
                pass
            mqtt_client = None
            safe_print(f"Yeniden denenecek {backoff} sn...")
            time.sleep(backoff)
            backoff = min(backoff * 2, MQTT_MAX_BACKOFF)


def is_network_ok():
    try:
        if wifi.radio.ipv4_address:
            return True
    except Exception:
        pass
    return False


def main():
    global mqtt_client

    safe_print("Başlatılıyor...")
    while not wifi_connect():
        safe_print(f"Wi-Fi bağlanamadı, {WIFI_RETRY_DELAY} sn sonra tekrar deneniyor...")
        time.sleep(WIFI_RETRY_DELAY)

    try_connect_mqtt_with_backoff()
    safe_print("Ana döngü başlıyor.")
    mqtt_backoff = MQTT_BASE_BACKOFF
    last_energy_t=0

    while True:
        
           

            try:
                mqtt_client.loop()
                mqtt_backoff = MQTT_BASE_BACKOFF
                now = time.monotonic()
                if now - last_energy_t >= 1:

                    publish_digital_inputs()
                    temp = read_temperature()
                    mqtt_client.publish(TOPIC_NTC, str(temp), retain=True)
                    print(f"Gönderilen sıcaklık: {temp} °C")
                    print("Relay commmand:",TOPIC_RELAY_COMMAND)
                    publish_analog_input()
                    last_energy_t = now

            except Exception as e_loop:
                safe_print("MQTT loop hatası:", e_loop)
                try:
                    mqtt_client.disconnect()
                except Exception:
                    pass
                mqtt_client = None
                if not is_network_ok():
                  while not wifi_connect():
                    for _ in range(15):  
                        time.sleep(1)
                try_connect_mqtt_with_backoff()

       


if __name__ == "__main__":
    main()
