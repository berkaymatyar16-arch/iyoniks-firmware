# ============================================================
#  IYONiKS Kombi Kontrol Sistemi  v2.4
#  PZEM-016 - voltaj/akim okuma
# ============================================================

import board, busio, digitalio, displayio, analogio
import terminalio, time, math, gc
import supervisor
from fourwire import FourWire
from adafruit_ili9341 import ILI9341
from adafruit_display_text import label
from adafruit_bus_device import i2c_device

# ---- OTA / MQTT icin ek kutuphaneler ----
import wifi
import socketpool
import ssl
import os
import microcontroller
import adafruit_minimqtt.adafruit_minimqtt as MQTT
import adafruit_requests

# ============================================================
#  0. OTA / MQTT AYARLARI
#  NOT: Bu blok sadece uzaktan guncelleme icin. Kombi kontrol
#  mantigi (ekran, elektrot, PZEM) bu ayarlardan etkilenmez.
# ============================================================

WIFI_SSID = "VODAFONE_5775"
WIFI_PASS = "r7iupxpvxn4"

MQTT_BROKER = "429cfdf2bbc44476b16e3466dfaea8e1.s1.eu.hivemq.cloud"
MQTT_USER   = "iyoniks"
MQTT_PASS   = "Iyoniks123"
MQTT_PORT   = 8883
CLIENT_ID   = "anka_kombi_1"

OTA_URL = "https://raw.githubusercontent.com/berkaymatyar16-arch/iyoniks-firmware/main/code.py"

DEVICE_ID = "1"

TOPIC_OTA_COMMAND   = f"otacommand{DEVICE_ID}"
TOPIC_OTA_STATUS    = f"otastatus{DEVICE_ID}"
TOPIC_NTC           = f"ntc{DEVICE_ID}"
TOPIC_RELAY_STATUS  = f"relaystatus{DEVICE_ID}"
TOPIC_AKIM          = f"akim{DEVICE_ID}"

WIFI_RETRY_INTERVAL  = 30   # wifi kopuksa kac saniyede bir tekrar denesin
MQTT_RETRY_INTERVAL  = 15   # mqtt kopuksa kac saniyede bir tekrar denesin
TELEMETRI_INTERVAL   = 5    # durum verisi kac saniyede bir yayinlansin
MQTT_LOOP_INTERVAL   = 4.0  # mqtt_client.loop() en fazla bu sikilikta cagrilsin (butonlarin kilitlenmemesi icin seyreltildi)

_pool         = None
_mqtt_client  = None
_wifi_baglandi_mi = False
_mqtt_baglandi_mi = False
_son_wifi_deneme  = -9999.0
_son_mqtt_deneme  = -9999.0
_son_telemetri    = 0.0
_son_mqtt_loop    = -9999.0

# ============================================================
#  1. DONANIM
# ============================================================

gc.collect()

buzzer = digitalio.DigitalInOut(board.GP8)
buzzer.direction = digitalio.Direction.OUTPUT
buzzer.value = False

ntc_pin = analogio.AnalogIn(board.GP27)
ain_pin = analogio.AnalogIn(board.GP28)

de_re = digitalio.DigitalInOut(board.GP10)
de_re.direction = digitalio.Direction.OUTPUT
de_re.value = False

time.sleep(0.5)
gc.collect()

displayio.release_displays()
tft_spi = busio.SPI(clock=board.GP14, MOSI=board.GP15)
display_bus = FourWire(tft_spi, command=board.GP6,
                       chip_select=board.GP13, reset=board.GP7)
display = ILI9341(display_bus, width=320, height=240, rotation=180)
gc.collect()

i2c = busio.I2C(scl=board.GP21, sda=board.GP20)
_t0 = time.monotonic()
while not i2c.try_lock():
    if time.monotonic() - _t0 > 3.0:
        break
i2c.unlock()
time.sleep(0.1)

uart = busio.UART(tx=board.GP4, rx=board.GP5,
                  baudrate=9600, bits=8, parity=None, stop=1, timeout=2)

# ============================================================
#  2. PCA9535
# ============================================================

class PCA:
    def __init__(self, i2c_bus, addr=0x24):
        self.dev = i2c_device.I2CDevice(i2c_bus, addr)
        self._p1 = 0xFF

    def _w(self, reg, val):
        try:
            with self.dev as d:
                d.write(bytes([reg, val]))
        except Exception as e:
            print("I2C yaz:", e)

    def _r(self, reg):
        b = bytearray(1)
        try:
            with self.dev as d:
                d.write_then_readinto(bytes([reg]), b)
        except Exception as e:
            print("I2C oku:", e)
            return 0xFF
        return b[0]

    def init(self):
        self._w(6, 0b11111111)
        self._w(7, 0b00000111)
        self._w(2, 0x00)
        self._w(3, 0x00)
        self._p1 = 0x00

    def port1_yaz_eger_degistiyse(self, yeni_deger):
        yeni_deger = yeni_deger & 0xFF
        if yeni_deger == self._p1:
            return
        self._p1 = yeni_deger
        try:
            self._w(3, self._p1)
        except Exception:
            pass

    def port0_oku(self):
        try:
            return self._r(0)
        except Exception:
            return 0xFF

    def port1_giris_oku(self):
        try:
            return self._r(1)
        except Exception:
            return None

pca = None
for _deneme in range(5):
    try:
        pca = PCA(i2c)
        pca.init()
        break
    except Exception:
        time.sleep(0.3)

gc.collect()

# ============================================================
#  3. SABITLER
# ============================================================

M_Q0 = 0x08
M_Q1 = 0x10
M_Q2 = 0x20
M_Q3 = 0x40
M_Q4 = 0x80

P2_AC_SICAKLIK   = 70.0
P2_KAPA_SICAKLIK = 65.0
VOLTAJ = 228.0

# ============================================================
#  4. ENERJi TAKiP
# ============================================================

elec_sure = [0.0, 0.0, 0.0]
elec_on   = [False, False, False]
elec_bas  = [None, None, None]

kwh_bugun  = 0.0
kwh_son_t  = None

