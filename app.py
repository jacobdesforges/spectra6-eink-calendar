import os
import hashlib
from datetime import datetime, date, timedelta
from flask import Flask, send_file, jsonify, request, make_response
from PIL import Image, ImageDraw, ImageFont
import caldav
from caldav.elements import dav, ical
from io import BytesIO
import requests
import time
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

app = Flask(__name__)

# Config Env w/ fallbacks
CALDAV_URL = os.getenv("CALDAV_URL", "http://radicale:5232/")
CALDAV_USER = os.getenv("CALDAV_USER", "")
CALDAV_PASSWORD = os.getenv("CALDAV_PASSWORD", "")

WEATHER_LATITUDE = float(os.getenv("WEATHER_LATITUDE", "40"))
WEATHER_LONGITUDE = float(os.getenv("WEATHER_LONGITUDE", "-74"))
WEATHER_TIMEZONE = os.getenv("WEATHER_TIMEZONE", "America%2FNew_York")
WEATHER_UNIT = os.getenv("WEATHER_UNIT", "F").upper()

CALENDAR_REFRESH_HOUR = int(os.getenv("CALENDAR_REFRESH_HOUR", "4"))
CALENDAR_REFRESH_MINUTE = int(os.getenv("CALENDAR_REFRESH_MINUTE", "15"))

# Physical Spectra 6 Solid Color Palette (RGB)
COLOR_WHITE = (255, 255, 255)
COLOR_BLACK = (0, 0, 0)
COLOR_RED = (255, 0, 0)
COLOR_YELLOW = (255, 255, 0)
COLOR_GREEN = (0, 200, 0)
COLOR_BLUE = (0, 0, 255)

SPECTRA_COLORS = [
    COLOR_WHITE,
    COLOR_BLACK,
    COLOR_RED,
    COLOR_YELLOW,
    COLOR_GREEN,
    COLOR_BLUE
]

FALLBACK_COLORS = [
    (31, 119, 180),   # Steel Blue
    (214, 39, 40),    # Crimson Red
    (44, 160, 44),    # Forest Green
    (255, 127, 14),   # Bright Orange
    (148, 103, 189),  # Purple
    (227, 119, 194),  # Soft Pink
    (23, 190, 207),   # Teal
    (188, 189, 34)    # Gold
]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def find_font_by_suffix(suffix_pattern, fallback_paths):
    search_dirs = [
        os.path.join(BASE_DIR, "fonts"),
        os.path.join(os.getcwd(), "fonts")
    ]

    for s_dir in search_dirs:
        if os.path.exists(s_dir):
            for file_name in sorted(os.listdir(s_dir)):
                if file_name.lower().endswith(".ttf") and file_name.lower().endswith(suffix_pattern.lower()):
                    full_path = os.path.join(s_dir, file_name)
                    print(f"[FONT AUTO-DETECT] Found '{suffix_pattern}' match: '{full_path}'", flush=True)
                    return full_path

    for fb in fallback_paths:
        if os.path.exists(fb):
            print(f"[FONT FALLBACK] Using system font: '{fb}'", flush=True)
            return fb

    print(f"[FONT ERROR] No matching font found for pattern '{suffix_pattern}'", flush=True)
    return None

# ------------------------------------------------------------------------------
# TERMINUS TTF BITMAP SIZE REFERENCE (Spectra 6 7.3" Display @ 127.8 DPI)
# Formula: pt = (px / dpi) * 72
#
# Terminus TTF contains pixel-perfect embedded bitmaps at specific px sizes:
#   12px, 14px, 16px, 18px, 20px, 22px, 24px, 28px, 32px
#
# Rendering via Pillow (Defaults to 72 DPI pixel grid):
#   Passing exact integer px values (12, 14, 16, 18, 20, 22, 24, 28, 32)
#   triggers the un-scaled bitmap strikes without vector anti-aliasing blur.
#
# Physical DPI Point Equivalents (if configuring target DPI = 127.8):
#   12px -> 6.76pt   |  18px -> 10.14pt  |  24px -> 13.52pt
#   14px -> 7.89pt   |  20px -> 11.27pt  |  28px -> 15.77pt
#   16px -> 9.01pt   |  22px -> 12.39pt  |  32px -> 18.03pt
# ------------------------------------------------------------------------------

