# Baleen manual test checklist

Spec §16.5 requires these steps on a **clean Mac (Apple silicon)** and a **clean Windows 11 PC**
before v1.0.0. It also lists everything else that needs a human, a real Mac or a real NAS, so
it can't be automated in CI. Record the date, machine, Baleen version and result (pass / fail +
note) for each line. Use synthetic or approved test material only: never real archive data in
bug reports (spec §15).

## 1. Spec §16.5 core run (both platforms)

| # | Step | Mac | Windows |
|---|------|-----|---------|
| 1.1 | Download the release zip. Windows: right-click → Properties → **Unblock** before extracting. macOS: run `xattr -dr com.apple.quarantine "<folder>"` once (§14.4). Unzip to a local disk. | | |
| 1.2 | Double-click **Start Baleen**. The browser opens on Convert; the console window shows the log and the re-open URL. Windows: SmartScreen may ask "Run anyway" for the .bat; note whether it does. | | |
| 1.3 | **Native folder pickers** (UI-C1, UI-C6, §12.9, R-03): Browse… for source and output. The dialog appears in front, or the hint "Don't see it? It may be behind this window." is shown. Pick a folder with a **Chinese name** (e.g. `訪談記錄`) and spaces. The path comes back exact (UTF-8). Cancel the dialog: nothing changes. | | |
| 1.4 | Pickers with **network locations**: macOS `/Volumes/<share>`; Windows UNC `\\server\share\…` and a mapped drive `Z:\…`. | | |
| 1.5 | **SMB share** as source (R-04, R-13): Preview, then Convert from the NAS to a local output, then from local to a NAS output. Check the Settings › Resource use "network drive" line and "File transfers at once: Auto 4". Watch for NFD/NFC name issues on macOS (CJK and accented names), slow listings and SMB session errors. | | |
| 1.6 | **Convert the reference set** (Appendix D, about 1,700 files). Confirm the counts, then open a sample of outputs in Preview / Acrobat / the Photos app / QuickTime / Windows Media Player. | | |
| 1.7 | **Check the result**: run Check on the output folder. Every placed output is OK; no `VALIDATOR_MISSING`. | | |
| 1.8 | **Quit** from the rail, which puts initial focus on "Stay" (D-17). Confirm. The tab shows "Baleen has stopped." and the console window closes. | | |
| 1.9 | **Delete the Baleen folder.** Nothing else remains except the §14.5 allowed list: macOS privacy entry for Terminal, browser history and cookie for 127.0.0.1. Also run `scripts/snapshot_profile.py` before step 1.2 and after step 1.9 (AC-11), and keep the diff. | | |

## 2. Browsers and keyboard (AC-14, §12.10)

| # | Step | Safari | Chrome | Edge | Firefox |
|---|------|--------|--------|------|---------|
| 2.1 | Complete Convert keyboard-only: Tab order rail → page actions → content → action bar; segmented controls with ← →; Start confirmation (Enter activates the focused button only). | | | | |
| 2.2 | Complete Check keyboard-only. | | | | |
| 2.3 | Run page: ↑ ↓ move rows, Enter opens the inspector, Esc closes it and returns focus to the row; `/` focuses the filter. | | | | |
| 2.4 | Screen reader spot-check (VoiceOver / NVDA): the live region announces start, every 25 % and finish; status badges read as text. | | | | |
| 2.5 | Zoom to 200 %: the layout reflows to the drawer and stays usable. | | | | |
| 2.6 | Desktop notification (Settings › "Show a desktop notification"): the permission prompt appears on enable; the notification fires on finish and clicking it opens the run. | | | | |

## 3. Platform behaviour that needs a real machine

| # | Item | How | Result |
|---|------|-----|--------|
| 3.1 | **R-01** LibreOffice headless from the portable folder on macOS, outside /Applications, after quarantine removal | Convert DOC, RTF, TXT, HTML and EML on a clean Mac | |
| 3.2 | **R-14** Lower priority: macOS QoS utility and nice +10 for children | Activity Monitor shows soffice/ffmpeg at nice 10 during a run; toggling "Run at lower priority" affects tasks started afterwards | |
| 3.3 | **R-14** Keep awake | macOS: `pmset -g assertions` shows the caffeinate assertion while running and not after; Windows: `powercfg /requests` shows SYSTEM while running | |
| 3.4 | Laptop lid closed during a run (UI copy warns) | Note the behaviour; resume by re-running | |
| 3.5 | **R-11** MP3-in-MP4/M4A playback | Play converted MP3-sourced .m4a and .mp4 in QuickTime, Music, Windows Media Player and the Films & TV app | |
| 3.6 | **R-06** Old DOC layout fidelity | Compare 5 old .doc files from the reference set against Word on Windows (font substitution accepted, DR-15) | |
| 3.7 | First access to a network volume on macOS | macOS asks whether Terminal may access it; choose Allow (§14.4) | |
| 3.8 | Closing the launcher window mid-run stops Baleen; a re-run resumes (P5, §5.6) | Close the console during a LibreOffice batch and during an FFmpeg encode, start again, re-run the same Convert | |
| 3.9 | macOS **Gatekeeper / quarantine** message wording when step 1.1 is skipped | Record the exact dialog for the README | |
| 3.10 | **R-13** Small NAS under parallel transfers | Run with File transfers at once = Auto (4), then Custom 8; note NAS responsiveness for colleagues | |

## 4. Release checks

| # | Item | Result |
|---|------|--------|
| 4.1 | `SHA256SUMS` matches both zips | |
| 4.2 | The GPL source archives for FFmpeg and x264 are attached and match the bundled build (R-08, §15) | |
| 4.3 | `THIRD_PARTY_NOTICES/` lists every component with its licence and source link | |
| 4.4 | README.md and README.txt are complete (start · stop · uninstall · first-run notes) | |
