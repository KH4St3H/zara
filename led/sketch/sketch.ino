// zara status on the UNO Q LED matrix.
// Asks Linux (zara_led.py via arduino-router) for a status every 2 s and draws it:
//   0 = off   1 = dim check (all good)
//   2 = cross (a zara service is down)   3 = blinking "!" (critical alert, unacknowledged)
//   10+n = n unread Mattermost DMs/mentions: envelope and count, alternating
//   10000+HHMM = all good: scrolling banner with time, date and outdoor temperature
//   20000+HHMM = same banner, dimmed (quiet hours)
// Optional ack button between D2 and GND -> calls "zara_ack" on Linux.

#include <Arduino_LED_Matrix.h>
#include <Arduino_RouterBridge.h>

Arduino_LED_Matrix matrix;

const int W = 13, H = 8;
const int BUTTON = 2;

// 8 rows x 13 columns, '#' = lit
const char *ICON_CHECK[H] = {
  ".............",
  "...........#.",
  "..........#..",
  ".#.......#...",
  "..#.....#....",
  "...#...#.....",
  "....#.#......",
  ".....#.......",
};
const char *ICON_CROSS[H] = {
  "..#.......#..",
  "...#.....#...",
  "....#...#....",
  ".....#.#.....",
  ".....#.#.....",
  "....#...#....",
  "...#.....#...",
  "..#.......#..",
};
const char *ICON_BANG[H] = {
  ".....###.....",
  ".....###.....",
  ".....###.....",
  ".....###.....",
  "......#......",
  ".............",
  ".....###.....",
  ".....###.....",
};
const char *ICON_MAIL[H] = {
  ".............",
  ".###########.",
  ".##.......##.",
  ".#.#.....#.#.",
  ".#..#...#..#.",
  ".#...#.#...#.",
  ".#....#....#.",
  ".###########.",
};
// 3x5 digits, rows top to bottom, 3 bits each (MSB = left column)
const uint8_t DIGITS[10][5] = {
  {7, 5, 5, 5, 7}, {2, 6, 2, 2, 7}, {7, 1, 7, 4, 7}, {7, 1, 7, 1, 7}, {5, 5, 7, 1, 1},
  {7, 4, 7, 1, 7}, {7, 4, 7, 5, 7}, {7, 1, 1, 2, 2}, {7, 5, 7, 5, 7}, {7, 5, 7, 1, 7},
};
const char *ICON_DOT[H] = {
  ".............", ".............", ".............", ".............",
  ".............", ".............", ".............", "............#",
};

uint8_t frame[W * H];

void show(const char **icon, uint8_t level) {   // level 0..7
  for (int r = 0; r < H; r++)
    for (int c = 0; c < W; c++)
      frame[r * W + c] = (icon && icon[r][c] == '#') ? level : 0;
  matrix.draw(frame);
}

void drawDigit(int d, int col, uint8_t level) {
  for (int r = 0; r < 5; r++)
    for (int c = 0; c < 3; c++)
      if (DIGITS[d][r] & (4 >> c)) frame[(r + 2) * W + col + c] = level;
}

void showNumber(int n, uint8_t level) {   // 1..99, centred
  for (int i = 0; i < W * H; i++) frame[i] = 0;
  if (n < 10) drawDigit(n, 5, level);
  else { drawDigit(n / 10, 3, level); drawDigit(n % 10, 7, level); }
  matrix.draw(frame);
}

