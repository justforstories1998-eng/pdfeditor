# PDF Studio Landing Page — Design Spec

## Overview

A premium dark developer-tool landing page for PDF Studio, deployed as a
static site to Vercel. Single 1440-wide desktop viewport hero with a living
animated warm aurora background as the signature visual.

**Tech stack:** Plain HTML/CSS/JS (no build step). Vercel static hosting.
**Download links:** GitHub releases (`github.com/pdfstudio/pdfstudio/releases/latest`).
**Backend:** None — purely static.

---

## File Structure

```
website/
├── index.html              # Single-page landing
├── css/
│   └── style.css           # All styles
├── js/
│   └── main.js             # Blinking caret, UA detection
├── assets/
│   ├── logo.svg            # Warm diamond + "PDF Studio" wordmark
│   ├── logo-icon.svg       # Diamond mark only (app icon)
│   ├── favicon.svg         # Browser tab icon
│   ├── apple.svg           # Apple OS glyph
│   └── windows.svg         # Windows OS glyph
└── vercel.json             # Optional headers/redirects
```

---

## Canvas & Palette

- **Base:** `#07080a` near-black
- **Type:** `#ffffff` (primary), `#9c9c9d` (muted), `#b0b0b2` (subtitle)
- **Aurora:** `#ff2f3a` (crimson core) → `#ff6b4a` (coral) → `#ffb347` (amber tips)
- **Button fill:** `#e6e6e6`, button text `#2f3031`
- **Active row:** `rgba(255,47,58,0.12)` warm crimson tint
- **Strictly warm:** No purple/indigo/violet anywhere
- **Typography:** Inter (all UI), GeistMono (mono caption + shortcut chips only)

---

## Component 1: Animated Aurora Background

Fill the entire `#07080a` canvas with a living warm aurora.

### Light blades

- 5 absolutely-positioned `div.aurora-blade` elements
- Each has a `linear-gradient` at ~35-45° angle: `#ff2f3a` (0%) → `#ff6b4a` (40%) → `#ffb347` (70%) → transparent (100%)
- `mix-blend-mode: screen`
- Varying sizes (400-800px wide, 1200-2000px tall), positions, and rotations
- `filter: blur(80px)` for soft-focus

### Animation

3 staggered `@keyframes` on ~15s, 18s, 22s loops:

```css
@keyframes drift-1 {
  0%   { transform: translate(0, 0) rotate(35deg) scale(1);   opacity: 0.7; }
  50%  { transform: translate(60px, -40px) rotate(38deg) scale(1.05); opacity: 0.9; }
  100% { transform: translate(0, 0) rotate(35deg) scale(1);   opacity: 0.7; }
}
```

- 0% keyframe at full bloom so static screenshots look rich
- Each blade gets a different keyframe and duration (staggered)

### Grain overlay

- Inline SVG with `feTurbulence` (type="fractalNoise", baseFrequency="0.65", numOctaves="3")
- Applied as a `::after` pseudo-element on the hero, `opacity: 0.04`, `pointer-events: none`

### Vignette

- `::before` pseudo-element with `radial-gradient(ellipse at center, transparent 40%, #07080a 100%)`
- `pointer-events: none`

---

## Component 2: Floating Pill Nav

Centered near top, ~48px tall, max-width ~900px.

### Structure

```html
<nav class="pill-nav">
  <div class="pill-nav-inner">
    <a class="pill-nav-logo">
      <img src="assets/logo.svg" alt="PDF Studio" />
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
      <a href="#" class="pill-nav-download">
        <span class="os-icon"></span> Download
      </a>
    </div>
  </div>
</nav>
```

### Styles

- `background: rgba(7,8,10,0.7)`, `backdrop-filter: blur(12px)`
- `border: 1px solid rgba(255,255,255,0.08)`, `border-radius: 9999px`
- Inner top highlight: `box-shadow: inset 0 1px 0 rgba(255,255,255,0.04)`
- Links: Inter 14px/500, `#9c9c9d`, hover → `#ffffff`
- Download pill: `background: #ffffff`, `color: #07080a`, 8px radius, small padding

