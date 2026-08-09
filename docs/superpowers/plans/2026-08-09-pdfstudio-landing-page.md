# PDF Studio Landing Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a premium dark developer-tool landing page with an animated warm aurora background, deployed as a static site to Vercel.

**Architecture:** Single `index.html` with embedded CSS and minimal JS in a `website/` directory. Plain HTML/CSS/JS — no build step. Static files deployed to Vercel. All aurora animation is pure CSS keyframes. Logos are inline SVGs.

**Tech Stack:** HTML5, CSS3 (custom properties, keyframes, backdrop-filter), vanilla JS (UA detection, caret blink), SVG for logos/icons, Vercel static hosting.

## Global Constraints

- Canvas base: `#07080a`
- Aurora palette: `#ff2f3a` (crimson) → `#ff6b4a` (coral) → `#ffb347` (amber) — strictly warm, no purple/indigo
- Typography: Inter (all UI), GeistMono (mono caption + shortcut chips only)
- Button fill: `#e6e6e6`, button text: `#2f3031`
- Download links point to: `https://github.com/pdfstudio/pdfstudio/releases/latest`
- All CSS animations use 0% keyframe at full bloom
- Scoped class selectors for font sizes (not bare tag selectors) to survive preview hosts

---

## File Structure

```
website/
├── index.html              # Single-page landing
├── css/
│   └── style.css           # All styles
├── js/
│   └── main.js             # UA detection, caret blink
├── assets/
│   ├── logo.svg            # Warm diamond + wordmark
│   ├── logo-icon.svg       # Diamond mark only
│   ├── favicon.svg         # Browser tab icon
│   ├── apple.svg           # Apple OS glyph
│   └── windows.svg         # Windows OS glyph
└── vercel.json             # Vercel config
```

---

### Task 1: Scaffold directory structure and base HTML

**Files:**
- Create: `website/index.html`
- Create: `website/vercel.json`

**Interfaces:** None — this is the foundation.

- [ ] **Step 1: Create directory structure**

```bash
mkdir -p website/css website/js website/assets
```

- [ ] **Step 2: Create `website/vercel.json`**

```json
{
  "buildCommand": null,
  "outputDirectory": ".",
  "framework": null,
  "headers": [
    {
      "source": "/assets/(.*)",
      "headers": [
        { "key": "Cache-Control", "value": "public, max-age=31536000, immutable" }
      ]
    }
  ]
}
```

- [ ] **Step 3: Create `website/index.html` with full page structure**

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>PDF Studio — Edit PDFs at the Speed of Thought</title>
  <meta name="description" content="A professional-grade PDF editor. Edit text, fill forms, OCR scans, merge splits, redact secrets, sign contracts — all from one fast, offline desktop app." />
  <link rel="icon" type="image/svg+xml" href="assets/favicon.svg" />
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet" />
  <link rel="stylesheet" href="css/style.css" />