def elektrot_sure_guncelle(q0a, q1a, q2a, now):
    aktifler = [q0a, q1a, q2a]
    for i in range(3):
        if aktifler[i] and not elec_on[i]:
            elec_bas[i] = now
        elif not aktifler[i] and elec_on[i]:
            if elec_bas[i] is not None:
                elec_sure[i] += now - elec_bas[i]
                elec_bas[i] = None
        elif aktifler[i] and elec_on[i]:
            if elec_bas[i] is not None and (now - elec_bas[i]) > 60.0:
                elec_sure[i] += now - elec_bas[i]
                elec_bas[i] = now
        elec_on[i] = aktifler[i]

def kwh_guncelle(now):
    global kwh_bugun, kwh_son_t
    if _pzem_akim is None or _pzem_voltaj is None:
        kwh_son_t = now
        return
    watt = _pzem_akim * _pzem_voltaj
    if kwh_son_t is not None:
        dt_h = (now - kwh_son_t) / 3600.0
        kwh_bugun += (watt / 1000.0) * dt_h
    kwh_son_t = now

def elec_saat(i):
    s = elec_sure[i]
    if elec_bas[i] is not None:
        s += time.monotonic() - elec_bas[i]
    return s / 3600.0

def elec_saniye(i):
    s = elec_sure[i]
    if elec_bas[i] is not None:
        s += time.monotonic() - elec_bas[i]
    return s

def elec_toplam_saniye():
    return elec_saniye(0) + elec_saniye(1) + elec_saniye(2)

# ============================================================
#  5. NTC
# ============================================================

NTC_BETA  = 3950
NTC_R0    = 10000
NTC_T0    = 298.15
NTC_RPULL = 10000
NTC_VCC   = 3.3
_son_gecerli = None
_okuma_buf   = []
MAX_ATLAMA   = 15.0

def read_ntc():
    global _son_gecerli, _okuma_buf
    try:
        total = 0
        for _ in range(8):
            total += ntc_pin.value
        v = (total / 8 / 65535.0) * NTC_VCC
        if v < 0.05 or v > 3.25:
            return _son_gecerli
        r = NTC_RPULL * (NTC_VCC - v) / v
        if r <= 0:
            return _son_gecerli
        t = 1.0 / ((1.0/NTC_T0) + (1.0/NTC_BETA)*math.log(r/NTC_R0)) - 273.15
        if not (-10 < t < 120):
            return _son_gecerli
        t = round(t, 1)
        if _son_gecerli is not None:
            if abs(t - _son_gecerli) > MAX_ATLAMA:
                return _son_gecerli
        _okuma_buf.append(t)
        if len(_okuma_buf) > 5:
            _okuma_buf.pop(0)
        _son_gecerli = round(sum(_okuma_buf) / len(_okuma_buf), 1)
        return _son_gecerli
    except Exception:
        return _son_gecerli

# ============================================================
#  6. PZEM-016
# ============================================================

_pzem_akim   = None
_pzem_voltaj = None
_pzem_watt   = None
_pzem_frekans = None
_pzem_pf     = None
_pzem_alarm  = None
_pzem_enerji = None

def _pzem_crc16(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])

def read_pzem():
    global _pzem_akim, _pzem_voltaj, _pzem_watt
    global _pzem_enerji, _pzem_frekans, _pzem_pf, _pzem_alarm
    try:
        # Tamponu temizle
        try:
            bek = uart.in_waiting
            if bek:
                uart.read(bek)
        except Exception:
            pass
        time.sleep(0.01)

        BEKLENEN = 25
        pkt = bytearray([0x01, 0x04, 0x00, 0x00, 0x00, 0x0A])
        pkt += _pzem_crc16(pkt)

        de_re.value = True
        time.sleep(0.02)
        uart.write(bytes(pkt))
        time.sleep(0.02)
        de_re.value = False

        tampon = bytearray()
        bitis = time.monotonic() + 2.0
        son_buton_tarama = 0.0
        while time.monotonic() < bitis:
            try:
                bek = uart.in_waiting
                if bek:
                    chunk = uart.read(bek)
                    if chunk:
                        tampon += chunk
            except Exception:
                pass
            if len(tampon) >= BEKLENEN:
                break
            simdi = time.monotonic()
            if simdi - son_buton_tarama >= 0.02:
                son_buton_tarama = simdi
                buton_hizli_tara()
            time.sleep(0.001)

        if len(tampon) < BEKLENEN:
            return False

        for i in range(len(tampon) - (BEKLENEN - 1)):
            if tampon[i] == 0x01 and tampon[i + 1] == 0x04:
                frame = tampon[i:i + BEKLENEN]
                if len(frame) < BEKLENEN:
                    continue
                if frame[2] != 0x14:
                    continue
                if _pzem_crc16(bytes(frame[:-2])) != bytes(frame[-2:]):
                    continue
                regs = []
                for j in range(10):
                    idx = 3 + j * 2
                    regs.append((frame[idx] << 8) | frame[idx + 1])

                _pzem_voltaj = round(regs[0] / 10.0, 1)

                # Akim - MiRDEV'in dogrulanmis tek formulu
                _pzem_akim = round(((regs[2] << 16) | regs[1]) / 1000.0, 3)

                _pzem_watt    = round(((regs[4] << 16) | regs[3]) / 10.0, 1)
                _pzem_enerji  = (regs[6] << 16) | regs[5]
                _pzem_frekans = round(regs[7] / 10.0, 1)
                _pzem_pf      = round(regs[8] / 100.0, 2)
                _pzem_alarm   = regs[9]
                return True

        return False
    except Exception:
        return False

def read_pzem_current():
    read_pzem()
    return _pzem_akim

# ============================================================
#  7. SSR
# ============================================================

PID_PERIYOT = 2.0
_q0_duty        = 0.0
_q1_duty        = 0.0
_q0_periyot_bas = None
_q1_periyot_bas = None
_q0_etkin       = False
_q1_etkin       = False

def ssr_guncelle(now):
    global _q0_periyot_bas, _q1_periyot_bas
    if pca is None:
        return
    q0_on = False
    q1_on = False
    if _q0_etkin:
        if _q0_periyot_bas is None:
            _q0_periyot_bas = now
        sure = now - _q0_periyot_bas
        if sure >= PID_PERIYOT:
            _q0_periyot_bas = now
            sure = 0.0
        q0_on = sure < (_q0_duty * PID_PERIYOT)
    else:
        _q0_periyot_bas = None
    if _q1_etkin:
        if _q1_periyot_bas is None:
            _q1_periyot_bas = now
        sure = now - _q1_periyot_bas
        if sure >= PID_PERIYOT:
            _q1_periyot_bas = now
            sure = 0.0
        q1_on = sure < (_q1_duty * PID_PERIYOT)
    else:
        _q1_periyot_bas = None
    port1_uygula(q0_on, q1_on, bool(pca._p1 & M_Q2),
                 bool(pca._p1 & M_Q4), bool(pca._p1 & M_Q3))