FONT_REGULAR_PATH = find_font_by_suffix("regular.ttf", [
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
])

FONT_BOLD_PATH = find_font_by_suffix("bold.ttf", [
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
])

def load_ttf(path, size):
    if path and os.path.exists(path):
        try:
            return ImageFont.truetype(path, size)
        except Exception as e:
            print(f"[FONT ERROR] Failed to load TTF '{path}' at size {size}: {e}", flush=True)
    return ImageFont.load_default()

def draw_crisp_text(img, pos, text, font, fill_color, anchor="lm"):
    bbox = font.getbbox(text, anchor=anchor)
    if not bbox:
        return

    pad = 4
    w = (bbox[2] - bbox[0]) + (pad * 2)
    h = (bbox[3] - bbox[1]) + (pad * 2)

    txt_img = Image.new("L", (w, h), 0)
    txt_draw = ImageDraw.Draw(txt_img)

    txt_draw.text((pad - bbox[0], pad - bbox[1]), text, fill=255, font=font, anchor=anchor)

    threshold_mask = txt_img.point(lambda p: 255 if p > 128 else 0, mode='1')

    color_patch = Image.new("RGB", (w, h), fill_color)
    paste_x = int(pos[0] + bbox[0] - pad)
    paste_y = int(pos[1] + bbox[1] - pad)
    img.paste(color_patch, (paste_x, paste_y), threshold_mask)

def hex_to_rgb(hex_str):
    if not hex_str:
        return None
    hex_str = hex_str.lstrip('#')
    if len(hex_str) >= 6:
        return tuple(int(hex_str[i:i+2], 16) for i in (0, 2, 4))
    return None

def get_calendar_color(calendar):
    try:
        properties_to_fetch = [
            dav.DisplayName(),
            ical.CalendarColor()
        ]
        calendar.get_properties(props=properties_to_fetch)

        color_hex = calendar.props.get('{http://apple.com/ns/ical/}calendar-color') or calendar.props.get(ical.CalendarColor.tag)

        if color_hex:
            rgb = hex_to_rgb(color_hex)
            if rgb:
                return rgb
    except Exception as e:
        print(f"DEBUG: Internal property fetch failed for {calendar.name}: {e}", flush=True)

    name_bytes = (calendar.name or "Primary").encode('utf-8')
    name_hash = int(hashlib.md5(name_bytes).hexdigest(), 16)
    return FALLBACK_COLORS[name_hash % len(FALLBACK_COLORS)]

def get_best_blend(target_rgb):
    best_pair = (COLOR_WHITE, COLOR_WHITE)
    min_dist = float('inf')

    for c1 in SPECTRA_COLORS:
        for c2 in SPECTRA_COLORS:
            mix_r = (c1[0] + c2[0]) / 2.0
            mix_g = (c1[1] + c2[1]) / 2.0
            mix_b = (c1[2] + c2[2]) / 2.0

            dr = target_rgb[0] - mix_r
            dg = target_rgb[1] - mix_g
            db = target_rgb[2] - mix_b
            dist = 2 * (dr**2) + 4 * (dg**2) + 3 * (db**2)

            if dist < min_dist:
                min_dist = dist
                best_pair = (c1, c2)

    return best_pair

def draw_patterned_square(draw, img, xy, target_rgb, size=12):
    color_a, color_b = get_best_blend(target_rgb)

    block = Image.new("RGB", (size, size))
    pixels = block.load()
    for x in range(size):
        for y in range(size):
            if (x + y) % 2 == 0:
                pixels[x, y] = color_a
            else:
                pixels[x, y] = color_b

    img.paste(block, (int(xy[0]), int(xy[1])))
    draw.rectangle([xy, (xy[0] + size - 1, xy[1] + size - 1)], outline=COLOR_BLACK, width=1)