</head>
<body>
  <!-- Aurora background -->
  <div class="aurora" aria-hidden="true">
    <div class="aurora-blade aurora-blade-1"></div>
    <div class="aurora-blade aurora-blade-2"></div>
    <div class="aurora-blade aurora-blade-3"></div>
    <div class="aurora-blade aurora-blade-4"></div>
    <div class="aurora-blade aurora-blade-5"></div>
    <div class="aurora-grain"></div>
    <div class="aurora-vignette"></div>
  </div>

  <!-- Floating pill nav -->
  <nav class="pill-nav">
    <div class="pill-nav-inner">
      <a class="pill-nav-logo" href="#">
        <img src="assets/logo.svg" alt="PDF Studio" height="20" />
      </a>
      <div class="pill-nav-links">
        <a href="#product">Product</a>
        <a href="#docs">Docs</a>
        <a href="#pricing">Pricing</a>
        <a href="#changelog">Changelog</a>
        <a href="#blog">Blog</a>
      </div>
      <div class="pill-nav-actions">
        <a href="#" class="pill-nav-login">Log in</a>
        <a href="https://github.com/pdfstudio/pdfstudio/releases/latest" class="pill-nav-download">
          <span class="os-icon" id="nav-os-icon"></span> Download
        </a>
      </div>
    </div>
  </nav>

  <!-- Hero content -->
  <main class="hero">
    <div class="hero-content">
      <div class="eyebrow">v2.0 — now with an AI command bar</div>

      <h1 class="hero-title">
        Everything you build,<br />
        one <span class="gradient-word">keystroke</span> away.
      </h1>

      <p class="hero-subtitle">
        Edit text, fill forms, OCR scans, merge splits, redact secrets,<br />
        sign contracts — all from one fast, offline desktop app.
      </p>

      <div class="download-row">
        <a href="https://github.com/pdfstudio/pdfstudio/releases/latest" class="keycap-btn">
          <svg class="os-icon" viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
            <path d="M18.71 19.5c-.83 1.24-1.71 2.45-3.05 2.47-1.34.03-1.77-.79-3.29-.79-1.53 0-2 .77-3.27.82-1.31.05-2.3-1.32-3.14-2.53C4.25 17 2.94 12.45 4.7 9.39c.87-1.52 2.43-2.48 4.12-2.51 1.28-.02 2.5.87 3.29.87.78 0 2.26-1.07 3.8-.91.65.03 2.47.26 3.64 1.98-.09.06-2.17 1.28-2.15 3.81.03 3.02 2.65 4.03 2.68 4.04-.03.07-.42 1.44-1.38 2.83M13 3.5c.73-.83 1.94-1.46 2.94-1.5.13 1.17-.34 2.35-1.04 3.19-.69.85-1.83 1.51-2.95 1.42-.15-1.15.41-2.35 1.05-3.11z"/>
          </svg>
          Download for Mac
        </a>
        <a href="https://github.com/pdfstudio/pdfstudio/releases/latest" class="keycap-btn">
          <svg class="os-icon" viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
            <path d="M0 3.449L9.75 2.1v9.451H0m10.949-9.602L24 0v11.4H10.949M0 12.6h9.75v9.451L0 20.699M10.949 12.6H24V24l-12.9-1.801"/>
          </svg>
          Download for Windows
        </a>
      </div>

      <p class="install-caption">brew install pdfstudio — Install via homebrew or winget</p>

      <!-- Command bar mockup -->
      <div class="command-bar">
        <div class="command-bar-input">
          <svg class="search-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
          </svg>
          <span class="command-query">deploy staging</span>
          <span class="command-caret"></span>
          <span class="command-mode-pill">Command</span>
        </div>
        <div class="command-results">
          <div class="command-row active">
            <svg class="row-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>
            </svg>
            <span class="row-label">Deploy to <code>staging</code></span>
            <span class="row-shortcut"><kbd>Cmd</kbd><kbd>Enter</kbd></span>
          </div>
          <div class="command-row">
            <svg class="row-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2">
              <polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>
            </svg>
            <span class="row-label">Build <code>pnpm build</code></span>
            <span class="row-shortcut"><kbd>Cmd</kbd><kbd>B</kbd></span>
          </div>
          <div class="command-row">
            <svg class="row-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
            </svg>
            <span class="row-label">Open <code>~/projects/app</code></span>
            <span class="row-shortcut"><kbd>Cmd</kbd><kbd>O</kbd></span>
          </div>
          <div class="command-row">
            <svg class="row-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><polyline points="13 2 13 9 20 9"/>
            </svg>
            <span class="row-label">Quick open <code>README.md</code></span>
            <span class="row-shortcut"><kbd>Cmd</kbd><kbd>K</kbd></span>
          </div>
        </div>
        <div class="command-footer">
          <span class="hints">↑↓ navigate · enter open · esc dismiss</span>
          <span class="footer-wordmark">PDF Studio</span>
        </div>
      </div>

      <a href="#features" class="ghost-pill">Learn more →</a>
    </div>
  </main>

  <!-- Product Hunt badge -->
  <div class="ph-badge">
    <svg viewBox="0 0 24 24" width="14" height="14" fill="#da552f">
      <path d="M13.6 11.2V6.8h-3.2v4.4H7.2v3.2h3.2v4.4h3.2v-4.4h3.2v-3.2z"/>
    </svg>
    <span>Featured on Product Hunt</span>
    <span class="ph-rank">#1 Product of the Day</span>
  </div>

  <!-- Below the fold: Features -->
  <section class="features" id="features">
    <h2 class="section-title">Everything a PDF editor should be</h2>
    <p class="section-subtitle">Professional-grade tools that work offline, respect your privacy, and never get in your way.</p>
    <div class="features-grid">
      <div class="feature-card">
        <div class="feature-icon">👁️</div>
        <h3>View & Edit</h3>
        <p>Virtualised canvas for 100k+ pages, in-place text editing, rich-text boxes, find &amp; replace with regex.</p>
      </div>
      <div class="feature-card">
        <div class="feature-icon">📄</div>
        <h3>Pages & Forms</h3>
        <p>Insert, merge, split, crop, resize. Every AcroForm widget, QR/barcodes, mail-merge, flattening.</p>
      </div>
      <div class="feature-card">
        <div class="feature-icon">🔍</div>
        <h3>OCR & Search</h3>
        <p>Tesseract, EasyOCR, MuPDF back-ends. Full-text search with regex, confidence reporting.</p>
      </div>
      <div class="feature-card">
        <div class="feature-icon">🤖</div>
        <h3>AI Assistant</h3>
        <p>Offline summaries, Q&amp;A with page citations, keyword tagging, grammar checks. Optional OpenAI endpoint.</p>
      </div>
      <div class="feature-card">
        <div class="feature-icon">🔒</div>
        <h3>Security</h3>
        <p>AES-256 encryption, true redaction, digital signatures, watermarks, Bates numbering.</p>
      </div>
      <div class="feature-card">
        <div class="feature-icon">🔄</div>
        <h3>Convert</h3>
        <p>Import/export DOCX, PPTX, images, SVG, HTML, Markdown. PDF/A, PDF/X, PDF/UA profiles.</p>
      </div>
    </div>
  </section>

  <!-- How it works -->
  <section class="how-it-works">
    <h2 class="section-title">Get started in seconds</h2>
    <div class="steps">
      <div class="step">
        <div class="step-number">1</div>
        <h3>Download</h3>
        <p>Get PDF Studio for Mac, Windows, or Linux.</p>
      </div>
      <div class="step">
        <div class="step-number">2</div>
        <h3>Open</h3>
        <p>Drop any PDF, scanned doc, or form into the app.</p>
      </div>
      <div class="step">
        <div class="step-number">3</div>
        <h3>Edit</h3>
        <p>Text, images, forms, signatures — everything is editable.</p>
      </div>
    </div>
  </section>

  <!-- Footer -->
  <footer class="footer">
    <div class="footer-inner">
      <div class="footer-brand">
        <img src="assets/logo.svg" alt="PDF Studio" height="18" />
      </div>
      <div class="footer-links">
        <a href="https://github.com/pdfstudio/pdfstudio">GitHub</a>
        <a href="#">Documentation</a>
        <a href="#">Changelog</a>
        <a href="#">MIT License</a>
      </div>
      <div class="footer-copy">
        Made with ♥ by PDF Studio team · © 2026
      </div>
    </div>
  </footer>

  <script src="js/main.js"></script>