// Scrolling banner "HH:MM  THU 12.11  7°C", looping right-to-left.
// Variable-width 5-row font; each row is `w` bits, MSB = left column.
struct Glyph { char ch; uint8_t w; uint8_t rows[5]; };
const Glyph FONT[] = {
  {'A', 3, {2, 5, 7, 5, 5}}, {'C', 3, {3, 4, 4, 4, 3}}, {'D', 3, {6, 5, 5, 5, 6}}, {'E', 3, {7, 4, 6, 4, 7}},
  {'F', 3, {7, 4, 6, 4, 4}}, {'H', 3, {5, 5, 7, 5, 5}}, {'I', 1, {1, 1, 1, 1, 1}}, {'M', 5, {17, 27, 21, 17, 17}},
  {'N', 4, {9, 13, 11, 9, 9}}, {'O', 3, {7, 5, 5, 5, 7}}, {'R', 3, {6, 5, 6, 5, 5}}, {'S', 3, {3, 4, 2, 1, 6}},
  {'T', 3, {7, 2, 2, 2, 2}}, {'U', 3, {5, 5, 5, 5, 7}}, {'W', 5, {17, 17, 21, 27, 17}},
  {':', 1, {0, 1, 0, 1, 0}}, {'.', 1, {0, 0, 0, 0, 1}}, {'-', 2, {0, 0, 3, 0, 0}}, {'*', 2, {3, 3, 0, 0, 0}},  // * = degree
  {' ', 1, {0, 0, 0, 0, 0}}, {'?', 3, {7, 1, 2, 0, 2}},
};
const char *WDAY[7] = {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"};

uint8_t banner[200];          // one byte per column, bit r = row r
int bannerLen = 0, bannerHHMM = -1, scrollPos = 0;
uint8_t clockLevel = 4;

void addColumns(uint8_t w, const uint8_t *rows) {
  for (int c = w - 1; c >= 0; c--) {
    if (bannerLen >= (int)sizeof(banner)) return;
    uint8_t col = 0;
    for (int r = 0; r < 5; r++) if ((rows[r] >> c) & 1) col |= 1 << r;
    banner[bannerLen++] = col;
  }
  if (bannerLen < (int)sizeof(banner)) banner[bannerLen++] = 0;   // 1-pixel gap
}

void addText(const char *t) {
  for (; *t; t++) {
    if (*t >= '0' && *t <= '9') { addColumns(3, DIGITS[*t - '0']); continue; }
    const Glyph *g = &FONT[sizeof(FONT) / sizeof(FONT[0]) - 1];   // '?'
    for (const Glyph &f : FONT) if (f.ch == *t) { g = &f; break; }
    addColumns(g->w, g->rows);
  }
}

void buildBanner(int hhmm) {
  int dt = -1, temp = -999;
  Bridge.call("zara_date").result(dt);
  Bridge.call("zara_temp").result(temp);
  char text[48];
  int n = snprintf(text, sizeof(text), "%02d:%02d", hhmm / 100, hhmm % 100);
  if (dt >= 0) n += snprintf(text + n, sizeof(text) - n, "  %s %d.%d", WDAY[(dt / 10000) % 7], (dt / 100) % 100, dt % 100);
  if (temp > -100) snprintf(text + n, sizeof(text) - n, "  %d*C", temp);
  bannerLen = 0;
  addText(text);
  for (int i = 0; i < 6; i++) if (bannerLen < (int)sizeof(banner)) banner[bannerLen++] = 0;   // space before repeat
  if (scrollPos >= bannerLen) scrollPos = 0;
}

void showBanner() {
  for (int i = 0; i < W * H; i++) frame[i] = 0;
  for (int x = 0; x < W; x++) {
    uint8_t col = banner[(scrollPos + x) % bannerLen];
    for (int r = 0; r < 5; r++) if (col & (1 << r)) frame[(r + 2) * W + x] = clockLevel;
  }
  matrix.draw(frame);
}

int status = -1;              // -1 = no answer from Linux yet; 10..109 = unread Mattermost (status - 10);
                              // >= 10000 = idle clock, HHMM = status - 10000
unsigned long lastColon = 0;
bool colonOn = true;
bool mailPhase = true;        // unread mode alternates envelope / count
unsigned long lastMailSwap = 0;
unsigned long lastPoll = 0, lastBlink = 0;
bool blinkOn = true;
int lastButton = HIGH;
unsigned long buttonChanged = 0;

void setup() {
  matrix.begin();
  matrix.setGrayscaleBits(3);   // brightness levels 0..7
  matrix.clear();
  pinMode(BUTTON, INPUT_PULLUP);
  Bridge.begin();
}

void render() {
  switch (status) {
    case 0: show(nullptr, 0); break;
    case 1: show(ICON_CHECK, 1); break;
    case 2: show(ICON_CROSS, 3); break;
    case 3: show(blinkOn ? ICON_BANG : nullptr, 7); break;
    default:
      if (status >= 10000) {             // idle: scrolling clock (>= 20000: quiet hours, dim)
        int hhmm = status % 10000;
        clockLevel = status >= 20000 ? 1 : 4;
        if (hhmm != bannerHHMM) { buildBanner(hhmm); bannerHHMM = hhmm; }
        showBanner();
      } else if (status >= 10) {         // unread Mattermost messages
        int n = status - 10;
        if (n > 99) n = 99;
        if (mailPhase) show(ICON_MAIL, 3); else showNumber(n, 4);
      } else {
        show(ICON_DOT, 1);               // Linux side not answering
      }
  }
}

void loop() {
  unsigned long now = millis();

  if (now - lastPoll >= 2000) {
    lastPoll = now;
    int s = -1;
    if (Bridge.call("zara_status").result(s)) status = s; else status = -1;
    render();
  }

  if (status >= 10000 && bannerLen > 0 && now - lastColon >= 130) {   // scroll one column
    lastColon = now;
    scrollPos = (scrollPos + 1) % bannerLen;
    render();
  }

  if (status >= 10 && status < 10000 && now - lastMailSwap >= (mailPhase ? 2500UL : 1500UL)) {
    lastMailSwap = now;
    mailPhase = !mailPhase;
    render();
  }

  if (status == 3 && now - lastBlink >= 400) {
    lastBlink = now;
    blinkOn = !blinkOn;
    render();
  }

  int b = digitalRead(BUTTON);
  if (b != lastButton && now - buttonChanged > 50) {
    buttonChanged = now;
    lastButton = b;
    if (b == LOW) {               // pressed
      show(ICON_CHECK, 7);
      bool ok = false;
      Bridge.call("zara_ack").result(ok);
      delay(800);
      status = 1;
      render();
    }
  }
  delay(20);
}