# ============================================================
#  8. EKRAN
# ============================================================

splash = displayio.Group()
display.root_group = splash

bg_bmp = displayio.Bitmap(320, 240, 1)
bg_pal = displayio.Palette(1)
bg_pal[0] = 0xFFFFFF
splash.append(displayio.TileGrid(bg_bmp, pixel_shader=bg_pal))

hdr_bmp = displayio.Bitmap(320, 18, 1)
hdr_pal = displayio.Palette(1)
hdr_pal[0] = 0x1B4D89
splash.append(displayio.TileGrid(hdr_bmp, pixel_shader=hdr_pal, x=0, y=0))

sic_cerceve_bmp = displayio.Bitmap(316, 86, 1)
sic_cerceve_pal = displayio.Palette(1)
sic_cerceve_pal[0] = 0x0055AA
splash.append(displayio.TileGrid(sic_cerceve_bmp, pixel_shader=sic_cerceve_pal, x=2, y=19))

sic_ic_bmp = displayio.Bitmap(312, 82, 1)
sic_ic_pal = displayio.Palette(1)
sic_ic_pal[0] = 0xF0F4F8
splash.append(displayio.TileGrid(sic_ic_bmp, pixel_shader=sic_ic_pal, x=4, y=21))

gc.collect()

eq_w = 96
eq_h = 50
eq_bmp0 = displayio.Bitmap(eq_w, eq_h, 1)
eq_pal0 = displayio.Palette(1)
eq_pal0[0] = 0xF0F0F3
splash.append(displayio.TileGrid(eq_bmp0, pixel_shader=eq_pal0, x=8,   y=107))
eq_bmp1 = displayio.Bitmap(eq_w, eq_h, 1)
eq_pal1 = displayio.Palette(1)
eq_pal1[0] = 0xF0F0F3
splash.append(displayio.TileGrid(eq_bmp1, pixel_shader=eq_pal1, x=112, y=107))
eq_bmp2 = displayio.Bitmap(eq_w, eq_h, 1)
eq_pal2 = displayio.Palette(1)
eq_pal2[0] = 0xF0F0F3
splash.append(displayio.TileGrid(eq_bmp2, pixel_shader=eq_pal2, x=216, y=107))
eq_pal_list = [eq_pal0, eq_pal1, eq_pal2]

gc.collect()

p1_bmp = displayio.Bitmap(156, 50, 1)
p1_pal = displayio.Palette(1)
p1_pal[0] = 0xF0F0F3
splash.append(displayio.TileGrid(p1_bmp, pixel_shader=p1_pal, x=2, y=159))

p2_bmp = displayio.Bitmap(156, 50, 1)
p2_pal = displayio.Palette(1)
p2_pal[0] = 0xF0F0F3
splash.append(displayio.TileGrid(p2_bmp, pixel_shader=p2_pal, x=161, y=159))

alt_bmp = displayio.Bitmap(320, 29, 1)
alt_pal = displayio.Palette(1)
alt_pal[0] = 0x1B4D89
splash.append(displayio.TileGrid(alt_bmp, pixel_shader=alt_pal, x=0, y=211))

gc.collect()

sbar_bg_bmp = displayio.Bitmap(150, 5, 1)
sbar_bg_pal = displayio.Palette(1)
sbar_bg_pal[0] = 0xF0F0F3
splash.append(displayio.TileGrid(sbar_bg_bmp, pixel_shader=sbar_bg_pal, x=4, y=97))
sbar_fg_bmp = displayio.Bitmap(1, 5, 1)
sbar_fg_pal = displayio.Palette(1)
sbar_fg_pal[0] = 0x0066FF
sbar_tile = displayio.TileGrid(sbar_fg_bmp, pixel_shader=sbar_fg_pal, x=4, y=97)
splash.append(sbar_tile)
sbar_w = 0

gc.collect()

# ETiKETLER
def _lbl(txt, color, scale, x, y, anchor=(0.0, 0.0)):
    l = label.Label(terminalio.FONT, text=txt, color=color, scale=scale)
    l.anchor_point = anchor
    l.anchored_position = (x, y)
    splash.append(l)
    return l

lbl_baslik = _lbl("IYONiKS KOMBI", 0xFFFFFF, 1, 4,   4)
lbl_ver    = _lbl("v2.4-BEYAZ",   0x5A7A9A, 1, 250,  4)
lbl_durum  = _lbl("* AKTiF",       0x00FF88, 1, 155,  4)
lbl_mod    = _lbl("* KIS *",       0x88CCFF, 1, 230,  4)

gc.collect()

_lbl("KAZAN", 0x5A7A9A, 1, 160, 18, (0.5, 0.0))
lbl_sicaklik = _lbl("--.-", 0x0A2A4A, 5, 160, 28, (0.5, 0.0))
_lbl("C", 0x5A7A9A, 2, 242, 55, (0.0, 0.0))
lbl_hd = _lbl("HEDEF 70C", 0x5A7A9A, 1, 160, 88, (0.5, 0.0))

gc.collect()

gc.collect()

EQ_CX   = [56, 160, 264]
EQ_ISIM = ["Q0", "Q1", "Q2"]
lbl_eq_isim = []
lbl_eq_saat = []
for i in range(3):
    li = _lbl(EQ_ISIM[i], 0xFF5555, 2, EQ_CX[i], 111, (0.5, 0.0))
    ls = _lbl("0sn",       0xFF8888, 2, EQ_CX[i], 132, (0.5, 0.0))
    lbl_eq_isim.append(li)
    lbl_eq_saat.append(ls)

gc.collect()

FAN_KARE = [">|", "/\\", "|<", "\\/"]
FAN_DUR  = " +"
fan_idx  = 0
son_fan  = 0.0