---

## Component 3: Headline Block

### Eyebrow chip

```html
<div class="eyebrow">v2.0 — now with an AI command bar</div>
```

- Inter 12px/500, `#9c9c9d`
- `background: rgba(255,255,255,0.06)`, `border: 1px solid rgba(255,255,255,0.08)`
- `border-radius: 9999px`, padding 6px 16px

### H1

```html
<h1 class="hero-title">
  Everything you build,<br />
  one <span class="gradient-word">keystroke</span> away.
</h1>
```

- Inter 64px / weight 600 / line-height 1.1, `#ffffff`
- `.hero-title` scoped with `font-size: 64px !important` (survives preview hosts)
- `.gradient-word`: `background: linear-gradient(135deg, #ffb347, #ff6b4a, #ff2f3a); -webkit-background-clip: text; -webkit-text-fill-color: transparent;`

### Subtitle

```html
<p class="hero-subtitle">
  Edit text, fill forms, OCR scans, merge splits, redact secrets,<br />
  sign contracts — all from one fast, offline desktop app.
</p>
```

- Inter 18px/400, `#b0b0b2`, letter-spacing 0.2px
- Max-width 640px, centered

---

## Component 4: Keycap Download Buttons

Two side-by-side buttons in a flex row, gap 16px.

### Button markup

```html
<a href="https://github.com/pdfstudio/pdfstudio/releases/latest" class="keycap-btn">
  <svg class="os-icon"><!-- Apple glyph --></svg>
  Download for Mac
</a>
<a href="https://github.com/pdfstudio/pdfstudio/releases/latest" class="keycap-btn">
  <svg class="os-icon"><!-- Windows glyph --></svg>
  Download for Windows
</a>
```

### Styles

- `background: #e6e6e6`, `color: #2f3031`, Inter 14px/500
- `border-radius: 8px`, `padding: 12px 20px`
- Layered shadow stack:
  - `0 2px 0 #000000` (black ring)
  - `0 0 14px rgba(255,255,255,0.19)` (white outer glow)
  - `inset 0 1px 0 rgba(255,255,255,0.5)` (top highlight)
  - `inset 0 -1px 0 rgba(0,0,0,0.1)` (bottom dark edge)
- Hover: slight translateY(-1px) + brighter glow
- Active: translateY(1px) + reduced shadow (press feel)

### Install caption

```html
<p class="install-caption">brew install pdfstudio — Install via homebrew or winget</p>
```

- GeistMono 12px, `#9c9c9d`, centered

---

## Component 5: Command Bar Mockup

Dark-glass panel, ~640px wide, centered.

### Structure

```html
<div class="command-bar">
  <div class="command-bar-input">
    <svg class="search-icon"><!-- search glyph --></svg>
    <span class="command-query">deploy staging</span>
    <span class="command-caret"></span>
    <span class="command-mode-pill">Command</span>
  </div>
  <div class="command-results">
    <div class="command-row active">
      <svg class="row-icon"><!-- icon --></svg>
      <span class="row-label">Deploy to <code>staging</code></span>
      <span class="row-shortcut"><kbd>Cmd</kbd><kbd>Enter</kbd></span>
    </div>
    <div class="command-row">
      <svg class="row-icon"><!-- icon --></svg>
      <span class="row-label">Build <code>pnpm build</code></span>
      <span class="row-shortcut"><kbd>Cmd</kbd><kbd>B</kbd></span>
    </div>
    <div class="command-row">
      <svg class="row-icon"><!-- icon --></svg>
      <span class="row-label">Open <code>~/projects/app</code></span>
      <span class="row-shortcut"><kbd>Cmd</kbd><kbd>O</kbd></span>
    </div>
    <div class="command-row">
      <svg class="row-icon"><!-- icon --></svg>
      <span class="row-label">Quick open <code>README.md</code></span>
      <span class="row-shortcut"><kbd>Cmd</kbd><kbd>K</kbd></span>
    </div>
  </div>
  <div class="command-footer">
    <span class="hints">↑↓ navigate · enter open · esc dismiss</span>
    <span class="footer-wordmark">PDF Studio</span>
  </div>
</div>
```