</body>
</html>
```

- [ ] **Step 4: Verify file structure**

Run: `ls -la website/ && ls -la website/css/ && ls -la website/js/ && ls -la website/assets/`
Expected: All directories exist, `index.html` and `vercel.json` present.

---

### Task 2: Create CSS with aurora animation and all component styles

**Files:**
- Create: `website/css/style.css`

**Interfaces:** Consumes HTML classes from Task 1. Produces all visual styling.

- [ ] **Step 1: Create `website/css/style.css` with CSS custom properties and reset**

```css
/* === Reset & Base === */
*, *::before, *::after {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

:root {
  --canvas: #07080a;
  --white: #ffffff;
  --muted: #9c9c9d;
  --subtitle: #b0b0b2;
  --crimson: #ff2f3a;
  --coral: #ff6b4a;
  --amber: #ffb347;
  --btn-fill: #e6e6e6;
  --btn-text: #2f3031;
  --glass-bg: rgba(15, 15, 18, 0.8);
  --glass-border: rgba(255, 255, 255, 0.06);
  --glass-blur: blur(20px);
  --active-row: rgba(255, 47, 58, 0.12);
}

html {
  scroll-behavior: smooth;
}

body {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
  background: var(--canvas);
  color: var(--white);
  overflow-x: hidden;
  min-height: 100vh;
}

a {
  color: inherit;
  text-decoration: none;
}

code {
  font-family: 'GeistMono', 'SF Mono', 'Fira Code', monospace;
  font-size: 0.85em;
  background: rgba(255, 255, 255, 0.06);
  padding: 2px 6px;
  border-radius: 4px;
}
```

- [ ] **Step 2: Add aurora background styles**

Append to `style.css`:

```css
/* === Aurora Background === */
.aurora {
  position: fixed;
  inset: 0;
  z-index: 0;
  overflow: hidden;
  pointer-events: none;
}

.aurora-blade {
  position: absolute;
  width: 600px;
  height: 1600px;
  border-radius: 50%;
  filter: blur(80px);
  mix-blend-mode: screen;
  opacity: 0;
}

.aurora-blade-1 {
  background: linear-gradient(35deg, var(--crimson) 0%, var(--coral) 40%, var(--amber) 70%, transparent 100%);
  top: -20%;
  left: 10%;
  transform: rotate(35deg);
  animation: drift-1 18s ease-in-out infinite;
}

.aurora-blade-2 {
  background: linear-gradient(40deg, var(--crimson) 0%, var(--coral) 35%, var(--amber) 65%, transparent 100%);
  top: -10%;
  left: 40%;
  width: 500px;
  height: 1400px;
  transform: rotate(38deg);
  animation: drift-2 22s ease-in-out infinite;
}

.aurora-blade-3 {
  background: linear-gradient(32deg, var(--crimson) 0%, var(--coral) 45%, var(--amber) 75%, transparent 100%);
  top: -15%;
  right: 15%;
  width: 700px;
  height: 1800px;
  transform: rotate(33deg);
  animation: drift-3 15s ease-in-out infinite;
}

.aurora-blade-4 {
  background: linear-gradient(38deg, var(--coral) 0%, var(--amber) 50%, transparent 100%);
  top: 10%;
  left: 25%;
  width: 450px;
  height: 1200px;
  transform: rotate(40deg);
  animation: drift-4 20s ease-in-out infinite;
}

.aurora-blade-5 {
  background: linear-gradient(36deg, var(--crimson) 0%, var(--amber) 60%, transparent 100%);
  top: -5%;
  right: 30%;
  width: 550px;
  height: 1500px;
  transform: rotate(36deg);
  animation: drift-5 17s ease-in-out infinite;
}

/* Grain overlay */
.aurora-grain {
  position: absolute;
  inset: 0;
  opacity: 0.04;
  pointer-events: none;
  background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.65' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)'/%3E%3C/svg%3E");
  background-repeat: repeat;
  background-size: 256px 256px;
}

/* Vignette */
.aurora-vignette {
  position: absolute;
  inset: 0;
  background: radial-gradient(ellipse at center, transparent 40%, var(--canvas) 100%);
  pointer-events: none;
}

/* Drift keyframes */
@keyframes drift-1 {
  0%   { transform: translate(0, 0) rotate(35deg) scale(1);    opacity: 0.7; }
  50%  { transform: translate(60px, -40px) rotate(38deg) scale(1.05); opacity: 0.9; }
  100% { transform: translate(0, 0) rotate(35deg) scale(1);    opacity: 0.7; }
}

@keyframes drift-2 {
  0%   { transform: translate(0, 0) rotate(38deg) scale(1);    opacity: 0.6; }
  50%  { transform: translate(-50px, 30px) rotate(41deg) scale(1.08); opacity: 0.85; }
  100% { transform: translate(0, 0) rotate(38deg) scale(1);    opacity: 0.6; }
}

@keyframes drift-3 {
  0%   { transform: translate(0, 0) rotate(33deg) scale(1);    opacity: 0.65; }
  50%  { transform: translate(40px, -50px) rotate(36deg) scale(1.03); opacity: 0.88; }
  100% { transform: translate(0, 0) rotate(33deg) scale(1);    opacity: 0.65; }
}

@keyframes drift-4 {
  0%   { transform: translate(0, 0) rotate(40deg) scale(1);    opacity: 0.5; }
  50%  { transform: translate(-30px, 40px) rotate(43deg) scale(1.06); opacity: 0.75; }
  100% { transform: translate(0, 0) rotate(40deg) scale(1);    opacity: 0.5; }
}

@keyframes drift-5 {
  0%   { transform: translate(0, 0) rotate(36deg) scale(1);    opacity: 0.55; }
  50%  { transform: translate(35px, -35px) rotate(39deg) scale(1.04); opacity: 0.8; }
  100% { transform: translate(0, 0) rotate(36deg) scale(1);    opacity: 0.55; }
}
```

- [ ] **Step 3: Add floating pill nav styles**

Append to `style.css`:

```css
/* === Floating Pill Nav === */
.pill-nav {
  position: fixed;
  top: 20px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 100;
  width: auto;
  max-width: 900px;
  padding: 0 20px;
}

.pill-nav-inner {
  display: flex;
  align-items: center;
  gap: 24px;
  background: rgba(7, 8, 10, 0.7);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 9999px;
  padding: 10px 12px 10px 16px;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
}

.pill-nav-logo img {
  display: block;
  height: 20px;
  width: auto;
}

.pill-nav-links {
  display: flex;
  align-items: center;
  gap: 20px;
}

.pill-nav-links a {
  font-size: 14px;
  font-weight: 500;
  color: var(--muted);
  transition: color 0.2s;
}

.pill-nav-links a:hover {
  color: var(--white);
}

.pill-nav-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-left: auto;
}