lbl_p1_fan = _lbl(FAN_DUR,    0x9AA3B0, 2, 8,   178, (0.0, 0.0))
lbl_p1_ad  = _lbl("P1 KAZAN", 0x9AA3B0, 1, 44,  180, (0.0, 0.0))
lbl_p1_alt = _lbl("",         0x9AA3B0, 1, 44,  192, (0.0, 0.0))
lbl_p2_fan = _lbl(FAN_DUR,    0x9AA3B0, 2, 169, 178, (0.0, 0.0))
lbl_p2_ad  = _lbl("P2 PETEK", 0x9AA3B0, 1, 205, 180, (0.0, 0.0))
lbl_p2_alt = _lbl("",         0x9AA3B0, 1, 205, 192, (0.0, 0.0))

gc.collect()

_lbl("TOPLAM", 0xFFFFFF, 1, 4, 216, (0.0, 0.0))
lbl_toplam_h = _lbl("0sn", 0x00FF88, 2, 58, 213, (0.0, 0.0))
lbl_mesaj = _lbl("", 0xFFFFFF, 1, 316, 228, (1.0, 0.0))
lbl_termo_durum = _lbl("", 0xFFFFFF, 1, 316, 217, (1.0, 0.0))

gc.collect()

# ---- OTA / uzaktan guncelleme banner (buyuk, renkli, dikkat cekici) ----
ota_banner_grp = displayio.Group()
ota_bg_bmp = displayio.Bitmap(320, 58, 1)
ota_bg_pal = displayio.Palette(1)
ota_bg_pal[0] = 0x00CFFF
ota_banner_grp.append(displayio.TileGrid(ota_bg_bmp, pixel_shader=ota_bg_pal, x=0, y=91))

lbl_ota_baslik = label.Label(terminalio.FONT, text="", color=0x000000, scale=2)
lbl_ota_baslik.anchor_point = (0.5, 0.0)
lbl_ota_baslik.anchored_position = (160, 97)
ota_banner_grp.append(lbl_ota_baslik)

lbl_ota_alt = label.Label(terminalio.FONT, text="", color=0x000000, scale=1)
lbl_ota_alt.anchor_point = (0.5, 0.0)
lbl_ota_alt.anchored_position = (160, 124)
ota_banner_grp.append(lbl_ota_alt)

ota_banner_grp.hidden = True
splash.append(ota_banner_grp)  # en sonda eklendi -> en ustte cizilir

def ota_banner_goster(baslik, alt, renk):
    ota_bg_pal[0] = renk
    lbl_ota_baslik.text = baslik
    lbl_ota_alt.text = alt
    ota_banner_grp.hidden = False

def ota_banner_gizle():
    ota_banner_grp.hidden = True

gc.collect()

# ---- Termostat durum banner'i (buyuk, ortada, 5sn sonra kendiliginden kapanir) ----
termo_banner_grp = displayio.Group()
termo_bg_bmp = displayio.Bitmap(320, 58, 1)
termo_bg_pal = displayio.Palette(1)
termo_bg_pal[0] = 0x00CC00
termo_banner_grp.append(displayio.TileGrid(termo_bg_bmp, pixel_shader=termo_bg_pal, x=0, y=91))

lbl_termo_banner = label.Label(terminalio.FONT, text="", color=0x000000, scale=2)
lbl_termo_banner.anchor_point = (0.5, 0.5)
lbl_termo_banner.anchored_position = (160, 120)
termo_banner_grp.append(lbl_termo_banner)

termo_banner_grp.hidden = True
splash.append(termo_banner_grp)  # en sonda -> en ustte cizilir

termo_banner_bitis = 0.0

def termo_banner_goster(txt, renk, sure=5.0):
    global termo_banner_bitis
    termo_bg_pal[0] = renk
    lbl_termo_banner.text = txt
    termo_banner_grp.hidden = False
    termo_banner_bitis = time.monotonic() + sure

gc.collect()

# ============================================================
#  9. SiSTEM DEGiSKENLERi
# ============================================================

sistem_ac      = True
hedef_sicaklik = 70
alarm_aktif    = False
alarm_esigi    = 80
alarm_reset    = 65
p2_aktif       = False
oda_termostat  = False
anot_dusus     = False
p1_aktif       = True
p1_dongu_bas   = 0.0
termostat_prev = None
di_ham_p0      = 0x00
di_ham_p1      = 0x00
btn_prev       = [False] * 5
btn_yakalanan  = [False] * 5  # PZEM/MQTT beklerken kacan basislari yakalar

def buton_hizli_tara():
    """
    Tek bir hizli I2C okumasi ile buton durumuna bakar, yeni basis varsa
    btn_yakalanan icine 'kaydeder'. PZEM/MQTT gibi uzun bekleme
    donguleri icinde cagirilarak basislarin kaybolmasini engeller.
    """
    if pca is None:
        return
    try:
        p0 = pca.port0_oku()
        for i in range(1, 5):
            basili = bool((p0 >> i) & 1)
            if basili and not btn_prev[i]:
                btn_yakalanan[i] = True
            btn_prev[i] = basili
    except Exception:
        pass
yaz_modu        = False
yaz_p1_bas65    = 0.0
yaz_p1_bas65ust = 0.0
standby_modu    = False
donma_koruma    = False
DONMA_ESIK      = 5.0
DONMA_CIKIS     = 15.0
son_ekran      = 0.0
son_modbus     = 0.0
son_gc         = 0.0
son_akim       = None
mesaj_bitis    = 0.0

# ============================================================
#  10. YARDIMCI
# ============================================================

def buzzer_bip(sure=0.05):
    buzzer.value = True
    time.sleep(sure)
    buzzer.value = False

def mesaj_goster(txt, sure=2.0, renk=0xFFFFFF):
    global mesaj_bitis
    lbl_mesaj.text  = txt
    lbl_mesaj.color = renk
    mesaj_bitis = time.monotonic() + sure

def port1_uygula(q0_ac, q1_ac, q2_ac, p2_ac, p1_ac=True):
    if pca is None:
        return
    deger = 0x00
    if q0_ac: deger |= M_Q0
    if q1_ac: deger |= M_Q1
    if q2_ac: deger |= M_Q2
    if p1_ac: deger |= M_Q3
    if p2_ac: deger |= M_Q4
    pca.port1_yaz_eger_degistiyse(deger)