# Open-Meteo WMO Weather Interpretation Codes
WMO_CODE_MAP = {
    0: "Clear",
    1: "Mainly Clear", 2: "Partly Cloudy", 3: "Overcast",
    45: "Foggy", 48: "Rime Fog",
    51: "Light Drizzle", 53: "Drizzle", 55: "Dense Drizzle",
    56: "Freezing Drizzle", 57: "Freezing Drizzle",
    61: "Slight Rain", 63: "Rain", 65: "Heavy Rain",
    66: "Freezing Rain", 67: "Freezing Rain",
    71: "Slight Snow", 73: "Snow", 75: "Heavy Snow",
    77: "Snow Grains",
    80: "Rain Showers", 81: "Rain Showers", 82: "Violent Rain",
    85: "Snow Showers", 86: "Heavy Snow Showers",
    95: "Thunderstorm", 96: "Thunderstorm", 99: "Thunderstorm w/ Hail"
}

def fetch_weather_data(lat=None, lon=None, retries=3):
    if lat is None:
        lat = WEATHER_LATITUDE
    if lon is None:
        lon = WEATHER_LONGITUDE

    if WEATHER_UNIT == "C":
        temp_unit = "celsius"
        precip_unit = "mm"
    else:
        temp_unit = "fahrenheit"
        precip_unit = "inch"

    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        f"&hourly=precipitation_probability"
        f"&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,snowfall_sum"
        f"&temperature_unit={temp_unit}"
        f"&precipitation_unit={precip_unit}"
        f"&timezone={WEATHER_TIMEZONE}"
        f"&forecast_days=1"
    )

    # 1. Configure session with automatic HTTP-level retries & backoff
    session = requests.Session()
    retry_strategy = Retry(
        total=retries,
        backoff_factor=2,  # Waits 2s, then 4s, then 8s if needed
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    # 2. Outer try-loop to safeguard against JSON parsing or timeout errors
    for attempt in range(1, retries + 1):
        try:
            # Generous 10-second timeout per request attempt since execution speed at 12:01 AM doesn't matter
            resp = session.get(url, timeout=10)

            if resp.status_code == 200:
                data = resp.json()
                daily = data.get("daily", {})
                hourly = data.get("hourly", {})

                # Extract daily aggregates safely with defaults
                code = daily.get("weather_code", [0])[0] if daily.get("weather_code") else 0
                hi = round(daily.get("temperature_2m_max", [0])[0]) if daily.get("temperature_2m_max") else 0
                lo = round(daily.get("temperature_2m_min", [0])[0]) if daily.get("temperature_2m_min") else 0
                precip_val = daily.get("precipitation_sum", [0.0])[0] if daily.get("precipitation_sum") else 0.0
                snow_val = daily.get("snowfall_sum", [0.0])[0] if daily.get("snowfall_sum") else 0.0

                # Safeguard hourly array: force exactly 24 integer items regardless of raw payload length
                raw_hourly = hourly.get("precipitation_probability", [])
                hourly_prob = [int(p) for p in raw_hourly[:24]]
                if len(hourly_prob) < 24:
                    hourly_prob.extend([0] * (24 - len(hourly_prob)))

                # Determine baseline condition string from WMO table
                status = WMO_CODE_MAP.get(code, "Clear")

                # Append accumulation if Rain or Snow is active
                if WEATHER_UNIT == "C":
                    if "Snow" in status and snow_val > 0.1:
                        status += f" ({snow_val:.1f}cm)"
                    elif ("Rain" in status or "Drizzle" in status) and precip_val > 0.1:
                        status += f" ({precip_val:.1f}mm)"
                else:
                    if "Snow" in status and snow_val > 0.05:
                        status += f" ({snow_val:.1f}\")"
                    elif ("Rain" in status or "Drizzle" in status) and precip_val > 0.01:
                        status += f" ({precip_val:.2f}\")"

                return {
                    "hi": hi,
                    "lo": lo,
                    "status": status,
                    "hourly_prob": hourly_prob
                }
            else:
                print(f"[WEATHER WARN] Attempt {attempt}/{retries} returned HTTP {resp.status_code}", flush=True)

        except Exception as e:
            print(f"[WEATHER ERROR] Attempt {attempt}/{retries} failed: {e}", flush=True)

        # Pause briefly before triggering outer retry if HTTP adapter didn't catch it
        if attempt < retries:
            time.sleep(3)

    print(f"[WEATHER CRITICAL] All {retries} attempts exhausted. Weather unavailable.", flush=True)
    return None

def draw_precip_sparkline(draw, x, y, width, height, hourly_probs):
    """
    Renders a 24-hour precipitation probability sparkline with 4-hour tick marks.
    Width: 147px (24 bars @ 5px + 1px gap + 3px frame padding).
    """
    bar_width = 5  # Expanded to 5px for a wider graph
    gap = 1

    # Outer box border
    draw.rectangle([(x, y), (x + width - 1, y + height - 1)], outline=COLOR_BLACK, width=1)

    plot_x = x + 2
    plot_y_bottom = y + height - 2
    max_bar_height = height - 2  # Allows 100% bars to touch top inner border

    # 4-hour tick indices: 4a (4), 8a (8), 12p (12), 4p (16), 8p (20)
    tick_indices = {4, 8, 12, 16, 20}

    for i, prob in enumerate(hourly_probs):
        p = max(0, min(100, prob)) # Clamp 0-100%

        # Draw 2px tick mark extending downward from bottom border
        if i in tick_indices:
            tick_x = plot_x + (bar_width // 2)
            draw.line([(tick_x, y + height), (tick_x, y + height + 2)], fill=COLOR_BLACK, width=1)

        if p > 0:
            bar_h = max(1, int((p / 100.0) * max_bar_height))
            bar_top = plot_y_bottom - bar_h + 1

            # Fill bar with solid blue if >= 40% chance, black for light
            bar_color = COLOR_BLUE if p >= 40 else COLOR_BLACK
            draw.rectangle([(plot_x, bar_top), (plot_x + bar_width - 1, plot_y_bottom)], fill=bar_color)

        plot_x += (bar_width + gap)

def get_ical_prop(obj, name):
    """Extract property value defensively whether obj is a vobject or icalendar component."""
    vname = name.lower().replace('-', '_')
    # 1. vobject attribute access
    if hasattr(obj, vname):
        attr = getattr(obj, vname)
        return attr.value if hasattr(attr, 'value') else attr

    # 2. vobject contents dictionary
    if hasattr(obj, 'contents') and isinstance(obj.contents, dict):
        items = obj.contents.get(name.lower(), [])
        if items:
            item = items[0]
            return item.value if hasattr(item, 'value') else item

    # 3. icalendar dictionary access
    if hasattr(obj, 'get'):
        val = obj.get(name.upper()) or obj.get(name.lower()) or obj.get(name.title())
        if val is not None:
            if hasattr(val, 'dt'):
                return val.dt
            return val
    return None

def get_ical_prop_list(obj, name):
    """Extract property list defensively for multi-value fields like EXDATE."""
    results = []
    if hasattr(obj, 'contents') and isinstance(obj.contents, dict):
        for item in obj.contents.get(name.lower(), []):
            val = item.value if hasattr(item, 'value') else item
            if isinstance(val, list):
                results.extend(val)
            else:
                results.append(val)
    elif hasattr(obj, 'get'):
        vals = obj.get(name.upper()) or obj.get(name.lower())
        if vals is not None:
            if not isinstance(vals, list):
                vals = [vals]
            for v in vals:
                if hasattr(v, 'dts'):
                    for dt_item in v.dts:
                        results.append(dt_item.dt if hasattr(dt_item, 'dt') else dt_item)
                elif hasattr(v, 'dt'):
                    results.append(v.dt)
                else:
                    results.append(v)
    return results

def fetch_calendar_events():
    today = date.today()
    events_by_day = {today + timedelta(days=i): [] for i in range(7)}

    if not CALDAV_URL:
        return events_by_day

    try:
        client = caldav.DAVClient(url=CALDAV_URL, username=CALDAV_USER, password=CALDAV_PASSWORD)
        principal = client.principal()
        calendars = principal.calendars()

        start_dt = datetime.combine(today, datetime.min.time())
        end_dt = start_dt + timedelta(days=7)

        for calendar in calendars:
            try:
                cal_color = get_calendar_color(calendar) or COLOR_BLACK

                results = calendar.search(start=start_dt, end=end_dt, event=True, expand=True)

                # Group VEVENT components across ALL CalDAV result items by UID
                events_by_uid = {}
                for event in results:
                    try:
                        # Fetch components defensively from vobject or icalendar structures
                        vevents = []
                        if hasattr(event, 'vobject_instance') and hasattr(event.vobject_instance, 'contents'):
                            vevents = event.vobject_instance.contents.get('vevent', [])
                        elif hasattr(event, 'icalendar_instance'):
                            vevents = event.icalendar_instance.subcomponents
                        elif hasattr(event, 'instance'):
                            vevents = [event.instance]

                        for vevent in vevents:
                            uid_val = get_ical_prop(vevent, 'UID')
                            uid = str(uid_val) if uid_val else str(id(vevent))
                            if uid not in events_by_uid:
                                events_by_uid[uid] = []
                            events_by_uid[uid].append(vevent)
                    except Exception:
                        continue

                # Process events UID by UID
                for uid, vevents in events_by_uid.items():
                    cancelled_or_exdates = set()
                    moved_from_dates = set()

                    # Pass 1: Collect EXDATEs, cancelled dates, and RECURRENCE-ID targets
                    for ical_obj in vevents:
                        # Collect EXDATEs
                        ex_list = get_ical_prop_list(ical_obj, 'EXDATE')
                        for ex in ex_list:
                            ex_d = ex.date() if isinstance(ex, datetime) else ex
                            cancelled_or_exdates.add(ex_d)

                        status_val = get_ical_prop(ical_obj, 'STATUS')
                        is_cancelled = status_val and str(status_val).upper() == 'CANCELLED'

                        rec_id_val = get_ical_prop(ical_obj, 'RECURRENCE-ID')
                        dtstart_val = get_ical_prop(ical_obj, 'DTSTART')

                        if rec_id_val:
                            rec_d = rec_id_val.date() if isinstance(rec_id_val, datetime) else rec_id_val
                            if is_cancelled:
                                cancelled_or_exdates.add(rec_d)
                            elif dtstart_val:
                                start_d = dtstart_val.date() if isinstance(dtstart_val, datetime) else dtstart_val
                                # If RECURRENCE-ID differs from DTSTART, it was moved away from rec_d to start_d
                                if rec_d != start_d:
                                    moved_from_dates.add(rec_d)

                    # Pass 2: Process active event occurrences
                    for ical_obj in vevents:
                        status_val = get_ical_prop(ical_obj, 'STATUS')
                        if status_val and str(status_val).upper() == 'CANCELLED':
                            continue

                        dtstart = get_ical_prop(ical_obj, 'DTSTART')
                        if not dtstart:
                            continue

                        is_datetime = isinstance(dtstart, datetime)
                        start_date = dtstart.date() if is_datetime else dtstart

                        rec_id_val = get_ical_prop(ical_obj, 'RECURRENCE-ID')
                        rec_d = rec_id_val.date() if (rec_id_val and isinstance(rec_id_val, datetime)) else rec_id_val

                        # Discard un-moved base occurrences that land on a date marked as moved or cancelled
                        if start_date in moved_from_dates and (rec_d is None or rec_d == start_date):
                            continue

                        if (start_date in cancelled_or_exdates or rec_d in cancelled_or_exdates) and (rec_d is None or rec_d == start_date):
                            continue

                        summary_val = get_ical_prop(ical_obj, 'SUMMARY')
                        summary_str = str(summary_val) if summary_val else "Untitled Event"

                        dtend = get_ical_prop(ical_obj, 'DTEND')

                        if dtend:
                            is_dtend_datetime = isinstance(dtend, datetime)
                            end_date = dtend.date() if is_dtend_datetime else dtend
                            if not is_dtend_datetime and end_date > start_date:
                                # All day events dtend is exclusive, so subtract 1 day to get the inclusive end date
                                end_date -= timedelta(days=1)
                        else:
                            end_date = start_date

                        curr_date = start_date
                        while curr_date <= end_date:
                            if curr_date in events_by_day:
                                if is_datetime:
                                    if curr_date == start_date and curr_date == end_date:
                                        # Single day datetime
                                        hour_str = dtstart.strftime("%I").lstrip("0") or "12"
                                        ampm_str = "a" if dtstart.strftime("%p") == "AM" else "p"
                                        time_str = f"{hour_str}:{dtstart.strftime('%M')}{ampm_str}"
                                    elif curr_date == start_date:
                                        # Multi-day start
                                        hour_str = dtstart.strftime("%I").lstrip("0") or "12"
                                        ampm_str = "a" if dtstart.strftime("%p") == "AM" else "p"
                                        time_str = f"{hour_str}:{dtstart.strftime('%M')}{ampm_str}"
                                    elif curr_date == end_date:
                                        # Multi-day end
                                        hour_str = dtend.strftime("%I").lstrip("0") or "12"
                                        ampm_str = "a" if dtend.strftime("%p") == "AM" else "p"
                                        time_str = f" ->{hour_str}:{dtend.strftime('%M')}{ampm_str}"
                                    else:
                                        # Multi-day middle
                                        time_str = "All Day"
                                else:
                                    time_str = "All Day"

                                events_by_day[curr_date].append({
                                    'time': time_str,
                                    'summary': summary_str,
                                    'color': (cal_color[0], cal_color[1], cal_color[2])
                                })
                            curr_date += timedelta(days=1)
            except Exception:
                continue
    except Exception as e:
        print(f"Calendar sync error: {e}", flush=True)

    for day in events_by_day:
        events_by_day[day].sort(key=lambda x: (0 if x['time'] == "All Day" else 1, x['time']))

    return events_by_day

def render_calendar(events_by_day, battery=None):
    width, height = 480, 800
    img = Image.new("RGB", (width, height), COLOR_WHITE)
    draw = ImageDraw.Draw(img)

    # Fonts (Updated sizes: event_time -> 16px, event_summary -> 20px)
    font_title = load_ttf(FONT_BOLD_PATH, 24)
    font_day_label = load_ttf(FONT_BOLD_PATH, 20)
    font_event_time = load_ttf(FONT_BOLD_PATH, 16)
    font_event_summary = load_ttf(FONT_REGULAR_PATH, 20)
    font_small = load_ttf(FONT_REGULAR_PATH, 12)
    font_no_events = load_ttf(FONT_REGULAR_PATH, 14)

    # Core Dimensions
    ROW_HEIGHT = 24
    DAY_PADDING = 16
    WEATHER_HEIGHT = 20

    TODAY_BASE_SLOTS = 4   # Today base expanded to 4 event slots
    HORIZON_BASE_SLOTS = 3 # Horizon base remains 3 event slots

    MIN_HORIZON_DAY_HEIGHT = DAY_PADDING + ROW_HEIGHT                       # 40px (1 slot min)
    BASE_TODAY_BODY_HEIGHT = DAY_PADDING + (TODAY_BASE_SLOTS * ROW_HEIGHT) + WEATHER_HEIGHT  # 132px
    BASE_HORIZON_DAY_HEIGHT = DAY_PADDING + (HORIZON_BASE_SLOTS * ROW_HEIGHT)                # 88px

    today = date.today()

    # 1. Status Bar
    draw.rectangle([(0, 0), (width, 25)], fill=COLOR_BLACK)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    draw_crisp_text(img, (10, 12), f"Last Sync: {now_str}", font_small, COLOR_WHITE, anchor="lm")

    # Right Side: Battery Percentage (if passed via query param)
    if battery is not None:
        battery = max(0, min(100, battery))
        bat_str = f"Bat: {battery}%"
        draw_crisp_text(img, (width - 10, 12), bat_str, font_small, COLOR_WHITE, anchor="rm")

    # 2. Today's Header (Faux-Bold via 1-pixel horizontal shift)
    draw.rectangle([(0, 25), (width, 75)], fill=COLOR_RED)
    today_str = today.strftime("%A, %B %d")
    # Pass 1: Base position
    draw_crisp_text(img, (15, 50), today_str, font_title, COLOR_WHITE, anchor="lm")
    # Pass 2: Shifted 1px right to thicken vertical stems
    draw_crisp_text(img, (16, 50), today_str, font_title, COLOR_WHITE, anchor="lm")

    # 3. Horizon Space Scavenger Engine
    today_events = events_by_day.get(today, [])
    horizon_days = [today + timedelta(days=i) for i in range(1, 7)]

    # Compute total height needed if everyone gets their requested event slots
    today_requested_slots = max(TODAY_BASE_SLOTS, len(today_events))
    today_target_body_height = DAY_PADDING + (today_requested_slots * ROW_HEIGHT) + WEATHER_HEIGHT

    horizon_requested_heights = []
    for d in horizon_days:
        ev_count = len(events_by_day.get(d, []))
        slots = max(HORIZON_BASE_SLOTS, ev_count)
        horizon_requested_heights.append(DAY_PADDING + (slots * ROW_HEIGHT))

    total_requested_height = 75 + today_target_body_height + sum(horizon_requested_heights)

    # Calculate available slack vs. deficit relative to total canvas height (800px)
    deficit = total_requested_height - height

    # Base allocation array: [Today, Horizon Day 1, Day 2, Day 3, Day 4, Day 5, Day 6]
    allocations = [today_target_body_height] + list(horizon_requested_heights)

    if deficit <= 0:
        # SURPLUS / EQUAL: Distribute surplus space evenly across ALL 7 DAYS
        surplus = abs(deficit)
        bonus_per_day = surplus // 7
        for idx in range(7):
            allocations[idx] += bonus_per_day
    else:
        # DEFICIT: Activate Space Scavenger Engine

        # Tier 1: Scavenge padding bottom-up from Horizon Days (index 6 down to 1)
        for idx in range(6, 0, -1):
            if deficit <= 0:
                break
            extra_padding = allocations[idx] - BASE_HORIZON_DAY_HEIGHT
            if extra_padding > 0:
                reclaim = min(deficit, extra_padding)
                allocations[idx] -= reclaim
                deficit -= reclaim

        # Tier 2: Symmetrical "No Events" Shrinker (down to 1 slot / 40px)
        if deficit > 0:
            zero_indices = [idx + 1 for idx, d in enumerate(horizon_days) if len(events_by_day.get(d, [])) == 0]
            if zero_indices:
                total_reclaimable = sum(allocations[idx] - MIN_HORIZON_DAY_HEIGHT for idx in zero_indices)
                reclaim = min(deficit, total_reclaimable)
                if reclaim > 0:
                    shrink_per_zero_day = reclaim // len(zero_indices)
                    for idx in zero_indices:
                        allocations[idx] -= shrink_per_zero_day
                    deficit -= reclaim

        # Tier 3: Bottom-Up Empty Slot Harvester (NEW!)
        # Reclaim empty event slots from days with 1 or 2 events (down to max(1, actual_event_count))
        if deficit > 0:
            for idx in range(6, 0, -1):
                if deficit <= 0:
                    break

                day_date = horizon_days[idx - 1]
                ev_count = len(events_by_day.get(day_date, []))

                # Minimum height needed to render actual events without truncation (at least 1 slot)
                needed_slots = max(1, ev_count)
                min_needed_height = DAY_PADDING + (needed_slots * ROW_HEIGHT)

                # Check if current allocation has unused event slot space
                slack = allocations[idx] - min_needed_height
                if slack > 0:
                    reclaim = min(deficit, slack)
                    allocations[idx] -= reclaim
                    deficit -= reclaim

        # Tier 4: Truncate horizon days from the bottom up if still in deficit
        while deficit > 0 and len(horizon_days) > 0:
            removed_height = allocations.pop()  # Remove bottom-most day
            horizon_days.pop()
            deficit -= removed_height

    # Unpack finalized layout heights
    today_final_body_height = allocations[0]
    horizon_final_heights = allocations[1:]

    # Fetch Weather Data
    weather = fetch_weather_data()

    # 4. Render Today Body & Weather
    today_bottom_y = 75 + today_final_body_height
    y_cursor = 95

    if not today_events:
        draw_crisp_text(img, (20, y_cursor), "No events today.", font_no_events, COLOR_BLACK, anchor="lm")
    else:
        today_max_render = max(1, int((today_final_body_height - DAY_PADDING - WEATHER_HEIGHT) / ROW_HEIGHT))
        for ev in today_events[:today_max_render]:
            target_rgb = ev.get('color', COLOR_BLACK)
            draw_patterned_square(draw, img, (20, y_cursor - 6), target_rgb, size=12)

            time_color = COLOR_BLUE if ev['time'] != "All Day" else COLOR_RED
            draw_crisp_text(img, (42, y_cursor), ev['time'], font_event_time, time_color, anchor="lm")

            summary = ev['summary']
            if len(summary) > 26:
                summary = summary[:23] + "..."
            draw_crisp_text(img, (125, y_cursor), summary, font_event_summary, COLOR_BLACK, anchor="lm")
            y_cursor += ROW_HEIGHT

    # Render Weather Line anchored relative to bottom of Today box
    weather_y = today_bottom_y - WEATHER_HEIGHT + 2

    if weather:
        # Left side: High / Low and Condition text
        weather_str = f"{weather['hi']}°/{weather['lo']}°  {weather['status']}"

        draw_crisp_text(img, (20, weather_y), weather_str, font_event_time, COLOR_BLACK, anchor="lm")

        # Right side: Expanded 24-hour Sparkline Graph (147px wide x 16px high)
        sparkline_w = 147
        sparkline_h = 16
        sparkline_x = width - sparkline_w - 15  # 15px right padding
        sparkline_y = weather_y - 8            # Center vertically on line

        draw_precip_sparkline(draw, sparkline_x, sparkline_y, sparkline_w, sparkline_h, weather['hourly_prob'])
    else:
        draw_crisp_text(img, (20, weather_y), f"—°{WEATHER_UNIT} Weather Unavailable", font_small, COLOR_BLACK, anchor="lm")

    # Bottom border for Today section
    draw.line([(0, today_bottom_y), (width, today_bottom_y)], fill=COLOR_BLACK, width=2)

    # 5. Vertical Grid Line (x = 90)
    grid_x = 90
    draw.line([(grid_x, today_bottom_y), (grid_x, height)], fill=COLOR_BLACK, width=1)

    # 6. Render Horizon Days
    current_y = today_bottom_y
    for d, day_h in zip(horizon_days, horizon_final_heights):
        day_events = events_by_day.get(d, [])

        draw.line([(0, current_y), (width, current_y)], fill=COLOR_BLACK, width=1)

        row_mid_y = current_y + 20
        day_label = d.strftime("%a %d")

        # Date column
        draw_crisp_text(img, (10, row_mid_y), day_label, font_day_label, COLOR_BLACK, anchor="lm")

        # Events column
        if not day_events:
            draw_crisp_text(img, (105, row_mid_y), "No events", font_no_events, COLOR_BLACK, anchor="lm")
        else:
            max_rows = max(1, int((day_h - DAY_PADDING) / ROW_HEIGHT))
            ev_y = row_mid_y
            for ev in day_events[:max_rows]:
                target_rgb = ev.get('color', COLOR_BLACK)

                draw_patterned_square(draw, img, (100, ev_y - 6), target_rgb, size=12)

                time_text = ev['time']
                time_color = COLOR_BLUE if time_text != "All Day" else COLOR_RED

                draw_crisp_text(img, (175, ev_y), time_text, font_event_time, time_color, anchor="rm")

                summary = ev['summary']
                if len(summary) > 22:
                    summary = summary[:19] + "..."
                draw_crisp_text(img, (185, ev_y), summary, font_event_summary, COLOR_BLACK, anchor="lm")
                ev_y += ROW_HEIGHT

        current_y += day_h

    return img

def get_seconds_until_next_target(target_hour=CALENDAR_REFRESH_HOUR, target_minute=CALENDAR_REFRESH_MINUTE):
    """Calculates seconds remaining until the next occurrence of target_hour:target_minute local time."""
    now = datetime.now()
    target = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)

    # If 4:15 AM today has already passed, set target to 4:15 AM tomorrow
    if now >= target:
        target += timedelta(days=1)

    return int((target - now).total_seconds())

@app.route('/calendar.bmp')
def calendar_bmp():
    battery_pct = request.args.get('battery', default=None, type=int)
    events = fetch_calendar_events()
    img = render_calendar(events, battery=battery_pct)

    img_io = BytesIO()
    img.save(img_io, 'BMP')
    img_io.seek(0)

    # Calculate sleep interval target (4:15 AM)
    seconds_to_sleep = get_seconds_until_next_target(CALENDAR_REFRESH_HOUR, CALENDAR_REFRESH_MINUTE)

    response = make_response(send_file(img_io, mimetype='image/bmp'))
    response.headers['X-Sleep-Seconds'] = str(seconds_to_sleep)

    return response

@app.route('/')
def health():
    return jsonify({"status": "running"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)
