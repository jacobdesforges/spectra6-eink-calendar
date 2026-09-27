#include <TFT_eSPI.h>
#include "driver.h"
#include "secrets.h"
#include <WiFi.h>
#include <HTTPClient.h>

// EE04 / Seeed XIAO Battery Sense Hardware Mapping
#ifndef BAT_PIN
#define BAT_PIN A0          // GPIO1: Analog input connected to center tap of voltage divider
#endif

#ifndef BAT_ENABLE_PIN
#define BAT_ENABLE_PIN D5   // GPIO6: Active-LOW gate controlling onboard divider MOSFET
#endif

EPaper epaper;

// Function Prototypes
uint32_t fetchAndRenderBMP();
uint16_t read16(WiFiClient &stream);
uint32_t read32(WiFiClient &stream);
uint8_t getClosestColorIndex(uint8_t r, uint8_t g, uint8_t b);
int getBatteryPercentage();

int getBatteryPercentage() {
    // 1. Set ADC attenuation to 11dB to measure voltages up to ~3.1V - 3.3V
    analogSetPinAttenuation(BAT_PIN, ADC_11db);

    // 2. Drive D5 HIGH to activate the N-channel MOSFET gate and energize the divider
    pinMode(BAT_ENABLE_PIN, OUTPUT);
    digitalWrite(BAT_ENABLE_PIN, HIGH); 
    delay(50); // Increased stabilization time for high-impedance divider

    // 3. Clear initial SAR ADC bias with a dummy read
    analogReadMilliVolts(BAT_PIN);
    delay(5);

    // 4. Take multiple ADC samples to average out high-impedance sampling noise
    uint32_t total_mv = 0;
    const int samples = 10;
    for (int i = 0; i < samples; i++) {
        total_mv += analogReadMilliVolts(BAT_PIN);
        delay(2);
    }
    uint32_t pin_mv = total_mv / samples;

    // 5. Drive D5 LOW immediately after reading to shut off current flow to ground
    digitalWrite(BAT_ENABLE_PIN, LOW);

    // 6. Calculate total battery voltage (1:1 divider doubles pin voltage)
    uint32_t vbat_mv = pin_mv * 2; 

    // Debug output over Serial (ONLY if an active USB CDC session is attached)
    if (Serial) {
        Serial.printf("[BATTERY] ADC Pin: %u mV | Battery Voltage: %u mV\n", pin_mv, vbat_mv);
    }

    // 7. Map cell voltage to percentage (3.3V = 0%, 4.2V = 100%)
    if (vbat_mv >= 4200) return 100;
    
    // Return a minimum floor of 1% during boot instead of 0% to prevent instant sleep loops on transient low reads
    if (vbat_mv <= 3300) return 1; 

    int pct = (vbat_mv - 3300) * 100 / (4200 - 3300);
    return constrain(pct, 1, 100);
}