def termostat_oku(p0=None):
    global oda_termostat, termostat_prev, di_ham_p0, di_ham_p1
    if pca is None:
        return oda_termostat
    if p0 is None:
        try:
            p0 = pca.port0_oku()
        except Exception:
            p0 = 0x00
    try:
        p1_raw = pca._r(1)
    except Exception:
        p1_raw = 0x00
    di_ham_p0 = p0
    di_ham_p1 = p1_raw
    p0_di = (p0 & 0b11100000) != 0
    p1_di = (p1_raw & 0b00000111) != 0
    yeni = p0_di or p1_di
    if termostat_prev is not None and yeni != termostat_prev:
        if yeni:
            termo_banner_goster("TERMOSTAT ACIK", 0x00CC00, 5.0)
        else:
            termo_banner_goster("TERMOSTAT KAPALI", 0xCC3300, 5.0)
    termostat_prev = yeni
    oda_termostat  = yeni
    lbl_termo_durum.text  = "TERMO: ACIK" if yeni else "TERMO: KAPALI"
    lbl_termo_durum.color = 0x00CC00 if yeni else 0x999999
    return oda_termostat

def fmt1(v):
    return str(int(v * 10) / 10.0)

def fmt2(v):
    return str(int(v * 100) / 100.0)

def fmti(v):
    return str(int(v))

def sure_format(toplam_sn):
    toplam_sn = int(toplam_sn)
    if toplam_sn < 60:
        return str(toplam_sn) + "sn"
    elif toplam_sn < 3600:
        dk = toplam_sn // 60
        sn = toplam_sn % 60
        return str(dk) + "dk " + str(sn) + "sn"
    else:
        sa = toplam_sn // 3600
        kalan = toplam_sn % 3600
        dk = kalan // 60
        sn = kalan % 60
        return str(sa) + "sa " + str(dk) + "dk " + str(sn) + "sn"

def sic_bar_guncelle(sic):
    if sic is None:
        return
    if sic < 55:
        sbar_fg_pal[0] = 0x0044CC
    elif sic < 65:
        sbar_fg_pal[0] = 0x00AA44
    elif sic < 70:
        sbar_fg_pal[0] = 0xEE8800
    else:
        sbar_fg_pal[0] = 0xEE2200

# ============================================================
#  11. BUTON
# ============================================================

def buton_oku(basilanlar):
    global yaz_modu, yaz_p1_bas65, yaz_p1_bas65ust
    global standby_modu, donma_koruma, sistem_ac
    press = basilanlar  # zaten kenar-yakalanmis (latched) basis dizisi
    if press[1]:
        yaz_modu = True
        yaz_p1_bas65 = 0.0
        yaz_p1_bas65ust = 0.0
        buzzer_bip(0.1)
    if press[2]:
        yaz_modu = False
        yaz_p1_bas65 = 0.0
        yaz_p1_bas65ust = 0.0
        buzzer_bip(0.05)
    if press[3]:
        standby_modu = not standby_modu
        donma_koruma = False
        if standby_modu:
            sistem_ac = False
            mesaj_goster("STANDBY", 3.0, 0xAAAAAA)
            buzzer_bip(0.05)
        else:
            sistem_ac = True
            mesaj_goster("SISTEM AKTIF", 3.0, 0x00FF88)
            buzzer_bip(0.1)

# ============================================================
#  12. KONTROL
# ============================================================

