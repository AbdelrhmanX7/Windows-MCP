---
name: spotify
title: Spotify
description: Spotify music player; play a song, artist, album or playlist, search music, pause, resume, skip to the next or previous track and change volume.
version: 1
source: builtin
match:
  process: [spotify.exe]
  aumid: ["SpotifyAB.SpotifyMusic_*", Spotify]
  name: [Spotify, "Spotify *"]
  exe: [spotify.exe]
uri_schemes: [spotify]
---

# Spotify

## What it's for

Music and podcast playback. Idle, the window title is `Spotify Premium` / `Spotify Free` (or just
`Spotify`); while playing, the title becomes `<Artist> - <Song>`, which is a cheap way to read
what is playing (`App(mode="switch", ...)` lists window titles, or check `Snapshot`).

## Launch and deep links

- `App(mode="launch", name="Spotify")` (Store and installer builds both register this name)
- `spotify:` URIs open the app (launching it if needed) on a page:
  - Search: `PowerShell(command="Start-Process 'spotify:search:lofi%20beats'")`
  - Track / album / artist / playlist: `spotify:track:<id>`, `spotify:album:<id>`,
    `spotify:artist:<id>`, `spotify:playlist:<id>` (the id is the last path part of an
    `open.spotify.com/track/<id>` link)
- Opening a URI navigates; it does not reliably start playback by itself.

## Keyboard shortcuts

System-wide media keys (work even when Spotify is in the background; preferred):

| Action | Shortcut |
|---|---|
| Play / pause | `Shortcut(shortcut="media_play_pause")` |
| Next / previous track | `Shortcut(shortcut="media_next_track")` / `Shortcut(shortcut="media_prev_track")` |
| System volume up / down / mute | `volume_up` / `volume_down` / `volume_mute` |

In-app shortcuts (Spotify window must be focused):

| Action | Shortcut |
|---|---|
| Play / pause | `Space` |
| Next / previous track | `Ctrl+Right` / `Ctrl+Left` |
| Spotify volume up / down | `Ctrl+Up` / `Ctrl+Down` |
| Shuffle / repeat | `Ctrl+S` / `Ctrl+R` |
| Show all keyboard shortcuts | `Ctrl+/` |

## Recipes

1. **Play a song or artist by name**
   1. `PowerShell(command="Start-Process 'spotify:search:bohemian%20rhapsody'")`
   2. `WaitFor(condition="active_window", window_name="Spotify")`
   3. `Snapshot()`, then `Click` the Play button of the "Top result" card (or the first song row's
      play button; rows can also be started by double-clicking them).
2. **Pause / resume / skip**: `Shortcut(shortcut="media_play_pause")` or `media_next_track` /
   `media_prev_track`; no window switch needed.
3. **Play a known playlist or album**: `Start-Process 'spotify:playlist:<id>'`, `Snapshot()`,
   `Click` the large green Play button.
4. **What's playing?**: read the Spotify window title (`<Artist> - <Song>`).
5. **Volume**: media volume keys change the system volume; for Spotify-only volume focus the
   window and use `Ctrl+Up` / `Ctrl+Down`.

## UI quirks and accessibility notes

- Spotify desktop is a Chromium (CEF) app: the UIA tree is sparse and can be slow to populate.
  Prefer URIs and media keys; use `Snapshot` only for the final click.
- There are several `Spotify.exe` processes; the window belongs to the main one.
- Media keys go to whichever app owns the system media session; if another player (browser video)
  grabbed it, focus Spotify and use `Space` instead.
- The free tier may play ads and limits on-demand playback on some devices.
