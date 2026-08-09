# Custom themes

Drop `.json` theme files here (or in the user theme directory shown in
**Preferences ▸ Storage**) and they appear in **View ▸ Theme**.

```json
{
  "name": "Ocean",
  "identifier": "ocean",
  "dark": true,
  "palette": { "accent": "#00b4d8", "window": "#0b1d26", "surface": "#122a35" },
  "metrics": { "radius": 8, "font_size": 10 }
}
```

Any key you omit falls back to the dark theme's value, so a handful of colours
is enough for a complete theme. See `pdfstudio/ui/theme.py` for every token.