def kontrol(sicaklik, now):
    global alarm_aktif, sistem_ac, p2_aktif
    global _q0_etkin, _q1_etkin, _q0_duty, _q1_duty
    global p1_aktif, p1_dongu_bas, anot_dusus
    global yaz_modu, yaz_p1_bas65, yaz_p1_bas65ust
    global standby_modu, donma_koruma

    if sicaklik is not None and sicaklik >= alarm_esigi and not alarm_aktif:
        alarm_aktif = True
        sistem_ac   = False
        buzzer.value = True
        _q0_etkin = False
        _q1_etkin = False
        port1_uygula(False, False, False, False, False)
        elektrot_sure_guncelle(False, False, False, now)
        mesaj_goster("! ISI COK YUKSELDI !", 5.0, 0xFF0000)
        return

    if alarm_aktif:
        _q0_etkin = False
        _q1_etkin = False
        port1_uygula(False, False, False, False, False)
        elektrot_sure_guncelle(False, False, False, now)
        if sicaklik is not None and sicaklik <= alarm_reset:
            alarm_aktif  = False
            buzzer.value = False
            mesaj_goster("ALARM TEMIZLENDI", 3.0, 0xFFAA00)
        return

    if not sistem_ac:
        if standby_modu and sicaklik is not None:
            if not donma_koruma and sicaklik <= DONMA_ESIK:
                donma_koruma = True
                mesaj_goster("DONMA KORUMA AKTIF", 5.0, 0xFF4400)
                buzzer.value = True
                time.sleep(0.2)
                buzzer.value = False
            if donma_koruma:
                if sicaklik >= DONMA_CIKIS:
                    donma_koruma = False
                    if pca:
                        pca.port1_yaz_eger_degistiyse(0x00)
                    elektrot_sure_guncelle(False, False, False, now)
                    mesaj_goster("STANDBY", 3.0, 0xAAAAAA)
                    return
                _q0_etkin = False
                _q1_etkin = True
                _q0_duty  = 0.0
                _q1_duty  = 0.3
                if pca:
                    deger = M_Q3 | M_Q4
                    pca._p1 = ~deger & 0xFF
                    pca.port1_yaz_eger_degistiyse(deger)
                ssr_guncelle(now)
                elektrot_sure_guncelle(False, True, False, now)
                return
        _q0_etkin = False
        _q1_etkin = False
        _q0_duty  = 0.0
        _q1_duty  = 0.0
        if pca:
            pca.port1_yaz_eger_degistiyse(0x00)
        elektrot_sure_guncelle(False, False, False, now)
        return

    if sicaklik is None:
        _q0_etkin = False
        _q1_etkin = False
        port1_uygula(False, False, False, False, False)
        elektrot_sure_guncelle(False, False, False, now)
        mesaj_goster("SENSOR HATASI!", 3.0, 0xFF0000)
        return

    # P1 pompa
    if yaz_modu:
        if sicaklik >= 65.0:
            if yaz_p1_bas65ust == 0.0:
                yaz_p1_bas65ust = now
            sure = (now - yaz_p1_bas65ust) % 240.0
            p1_aktif = sure < 120.0
            yaz_p1_bas65 = 0.0
        else:
            if yaz_p1_bas65 == 0.0:
                yaz_p1_bas65 = now
            sure = (now - yaz_p1_bas65) % 60.0
            p1_aktif = sure < 30.0
            yaz_p1_bas65ust = 0.0
    else:
        if 60.0 <= sicaklik <= 65.0:
            if p1_dongu_bas == 0.0:
                p1_dongu_bas = now
                p1_aktif = True
            sure = (now - p1_dongu_bas) % 60.0
            p1_aktif = sure < 30.0
        else:
            p1_aktif     = True
            p1_dongu_bas = 0.0

    # P2 pompa (petek) - oda termostati YOK, sadece sicakliga gore calisir
    if yaz_modu:
        p2_aktif = False
    elif sicaklik >= 70.0:
        p2_aktif = True
    elif sicaklik <= 65.0:
        p2_aktif = False
    # 65-70C arasi: p2_aktif oldugu gibi kalir (histerezis)

    # Elektrot kademesi
    if sicaklik >= 70.0:
        anot_dusus = True
        _q0_etkin = False
        _q1_etkin = False
        _q0_duty  = 0.0
        _q1_duty  = 0.0
        if pca is not None:
            deger = 0x00
            if p1_aktif: deger |= M_Q3
            if p2_aktif: deger |= M_Q4
            pca._p1 = ~deger & 0xFF
            pca.port1_yaz_eger_degistiyse(deger)
        elektrot_sure_guncelle(False, False, False, now)
        return

    if anot_dusus:
        if sicaklik > 67.0:
            # 70 -> 67C: hicbir elektrot calismaz
            _q0_etkin = False
            _q1_etkin = False
            _q0_duty  = 0.0
            _q1_duty  = 0.0
            if pca is not None:
                deger = 0x00
                if p1_aktif: deger |= M_Q3
                if p2_aktif: deger |= M_Q4
                pca._p1 = ~deger & 0xFF
                pca.port1_yaz_eger_degistiyse(deger)
            elektrot_sure_guncelle(False, False, False, now)
            return
        elif sicaklik > 65.0:
            # 67 -> 65C: sadece Q2 calisir
            _q0_etkin = False
            _q1_etkin = False
            _q0_duty  = 0.0
            _q1_duty  = 0.0
            port1_uygula(False, False, True, p2_aktif, p1_aktif)
            elektrot_sure_guncelle(False, False, True, now)
            return
        else:
            anot_dusus = False

    q2_ac = False
    if sicaklik >= 65.0:
        # 65-70C bandi: Q1 ve Q2 calisir (Q0 kapali)
        _q0_etkin = False
        _q1_etkin = True
        _q0_duty  = 0.0
        _q1_duty  = 1.0
        q2_ac     = True
        port1_uygula(False, True, True, p2_aktif, p1_aktif)
    elif sicaklik >= 60.0:
        _q0_etkin = True
        _q1_etkin = True
        _q0_duty  = 1.0
        _q1_duty  = 1.0
        port1_uygula(True, True, False, p2_aktif, p1_aktif)
    else:
        q2_ac     = True
        _q0_etkin = True
        _q1_etkin = True
        _q0_duty  = 1.0
        _q1_duty  = 1.0
        port1_uygula(True, True, True, p2_aktif, p1_aktif)

    elektrot_sure_guncelle(_q0_etkin, _q1_etkin, q2_ac, now)
    ssr_guncelle(now)

# ============================================================
#  13. EKRAN GUNCELLE
# ============================================================

def ekran_guncelle(sicaklik, akim, now):
    global fan_idx, son_fan, sbar_w

    if alarm_aktif:
        bg_pal[0] = 0xFF2244 if int(now * 2) % 2 == 0 else 0xFFFFFF
    else:
        bg_pal[0] = 0xFFFFFF

    if alarm_aktif:
        lbl_durum.text  = "! ALARM !"
        lbl_durum.color = 0xFF2200
    elif donma_koruma:
        lbl_durum.text  = "DONMA!"
        lbl_durum.color = 0xFF4400
    elif standby_modu:
        lbl_durum.text  = "STANDBY"
        lbl_durum.color = 0x9AA3B0
    elif not sistem_ac:
        lbl_durum.text  = "KAPALI"
        lbl_durum.color = 0xFFAA00
    else:
        lbl_durum.text  = "* AKTiF"
        lbl_durum.color = 0x00DD66

    if yaz_modu:
        lbl_mod.text  = "* YAZ *"
        lbl_mod.color = 0xFFDD00
    else:
        lbl_mod.text  = "* KIS *"
        lbl_mod.color = 0x88CCFF

    if sicaklik is not None:
        lbl_sicaklik.text = fmt1(sicaklik)
        if alarm_aktif:
            lbl_sicaklik.color = 0xFF3333
        elif sicaklik >= 68.0:
            lbl_sicaklik.color = 0xFF9900
        else:
            lbl_sicaklik.color = 0x0A2A4A
        sic_cerceve_pal[0] = 0xFF2255 if alarm_aktif else 0x0055AA
    else:
        lbl_sicaklik.text  = "--.-"
        lbl_sicaklik.color = 0x9AA3B0

    sic_bar_guncelle(sicaklik)
    lbl_hd.text = "HEDEF " + fmti(hedef_sicaklik) + "C"

    p1_reg = pca._p1 if pca else 0x00

    q2_aktif    = bool(p1_reg & M_Q2)
    sq_durumlar = [_q0_etkin, _q1_etkin, q2_aktif]
    for i in range(3):
        aktif = sq_durumlar[i]
        if aktif:
            eq_pal_list[i][0]    = 0xD4F5DC
            lbl_eq_isim[i].color = 0x0F7A34
            lbl_eq_saat[i].color = 0x0F7A34
        else:
            eq_pal_list[i][0]    = 0xF0F0F3
            lbl_eq_isim[i].color = 0x9AA3B0
            lbl_eq_saat[i].color = 0x9AA3B0
        lbl_eq_saat[i].text = sure_format(elec_saniye(i))

    if now - son_fan >= 0.20:
        fan_idx = (fan_idx + 1) % 4
        son_fan = now

    if sistem_ac and not alarm_aktif and p1_aktif:
        p1_pal[0]       = 0xD4F5DC
        lbl_p1_fan.text  = FAN_KARE[fan_idx]
        lbl_p1_fan.color = 0x0F7A34
        lbl_p1_ad.color  = 0x0F7A34
    else:
        p1_pal[0]       = 0xF0F0F3
        lbl_p1_fan.text  = FAN_DUR
        lbl_p1_fan.color = 0x9AA3B0
        lbl_p1_ad.color  = 0x9AA3B0

    if p1_reg & M_Q4:
        p2_pal[0]       = 0xD4F5DC
        lbl_p2_fan.text  = FAN_KARE[(fan_idx + 2) % 4]
        lbl_p2_fan.color = 0x0F7A34
        lbl_p2_ad.color  = 0x0F7A34
    else:
        p2_pal[0]       = 0xF0F0F3
        lbl_p2_fan.text  = FAN_DUR
        lbl_p2_fan.color = 0x9AA3B0
        lbl_p2_ad.color  = 0x9AA3B0

    lbl_toplam_h.text = sure_format(elec_toplam_saniye())

    if now > mesaj_bitis and not alarm_aktif and sistem_ac:
        lbl_mesaj.text = ""

    if not termo_banner_grp.hidden and now > termo_banner_bitis:
        termo_banner_grp.hidden = True