.pill-nav-login {
  font-size: 14px;
  font-weight: 500;
  color: var(--muted);
  transition: color 0.2s;
}

.pill-nav-login:hover {
  color: var(--white);
}

.pill-nav-download {
  display: flex;
  align-items: center;
  gap: 6px;
  background: var(--white);
  color: var(--canvas);
  font-size: 13px;
  font-weight: 600;
  padding: 6px 14px;
  border-radius: 8px;
  transition: opacity 0.2s;
}

.pill-nav-download:hover {
  opacity: 0.9;
}

.pill-nav-download .os-icon {
  width: 14px;
  height: 14px;
}
```

- [ ] **Step 4: Add hero content styles**

Append to `style.css`:

```css
/* === Hero Content === */
.hero {
  position: relative;
  z-index: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  padding: 120px 24px 60px;
}

.hero-content {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  max-width: 800px;
}

/* Eyebrow */
.eyebrow {
  display: inline-flex;
  align-items: center;
  font-size: 12px;
  font-weight: 500;
  color: var(--muted);
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 9999px;
  padding: 6px 16px;
  margin-bottom: 24px;
}

/* Headline */
.hero-title {
  font-size: 64px !important;
  font-weight: 600;
  line-height: 1.1;
  color: var(--white);
  margin-bottom: 20px;
  letter-spacing: -0.02em;
}

