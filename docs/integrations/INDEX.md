# Integrations

How Archie extends itself and reaches outside services: the skills/agents/scripts system (with a
catalog of every skill), the external APIs behind the content-creation skills, and the ways files
become URLs that any of Rodrigo's devices can open. Most integrations are personal skills whose
files live in the private `context/skills/` and `context/scripts/`; these docs describe them
without credentials or account details.

- [skills.md](skills.md) — SKILL.md format, where skills/agents/scripts live, discovery, the `run_script` allowlist, the "skills may not register" workaround, and the full skill catalog
- [youtube.md](youtube.md) — YouTube Data API scripts behind `/youtube`, OAuth token handling, the yt-dlp downloader, playing videos on the Fire TV
- [google-photos.md](google-photos.md) — Google Photos Picker + Drive v3 behind `/google-photos`: why Picker, the two-step pattern, scopes, `baseUrl` rules, where downloads go
- [visualizations-and-sharing.md](visualizations-and-sharing.md) — `/create-viz` (interactive canvases, live reload, in-app links), what each URL prefix serves, "Show on TV", the markdown reader and link convention, review-artifact placement, the hardlink gotcha, media generation skills