# ============================================================
#  14. LOGO
# ============================================================

logo_group = displayio.Group()
logo_yuklu = False
for _yol in ["/sd/logo.bmp", "/logo.bmp"]:
    try:
        logo_bmp  = displayio.OnDiskBitmap(_yol)
        logo_tile = displayio.TileGrid(logo_bmp, pixel_shader=logo_bmp.pixel_shader)
        logo_group.append(logo_tile)
        logo_yuklu = True
        break
    except Exception:
        pass

gc.collect()

# ============================================================
#  14b. OTA / MQTT / WIFI (kombi kontrolunu ASLA bloklamaz)
# ============================================================

def _wifi_dene():
    """Tek seferlik, sinirli sureli wifi baglanti denemesi. Bloklamaz/patlamaz."""
    global _wifi_baglandi_mi, _pool
    try:
        if wifi.radio.ipv4_address:
            _wifi_baglandi_mi = True
            return True
    except Exception:
        pass
    if not WIFI_SSID:
        return False
    try:
        wifi.radio.connect(WIFI_SSID, WIFI_PASS, timeout=8)
        if wifi.radio.ipv4_address:
            _wifi_baglandi_mi = True
            _pool = socketpool.SocketPool(wifi.radio)
            print("Wi-Fi baglandi:", wifi.radio.ipv4_address)
            return True
    except Exception as e:
        print("Wi-Fi baglanti hatasi:", e)
    _wifi_baglandi_mi = False
    return False


def _mqtt_on_message(client_inner, topic, message):
    try:
        if isinstance(message, (bytes, bytearray)):
            message = message.decode("utf-8")
        msg = message.strip()
        print("MQTT mesaj:", topic, "->", msg)
        if topic == TOPIC_OTA_COMMAND:
            if msg == "UPDATE":
                _ota_guncelle(OTA_URL)
            elif msg.startswith("UPDATE:"):
                _ota_guncelle(msg[7:].strip())
    except Exception as e:
        print("MQTT mesaj isleme hatasi:", e)


def _mqtt_dene():
    """Tek seferlik mqtt baglanti denemesi. Bloklamaz/patlamaz."""
    global _mqtt_client, _mqtt_baglandi_mi
    if _pool is None:
        return False
    try:
        if _mqtt_client is None:
            ssl_ctx = ssl.create_default_context()
            _mqtt_client = MQTT.MQTT(
                broker=MQTT_BROKER,
                port=MQTT_PORT,
                username=MQTT_USER,
                password=MQTT_PASS,
                socket_pool=_pool,
                client_id=CLIENT_ID,
                ssl_context=ssl_ctx,
                is_ssl=True,
            )
            _mqtt_client.on_message = _mqtt_on_message
        _mqtt_client.connect()
        _mqtt_client.subscribe(TOPIC_OTA_COMMAND)
        _mqtt_baglandi_mi = True
        print("MQTT baglandi.")
        return True
    except Exception as e:
        print("MQTT baglanti hatasi:", e)
        _mqtt_baglandi_mi = False
        try:
            _mqtt_client.disconnect()
        except Exception:
            pass
        return False


def _telemetri_yayinla():
    """Kombinin canli durumunu MQTT'ye yayinlar. Hata olursa sessizce gecer."""
    try:
        if sicaklik_son[0] is not None:
            _mqtt_client.publish(TOPIC_NTC, str(sicaklik_son[0]), retain=True)
        if son_akim is not None:
            _mqtt_client.publish(TOPIC_AKIM, str(son_akim), retain=True)
        p1 = pca._p1 if pca else 0
        durum = f"Q0:{int(bool(p1 & M_Q0))},Q1:{int(bool(p1 & M_Q1))},Q2:{int(bool(p1 & M_Q2))},P1:{int(bool(p1 & M_Q3))},P2:{int(bool(p1 & M_Q4))}"
        _mqtt_client.publish(TOPIC_RELAY_STATUS, durum, retain=True)
    except Exception as e:
        print("Telemetri yayin hatasi:", e)