.gradient-word {
  background: linear-gradient(135deg, var(--amber), var(--coral), var(--crimson));
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}

/* Subtitle */
.hero-subtitle {
  font-size: 18px;
  font-weight: 400;
  color: var(--subtitle);
  letter-spacing: 0.2px;
  line-height: 1.6;
  max-width: 640px;
  margin-bottom: 32px;
}
```

- [ ] **Step 5: Add keycap button styles**

Append to `style.css`:

```css
/* === Keycap Download Buttons === */
.download-row {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-bottom: 16px;
}

.keycap-btn {
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--btn-fill);
  color: var(--btn-text);
  font-family: 'Inter', sans-serif;
  font-size: 14px;
  font-weight: 500;
  padding: 12px 20px;
  border-radius: 8px;
  border: none;
  cursor: pointer;
  text-decoration: none;
  box-shadow:
    0 2px 0 #000000,
    0 0 14px rgba(255, 255, 255, 0.19),
    inset 0 1px 0 rgba(255, 255, 255, 0.5),
    inset 0 -1px 0 rgba(0, 0, 0, 0.1);
  transition: transform 0.15s, box-shadow 0.15s;
}

.keycap-btn:hover {
  transform: translateY(-1px);
  box-shadow:
    0 3px 0 #000000,
    0 0 20px rgba(255, 255, 255, 0.25),
    inset 0 1px 0 rgba(255, 255, 255, 0.5),
    inset 0 -1px 0 rgba(0, 0, 0, 0.1);
}

.keycap-btn:active {
  transform: translateY(1px);
  box-shadow:
    0 1px 0 #000000,
    0 0 8px rgba(255, 255, 255, 0.12),
    inset 0 1px 0 rgba(255, 255, 255, 0.3),
    inset 0 -1px 0 rgba(0, 0, 0, 0.15);
}

.keycap-btn .os-icon {
  width: 16px;
  height: 16px;
  flex-shrink: 0;
}

/* Install caption */
.install-caption {
  font-family: 'GeistMono', 'SF Mono', 'Fira Code', monospace;
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 48px;
}
```

- [ ] **Step 6: Add command bar styles**

Append to `style.css`:

```css
/* === Command Bar Mockup === */
.command-bar {
  width: 100%;
  max-width: 640px;
  background: var(--glass-bg);
  backdrop-filter: var(--glass-blur);
  -webkit-backdrop-filter: var(--glass-blur);
  border: 1px solid var(--glass-border);
  border-radius: 14px;
  box-shadow:
    0 8px 32px rgba(255, 47, 58, 0.06),
    0 2px 8px rgba(0, 0, 0, 0.4);
  overflow: hidden;
  margin-bottom: 32px;
}

.command-bar-input {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 16px;
  border-bottom: 1px solid var(--glass-border);
}

.search-icon {
  color: var(--muted);
  flex-shrink: 0;
}

.command-query {
  font-size: 14px;
  color: var(--white);
}

.command-caret {
  display: inline-block;
  width: 2px;
  height: 18px;
  background: var(--coral);
  animation: blink 1s step-end infinite;
}

@keyframes blink {
  0%, 100% { opacity: 1; }
  50% { opacity: 0; }
}

.command-mode-pill {
  margin-left: auto;
  font-size: 11px;
  font-weight: 500;
  color: var(--muted);
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 4px;
  padding: 3px 8px;
}

.command-results {
  padding: 6px;
}

.command-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-radius: 8px;
  transition: background 0.15s;
}

.command-row:hover {
  background: rgba(255, 255, 255, 0.04);
}

.command-row.active {
  background: var(--active-row);
  border-left: 2px solid var(--crimson);
}

.command-row .row-icon {
  color: var(--muted);
  flex-shrink: 0;
}

.command-row.active .row-icon {
  color: var(--crimson);
}

.command-row .row-label {
  font-size: 14px;
  color: var(--subtitle);
  flex: 1;
}

.command-row.active .row-label {
  color: var(--white);
}

.row-shortcut {
  display: flex;
  gap: 4px;
}

.row-shortcut kbd {
  font-family: 'GeistMono', 'SF Mono', 'Fira Code', monospace;
  font-size: 11px;
  color: var(--muted);
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 4px;
  padding: 2px 6px;
}

.command-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 16px;
  border-top: 1px solid var(--glass-border);
}

.command-footer .hints {
  font-family: 'GeistMono', 'SF Mono', 'Fira Code', monospace;
  font-size: 11px;
  color: var(--muted);
}