### Styles

- Panel: `background: rgba(15,15,18,0.8)`, `backdrop-filter: blur(20px)`, 14px radius
- `border: 1px solid rgba(255,255,255,0.06)`
- `box-shadow: 0 8px 32px rgba(255,47,58,0.06), 0 2px 8px rgba(0,0,0,0.4)` (warm-tinted)
- Active row: `background: rgba(255,47,58,0.12)`, left accent border `#ff2f3a`
- Caret: `2px solid #ff6b4a`, blinking `@keyframes blink` (1s step-end)
- Shortcut chips: GeistMono 11px, `rgba(255,255,255,0.06)` bg, 4px radius
- Footer: GeistMono 11px, `#9c9c9d`

---

## Component 6: Product Hunt Badge

Floating bottom-right, positioned `absolute` or `fixed`.

```html
<div class="ph-badge">
  <img src="ph-logo.svg" alt="Product Hunt" />
  <span>Featured on Product Hunt</span>
  <span class="ph-rank">#1 Product of the Day</span>
</div>
```

- `background: rgba(15,15,18,0.9)`, 12px radius, 1px border `rgba(255,255,255,0.08)`
- Muted, small, unobtrusive

---

## Component 7: Ghost Pill

Single centered link below command bar.

```html
<a href="#features" class="ghost-pill">Learn more →</a>
```

- `background: transparent`, `border: 1px solid rgba(255,255,255,0.15)`
- Inter 14px/500, `#9c9c9d`, 20px radius
- Hover: border brightens, text → `#ffffff`

---

## Below-the-Fold Sections

### Features grid

3-column grid, 6 cards:

| Icon | Title | Description |
|------|-------|-------------|
| 👁️ | View & Edit | Virtualised canvas, in-place text editing, rich-text boxes |
| 📄 | Pages & Forms | Insert, merge, split, crop, every AcroForm widget |
| 🔍 | OCR & Search | Tesseract/EasyOCR, full-text search with regex |
| 🤖 | AI Assistant | Offline summaries, Q&A with page citations, grammar checks |
| 🔒 | Security | AES-256 encryption, true redaction, digital signatures |
| 🔄 | Convert | Import/export DOCX, PPTX, images, SVG, PDF/A |

Each card: dark glass bg, 12px radius, subtle border, hover lift.

### How it works

3-step horizontal flow:

1. **Download** — Get PDF Studio for your platform
2. **Open** — Drop any PDF, scanned doc, or form
3. **Edit** — Text, images, forms, signatures — everything is editable

### Footer

- Logo + wordmark left
- Links: GitHub, Documentation, Changelog, License (MIT)
- "Made with ❤️ by PDF Studio team" center
- Copyright 2026 right

---

## Responsive Behavior

- **≤1024px:** Headline steps down to 48px, nav links collapse to hamburger
- **≤768px:** Headline 36px, buttons stack full-width, command bar scales to 100%
- **≤480px:** Headline 28px, minimal padding

---

## Deployment

### Vercel config (`vercel.json`)

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

### Setup

1. Push `website/` to repo
2. Connect repo to Vercel
3. Set root directory to `website/`
4. Deploy

---

## Logos

### Website logo (logo.svg)

Warm diamond shape with geometric facets, gradient fill (crimson → amber),
paired with "PDF Studio" text in Inter 600 white. Horizontal layout.

### App icon (logo-icon.svg)

Same diamond mark, standalone, optimized for 256x256 and favicon sizes.

### Browser tab favicon (favicon.svg)

Simplified diamond at 32x32, single warm color for legibility at small sizes.