def _ota_guncelle(url):
    """
    GitHub'dan yeni code.py indirir ve atomik olarak degistirir.
    Basarisiz olursa yedegi geri yukler. Kombi kontrolunu etkilemez
    cunku sadece MQTT komutu geldiginde, bilinçli olarak cagrilir.
    """
    print("OTA basliyor:", url)
    try:
        try:
            ota_banner_goster("UZAKTAN GUNCELLEME", "Baslatiliyor...", 0x00CFFF)
        except Exception:
            pass
        try:
            _mqtt_client.publish(TOPIC_OTA_STATUS, "OTA:BASLIYOR", retain=False)
        except Exception:
            pass

        ssl_ctx = ssl.create_default_context()
        req = adafruit_requests.Session(_pool, ssl_ctx)

        try:
            ota_banner_goster("INDIRILIYOR...", "GitHub'dan kod aliniyor", 0xFFAA00)
        except Exception:
            pass
        try:
            _mqtt_client.publish(TOPIC_OTA_STATUS, "OTA:INDIRILIYOR", retain=False)
        except Exception:
            pass

        cache_buster = url + ("&" if "?" in url else "?") + "_ts=" + str(int(time.monotonic() * 1000))
        response = req.get(cache_buster, timeout=30)
        if response.status_code != 200:
            print("OTA HTTP hatasi:", response.status_code)
            try:
                ota_banner_goster("GUNCELLEME HATASI", f"HTTP {response.status_code}", 0xFF3333)
            except Exception:
                pass
            try:
                _mqtt_client.publish(TOPIC_OTA_STATUS, f"OTA:HATA:HTTP{response.status_code}", retain=False)
            except Exception:
                pass
            time.sleep(4)
            try:
                ota_banner_gizle()
            except Exception:
                pass
            return False

        yeni_kod = response.text
        response.close()

        try:
            ota_banner_goster("YAZILIYOR...", f"{len(yeni_kod)} byte", 0xFF00CC)
        except Exception:
            pass
        try:
            _mqtt_client.publish(TOPIC_OTA_STATUS, f"OTA:YAZILIYOR:{len(yeni_kod)}byte", retain=False)
        except Exception:
            pass

        with open("/code_new.py", "w") as f:
            f.write(yeni_kod)

        try:
            os.rename("/code.py", "/code_bak.py")
        except Exception:
            pass

        os.rename("/code_new.py", "/code.py")

        print("OTA tamam, yeniden baslatiliyor...")
        try:
            ota_banner_goster("GUNCELLEME TAMAM!", "Yeniden baslatiliyor...", 0x00FF88)
        except Exception:
            pass
        try:
            _mqtt_client.publish(TOPIC_OTA_STATUS, "OTA:TAMAMLANDI:REBOOT", retain=False)
        except Exception:
            pass
        time.sleep(2)
        microcontroller.reset()

    except Exception as e:
        print("OTA hatasi:", e)
        try:
            ota_banner_goster("GUNCELLEME HATASI", str(e)[:28], 0xFF3333)
        except Exception:
            pass
        try:
            os.rename("/code_bak.py", "/code.py")
            print("Yedek geri yuklendi.")
        except Exception:
            pass
        try:
            _mqtt_client.publish(TOPIC_OTA_STATUS, f"OTA:HATA:{str(e)[:50]}", retain=False)
        except Exception:
            pass
        time.sleep(4)
        try:
            ota_banner_gizle()
        except Exception:
            pass
        return False


def ag_servis(now):
    """
    Her ana dongu turunde cagrilir. Wi-Fi/MQTT ile ilgili HER SEYI
    try/except icine alir; burada olusacak hicbir hata kombi kontrol
    dongusunu asla durduramaz.
    """
    global _son_wifi_deneme, _son_mqtt_deneme, _son_telemetri, _mqtt_baglandi_mi, _son_mqtt_loop
    try:
        if not _wifi_baglandi_mi:
            if now - _son_wifi_deneme >= WIFI_RETRY_INTERVAL:
                _son_wifi_deneme = now
                _wifi_dene()
            return

        if not _mqtt_baglandi_mi:
            if now - _son_mqtt_deneme >= MQTT_RETRY_INTERVAL:
                _son_mqtt_deneme = now
                _mqtt_dene()
            return

        if now - _son_mqtt_loop >= MQTT_LOOP_INTERVAL:
            _son_mqtt_loop = now
            _mqtt_client.loop(timeout=1)

        if now - _son_telemetri >= TELEMETRI_INTERVAL:
            _son_telemetri = now
            _telemetri_yayinla()

    except Exception as e:
        print("Ag servis hatasi:", e)
        _mqtt_baglandi_mi = False


# sicaklik_son: ekran/telemetri arasinda son gecerli sicakligi tasimak icin kutu
sicaklik_son = [None]

# ============================================================
#  15. ANA DONGU
# ============================================================

LOGO_SURE  = 3.0
BOSTA_SURE = 999999.0
LOGO_MOD   = 0
DERECE_MOD = 1

mod               = LOGO_MOD
son_tus           = time.monotonic()
acilis_zamani     = time.monotonic()
acilis_tamamlandi = False

if logo_yuklu:
    display.root_group = logo_group
else:
    display.root_group = splash
    mod = DERECE_MOD

try:
    supervisor.disable_autoreload()
except Exception:
    pass

print("AnKA DwGLC v2.4 baslatiliyor...")
gc.collect()
buzzer_bip(0.1)
time.sleep(0.1)
buzzer_bip(0.1)

p0_ham = 0xFF
while True:
    try:
        now = time.monotonic()

        buton_hizli_tara()  # bloklamadan once ilk hizli tarama

        sicaklik = read_ntc()
        sicaklik_son[0] = sicaklik

        ag_servis(now)

        buton_hizli_tara()  # mqtt kontrolunden hemen sonra bir tarama daha

        if now - son_modbus >= 3.0:
            son_akim = read_pzem_current()  # kendi icinde de butonlari tarar
            kwh_guncelle(now)
            son_modbus = now

        buton_hizli_tara()

        if pca is not None:
            try:
                p0_ham = pca.port0_oku()
                termostat_oku(p0_ham)
            except Exception as e:
                print("PCA hatasi:", e)

        kontrol(sicaklik, now)

        tus_var = any(btn_yakalanan[1:5])

        if mod == LOGO_MOD:
            if tus_var:
                mod     = DERECE_MOD
                son_tus = now
                display.root_group = splash
                gc.collect()
            elif now - acilis_zamani < LOGO_SURE:
                pass
            elif not acilis_tamamlandi:
                acilis_tamamlandi = True
                mod     = DERECE_MOD
                son_tus = now
                display.root_group = splash
                gc.collect()

        elif mod == DERECE_MOD:
            if tus_var:
                son_tus = now
                buton_oku(btn_yakalanan)
                for i in range(1, 5):
                    btn_yakalanan[i] = False
            if logo_yuklu and (now - son_tus >= BOSTA_SURE):
                mod = LOGO_MOD
                display.root_group = logo_group
                gc.collect()
            else:
                if now - son_ekran >= 0.5:
                    ekran_guncelle(sicaklik, son_akim, now)
                    son_ekran = now

        if now - son_gc >= 15.0:
            gc.collect()
            son_gc = now

        time.sleep(0.1)

    except MemoryError as e:
        print("BELLEK HATASI:", e)
        gc.collect()
        time.sleep(0.5)
    except Exception as e:
        print("HATA:", type(e).__name__, e)
        try:
            if pca is not None:
                pca.port1_yaz_eger_degistiyse(0x00)
        except Exception:
            pass
        gc.collect()
        time.sleep(0.2)