.command-footer .footer-wordmark {
  font-size: 11px;
  font-weight: 600;
  color: var(--muted);
}
```

- [ ] **Step 7: Add ghost pill and Product Hunt badge styles**

Append to `style.css`:

```css
/* === Ghost Pill === */
.ghost-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: transparent;
  border: 1px solid rgba(255, 255, 255, 0.15);
  border-radius: 9999px;
  padding: 10px 20px;
  font-size: 14px;
  font-weight: 500;
  color: var(--muted);
  transition: border-color 0.2s, color 0.2s;
}

.ghost-pill:hover {
  border-color: rgba(255, 255, 255, 0.3);
  color: var(--white);
}

/* === Product Hunt Badge === */
.ph-badge {
  position: fixed;
  bottom: 24px;
  right: 24px;
  z-index: 100;
  display: flex;
  align-items: center;
  gap: 8px;
  background: rgba(15, 15, 18, 0.9);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 12px;
  padding: 10px 14px;
  font-size: 12px;
  color: var(--muted);
}

.ph-badge .ph-rank {
  color: var(--white);
  font-weight: 600;
}
```

- [ ] **Step 8: Add below-the-fold section styles**

Append to `style.css`:

```css
/* === Features Section === */
.features {
  position: relative;
  z-index: 1;
  padding: 80px 24px;
  max-width: 1100px;
  margin: 0 auto;
}

.section-title {
  font-size: 36px;
  font-weight: 600;
  text-align: center;
  margin-bottom: 12px;
}

.section-subtitle {
  font-size: 16px;
  color: var(--subtitle);
  text-align: center;
  max-width: 560px;
  margin: 0 auto 48px;
}

.features-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 20px;
}

.feature-card {
  background: rgba(15, 15, 18, 0.6);
  border: 1px solid rgba(255, 255, 255, 0.06);
  border-radius: 12px;
  padding: 24px;
  transition: transform 0.2s, border-color 0.2s;
}

.feature-card:hover {
  transform: translateY(-2px);
  border-color: rgba(255, 255, 255, 0.12);
}

.feature-icon {
  font-size: 24px;
  margin-bottom: 12px;
}

.feature-card h3 {
  font-size: 16px;
  font-weight: 600;
  margin-bottom: 8px;
}

.feature-card p {
  font-size: 14px;
  color: var(--subtitle);
  line-height: 1.5;
}

/* === How It Works === */
.how-it-works {
  position: relative;
  z-index: 1;
  padding: 80px 24px;
  max-width: 900px;
  margin: 0 auto;
}

.steps {
  display: flex;
  gap: 40px;
  justify-content: center;
}

.step {
  flex: 1;
  text-align: center;
}

.step-number {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  background: rgba(255, 47, 58, 0.12);
  border: 1px solid rgba(255, 47, 58, 0.2);
  border-radius: 50%;
  font-size: 16px;
  font-weight: 600;
  color: var(--crimson);
  margin-bottom: 16px;
}

.step h3 {
  font-size: 18px;
  font-weight: 600;
  margin-bottom: 8px;
}

.step p {
  font-size: 14px;
  color: var(--subtitle);
  line-height: 1.5;
}

/* === Footer === */
.footer {
  position: relative;
  z-index: 1;
  border-top: 1px solid rgba(255, 255, 255, 0.06);
  padding: 40px 24px;
}