void setup() {
    Serial.begin(115200);
    delay(2000);

    if (Serial) {
        Serial.println("\n====================================");
        Serial.println("     EE04 CALENDAR CLEAN MODE       ");
        Serial.println("====================================");
    }

    // Initialize E-Paper using your driver.h configuration
    epaper.begin();

    if (Serial) {
        Serial.printf("Targeting SSID: %s\n", WIFI_SSID);
    }
    
    // Call WiFi.begin ONCE to avoid brownout current spikes on battery
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

void loop() {
    uint32_t sleepSeconds = 3600; // Default fallback to 1 hour if HTTP fails

    // WiFi Connection (Wait for the connection initiated in setup)
    int timeout = 0;
    while (WiFi.status() != WL_CONNECTED && timeout < 20) {
        delay(500);
        timeout++;
    }

    if (WiFi.status() == WL_CONNECTED) {
        // Clear screen to white (0) before drawing new content
        // This ensures old pixels are reset
        epaper.fillScreen(0); 
        
        sleepSeconds = fetchAndRenderBMP();

        // One update, then sleep
        epaper.update();
    } else {
        if (Serial) Serial.println("[WIFI] Failed to connect. Proceeding to sleep.");
    }

    if (Serial) {
        Serial.printf("[SLEEP] Setting wakeup timer for %u seconds (%u hrs, %u mins)\n", 
                      sleepSeconds, sleepSeconds / 3600, (sleepSeconds % 3600) / 60);
        // Serial.flush() is INTENTIONALLY REMOVED HERE. 
        // Calling flush() on Native USB CDC without a host causes an infinite hang.
    }

    // Configure ESP32 deep sleep wakeup timer (convert seconds to microseconds)
    esp_sleep_enable_timer_wakeup((uint64_t)sleepSeconds * 1000000ULL);

    // Go to sleep, prevent the display from refreshing again
    esp_deep_sleep_start();
}

uint16_t read16(WiFiClient &stream) {
    uint8_t buf[2];
    stream.readBytes(buf, 2);
    return (uint16_t)buf[0] | ((uint16_t)buf[1] << 8);
}

uint32_t read32(WiFiClient &stream) {
    uint8_t buf[4];
    stream.readBytes(buf, 4);
    return (uint32_t)buf[0] | ((uint32_t)buf[1] << 8) | ((uint32_t)buf[2] << 16) | ((uint32_t)buf[3] << 24);
}

uint16_t getClosestColor(uint8_t r, uint8_t g, uint8_t b) {
    if (r < 50 && g < 50 && b < 50) return TFT_BLACK;
    if (r > 200 && g > 200 && b < 50) return TFT_YELLOW;
    if (r > 200 && g < 50 && b < 50) return TFT_RED;
    if (r < 50 && g > 150 && b < 50) return TFT_GREEN;
    if (r < 50 && g < 50 && b > 200) return TFT_BLUE;
    return TFT_WHITE; // Default White
}

uint32_t fetchAndRenderBMP() {
    HTTPClient http;
    WiFiClient stream;
    uint32_t sleepSeconds = 3600; // Fallback default (1 hour)

    int batteryPct = getBatteryPercentage();
    String targetUrl = "http://" + String(SERVER_IP) + ":" + String(SERVER_PORT) + String(IMAGE_PATH) + "?battery=" + String(batteryPct);
    
    http.begin(targetUrl);

    // Register custom header to track before sending GET request
    const char* headerKeys[] = {"X-Sleep-Seconds"};
    http.collectHeaders(headerKeys, 1);

    int httpResponseCode = http.GET();

    if (httpResponseCode == HTTP_CODE_OK) {
        // Extract sleep duration header if supplied by server
        if (http.hasHeader("X-Sleep-Seconds")) {
            sleepSeconds = http.header("X-Sleep-Seconds").toInt();
            if (Serial) {
                Serial.printf("[HTTP] Ingested X-Sleep-Seconds header: %u\n", sleepSeconds);
            }
        } else {
            if (Serial) Serial.println("[HTTP] Header X-Sleep-Seconds missing. Using 3600s default.");
        }

        stream = http.getStream();

        if (read16(stream) != 0x4D42) return sleepSeconds;
        read32(stream); read16(stream); read16(stream);
        uint32_t dataOffset = read32(stream);
        read32(stream);
        int32_t w = read32(stream);
        int32_t h = read32(stream);
        read16(stream); read16(stream); read32(stream);

        while (stream.available() && dataOffset > 34) {
            stream.read();
            dataOffset--;
        }

        uint32_t lineSize = (w * 3 + 3) & ~3;
        uint8_t lineStripeBuffer[lineSize];

        for (int32_t y = h - 1; y >= 0; y--) {
            stream.readBytes(lineStripeBuffer, lineSize);
            uint32_t dataIndex = 0;

            for (int32_t x = 0; x < w; x++) {
                uint8_t b = lineStripeBuffer[dataIndex++];
                uint8_t g = lineStripeBuffer[dataIndex++];
                uint8_t r = lineStripeBuffer[dataIndex++];

                uint16_t color = getClosestColor(r, g, b);
                
                // Rotation logic
                int32_t rotX = (h - 1) - y;
                int32_t rotY = x;

                if (rotX < epaper.width() && rotY < epaper.height()) {
                    epaper.drawPixel(rotX, rotY, color);
                }
            }
        }
    } else {
        if (Serial) Serial.printf("[HTTP] Request failed with code: %d\n", httpResponseCode);
    }
    
    http.end();
    return sleepSeconds;