.footer-inner {
  max-width: 1100px;
  margin: 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.footer-links {
  display: flex;
  gap: 24px;
}

.footer-links a {
  font-size: 13px;
  color: var(--muted);
  transition: color 0.2s;
}

.footer-links a:hover {
  color: var(--white);
}

.footer-copy {
  font-size: 12px;
  color: var(--muted);
}
```

- [ ] **Step 9: Add responsive styles**

Append to `style.css`:

```css
/* === Responsive === */
@media (max-width: 1024px) {
  .hero-title {
    font-size: 48px !important;
  }

  .pill-nav-links {
    display: none;
  }
}

@media (max-width: 768px) {
  .hero-title {
    font-size: 36px !important;
  }

  .download-row {
    flex-direction: column;
    width: 100%;
  }

  .keycap-btn {
    width: 100%;
    justify-content: center;
  }

  .command-bar {
    max-width: 100%;
  }

  .features-grid {
    grid-template-columns: 1fr;
  }

  .steps {
    flex-direction: column;
    gap: 32px;
  }

  .footer-inner {
    flex-direction: column;
    gap: 20px;
    text-align: center;
  }
}

@media (max-width: 480px) {
  .hero-title {
    font-size: 28px !important;
  }

  .hero {
    padding: 100px 16px 40px;
  }

  .ph-badge {
    display: none;
  }
}
```

- [ ] **Step 10: Verify CSS loads**

Open `website/index.html` in a browser. Expected: Dark canvas, aurora animating, all components visible and styled.

---

### Task 3: Create SVG assets (logos and icons)

**Files:**
- Create: `website/assets/logo.svg`
- Create: `website/assets/logo-icon.svg`
- Create: `website/assets/favicon.svg`
- Create: `website/assets/apple.svg`
- Create: `website/assets/windows.svg`

**Interfaces:** Consumed by `index.html` `<img>` tags and inline SVGs.

- [ ] **Step 1: Create `website/assets/logo.svg` — warm diamond + wordmark**

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 32" fill="none">
  <defs>
    <linearGradient id="diamond-grad" x1="0" y1="0" x2="24" y2="24" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#ffb347"/>
      <stop offset="50%" stop-color="#ff6b4a"/>
      <stop offset="100%" stop-color="#ff2f3a"/>
    </linearGradient>
  </defs>
  <!-- Diamond mark -->
  <path d="M16 2 L28 16 L16 30 L4 16 Z" fill="url(#diamond-grad)"/>
  <path d="M16 2 L16 30" stroke="rgba(255,255,255,0.3)" stroke-width="0.5"/>
  <path d="M4 16 L28 16" stroke="rgba(255,255,255,0.3)" stroke-width="0.5"/>
  <!-- Wordmark -->
  <text x="36" y="21" font-family="Inter, sans-serif" font-size="18" font-weight="600" fill="#ffffff">PDF Studio</text>
</svg>
```

- [ ] **Step 2: Create `website/assets/logo-icon.svg` — diamond mark only**

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" fill="none">
  <defs>
    <linearGradient id="diamond-grad" x1="0" y1="0" x2="32" y2="32" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#ffb347"/>
      <stop offset="50%" stop-color="#ff6b4a"/>
      <stop offset="100%" stop-color="#ff2f3a"/>
    </linearGradient>
  </defs>
  <path d="M16 2 L30 16 L16 30 L2 16 Z" fill="url(#diamond-grad)"/>
  <path d="M16 2 L16 30" stroke="rgba(255,255,255,0.3)" stroke-width="0.5"/>
  <path d="M2 16 L30 16" stroke="rgba(255,255,255,0.3)" stroke-width="0.5"/>
</svg>
```

- [ ] **Step 3: Create `website/assets/favicon.svg` — simplified for small sizes**

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" fill="none">
  <path d="M16 2 L30 16 L16 30 L2 16 Z" fill="#ff6b4a"/>
</svg>
```

- [ ] **Step 4: Create `website/assets/apple.svg`**

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
  <path d="M18.71 19.5c-.83 1.24-1.71 2.45-3.05 2.47-1.34.03-1.77-.79-3.29-.79-1.53 0-2 .77-3.27.82-1.31.05-2.3-1.32-3.14-2.53C4.25 17 2.94 12.45 4.7 9.39c.87-1.52 2.43-2.48 4.12-2.51 1.28-.02 2.5.87 3.29.87.78 0 2.26-1.07 3.8-.91.65.03 2.47.26 3.64 1.98-.09.06-2.17 1.28-2.15 3.81.03 3.02 2.65 4.03 2.68 4.04-.03.07-.42 1.44-1.38 2.83M13 3.5c.73-.83 1.94-1.46 2.94-1.5.13 1.17-.34 2.35-1.04 3.19-.69.85-1.83 1.51-2.95 1.42-.15-1.15.41-2.35 1.05-3.11z"/>
</svg>
```

- [ ] **Step 5: Create `website/assets/windows.svg`**

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
  <path d="M0 3.449L9.75 2.1v9.451H0m10.949-9.602L24 0v11.4H10.949M0 12.6h9.75v9.451L0 20.699M10.949 12.6H24V24l-12.9-1.801"/>
</svg>
```

- [ ] **Step 6: Verify assets load**

Open `website/index.html` in a browser. Expected: Logo renders in nav, favicon shows in tab, OS icons display in buttons.

---

### Task 4: Create JavaScript for UA detection and caret blink

**Files:**
- Create: `website/js/main.js`

**Interfaces:** Consumes DOM elements from `index.html`. No exports.

- [ ] **Step 1: Create `website/js/main.js`**

```js
// OS detection for download buttons and nav
(function () {
  const ua = navigator.userAgent.toLowerCase();
  const isMac = ua.includes('mac');
  const isWindows = ua.includes('win');

  // Set nav download icon
  const navIcon = document.getElementById('nav-os-icon');
  if (navIcon) {
    if (isMac) {
      navIcon.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M18.71 19.5c-.83 1.24-1.71 2.45-3.05 2.47-1.34.03-1.77-.79-3.29-.79-1.53 0-2 .77-3.27.82-1.31.05-2.3-1.32-3.14-2.53C4.25 17 2.94 12.45 4.7 9.39c.87-1.52 2.43-2.48 4.12-2.51 1.28-.02 2.5.87 3.29.87.78 0 2.26-1.07 3.8-.91.65.03 2.47.26 3.64 1.98-.09.06-2.17 1.28-2.15 3.81.03 3.02 2.65 4.03 2.68 4.04-.03.07-.42 1.44-1.38 2.83M13 3.5c.73-.83 1.94-1.46 2.94-1.5.13 1.17-.34 2.35-1.04 3.19-.69.85-1.83 1.51-2.95 1.42-.15-1.15.41-2.35 1.05-3.11z"/></svg>';
    } else if (isWindows) {
      navIcon.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M0 3.449L9.75 2.1v9.451H0m10.949-9.602L24 0v11.4H10.949M0 12.6h9.75v9.451L0 20.699M10.949 12.6H24V24l-12.9-1.801"/></svg>';
    }
  }

  // Hide non-relevant download button
  const btns = document.querySelectorAll('.keycap-btn');
  btns.forEach((btn) => {
    const text = btn.textContent.trim();
    if (isMac && text.includes('Windows')) {
      btn.style.display = 'none';
    } else if (isWindows && text.includes('Mac')) {
      btn.style.display = 'none';
    }
  });
})();

// Smooth scroll for anchor links
document.querySelectorAll('a[href^="#"]').forEach((anchor) => {
  anchor.addEventListener('click', function (e) {
    const target = document.querySelector(this.getAttribute('href'));
    if (target) {
      e.preventDefault();
      target.scrollIntoView({ behavior: 'smooth' });
    }
  });
});
```

- [ ] **Step 2: Verify JS works**

Open `website/index.html` on Mac — expected: Apple button visible, Windows hidden, nav shows Apple icon.
Open on Windows — expected: Windows button visible, Apple hidden, nav shows Windows icon.
Click "Learn more →" — expected: smooth scroll to features section.

---

### Task 5: Add GeistMono font and finalize typography

**Files:**
- Modify: `website/index.html` (add font link)
- Modify: `website/css/style.css` (add GeistMono font-face)

**Interfaces:** Consumes existing CSS custom properties.

- [ ] **Step 1: Add GeistMono font link to `index.html` `<head>`**

Add before the Inter font link:

```html
<link href="https://fonts.cdnfonts.com/css/geist-mono" rel="stylesheet" />
```

- [ ] **Step 2: Verify mono font renders**

Check the install caption and keyboard shortcut chips — expected: GeistMono font, not fallback monospace.

---

### Task 6: Final polish and responsive testing

**Files:**
- Modify: `website/css/style.css` (any fixes found during testing)

**Interfaces:** None — visual polish pass.

- [ ] **Step 1: Test at 1440px width**

Expected: Full hero visible, aurora animating, nav centered, all components spaced correctly.

- [ ] **Step 2: Test at 1024px width**

Expected: Headline at 48px, nav links hidden (hamburger if implemented, or collapsed).

- [ ] **Step 3: Test at 768px width**

Expected: Headline at 36px, buttons stacked full-width, command bar scales to 100%.

- [ ] **Step 4: Test at 480px width**

Expected: Headline at 28px, Product Hunt badge hidden, minimal padding.

- [ ] **Step 5: Verify aurora 0% keyframe is full bloom**

Pause CSS animations in DevTools — expected: aurora still looks rich at 0% state.

- [ ] **Step 6: Verify no purple/indigo anywhere**

Inspect all gradient values — expected: only crimson/coral/amber warm tones.

---

### Task 7: Deploy to Vercel

**Files:**
- Modify: `website/vercel.json` (if needed)

**Interfaces:** None — deployment step.

- [ ] **Step 1: Commit all website files**

```bash
cd website
git add .
git commit -m "feat: add PDF Studio landing page"
```

- [ ] **Step 2: Connect to Vercel**

1. Go to vercel.com → New Project
2. Import the GitHub repo
3. Set root directory to `website/`
4. Framework: Other
5. Build command: (leave empty)
6. Output directory: `.`
7. Deploy

- [ ] **Step 3: Verify deployment**

Visit the Vercel URL. Expected: All components render, aurora animates, download buttons link to GitHub releases, responsive breakpoints work.

- [ ] **Step 4: Add custom domain (optional)**

In Vercel dashboard → Settings → Domains → add custom domain.

---

## Self-Review Checklist

- [x] Spec coverage: All 7 components (aurora, nav, headline, buttons, command bar, badge, ghost pill) + below-fold sections have tasks
- [x] Placeholder scan: No TBD/TODO found
- [x] Type consistency: CSS class names consistent between HTML and CSS
- [x] File structure matches spec
- [x] Download links point to GitHub releases
- [x] Aurora strictly warm (no purple)
- [x] 0% keyframe at full bloom
- [x] Scoped selectors (not bare tags) for font sizes
