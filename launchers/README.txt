Baleen by Walking Whale
=======================

Baleen converts legacy archive files into archival formats (JPEG, TIFF, PDF/A, MP4,
M4A) and verifies every file. Your source folder is never changed: results go to a
separate output folder, with a report of every file and its SHA-256.

Everything Baleen needs is inside this folder. There is nothing to install.


Start
-----

1. Copy this whole folder to a local disk (for example from the NAS or a USB drive).
   Baleen does not run from a network folder.
2. Double-click "Start Baleen" ("Start Baleen.bat" on Windows, "Start Baleen.command"
   on macOS).
3. Your web browser opens Baleen. A console window (Terminal on macOS) shows what
   Baleen is doing. Keep it open while you work.


Stop
----

Close the console window (or the Terminal window on macOS). Baleen stops at once.
If a job was running, files that were not finished are never left half-written in
the output folder. Run the same job again later: Baleen keeps the files that are
already done and continues with the rest.


First run
---------

Windows
  - If you downloaded the zip, right-click it, choose Properties, tick "Unblock",
    then OK, before you extract it. Otherwise Windows may block the programs inside.
  - Windows SmartScreen may say "Windows protected your PC" when you start
    "Start Baleen.bat". Choose "More info", then "Run anyway". Baleen is not
    code-signed.

macOS
  - A folder that arrived by browser download, AirDrop or e-mail is quarantined by
    macOS, which blocks the programs inside. Open Terminal and run this once,
    with the path of this folder (you can drag the folder onto the Terminal window
    to type its path):

        xattr -dr com.apple.quarantine "/path/to/this/folder"

  - The first time Baleen opens a network volume, macOS asks whether Terminal may
    access it. Choose Allow.


Where to keep this folder
-------------------------

  - On a local disk, not on a network share.
  - Windows: keep the folder in a path made of Latin letters, digits, spaces and
    common punctuation, for example C:\Baleen or your Desktop. If the path contains
    characters that the Windows language settings cannot represent (for example
    Chinese characters on an English-language Windows), the bundled Java cannot
    start, so PDF/A validation does not run and new PDFs are marked "Needs review".
    "Start Baleen" warns you when this is the case. Your source and output folders
    may use any characters.


What is in this folder
----------------------

  Start Baleen            starts Baleen
  README.txt              this file
  LICENSE, NOTICE         Baleen's licence (Apache-2.0)
  THIRD_PARTY_NOTICES/    licences and source links of the bundled programs
  runtime/                the bundled programs: Python, LibreOffice, FFmpeg,
                          Java (Eclipse Temurin) and veraPDF. Do not change it.
  data/                   created on first start: settings, logs, temporary files.
                          Delete it to reset Baleen to its defaults.

Each run also writes a report folder named _baleen/ inside the output folder.


Uninstall
---------

Quit Baleen and delete this folder. That is all.

Your output folders and their _baleen/ reports are kept on purpose: they are your
archival record. Baleen leaves nothing else behind except, on macOS, the privacy
permission entry for Terminal (managed by macOS), and your browser's history and
session cookie for 127.0.0.1.


Privacy
-------

Baleen works fully offline. It sends no telemetry, checks for no updates and never
uploads anything. The web page it opens is served from this computer only
(127.0.0.1) and nobody else can reach it.

Reports and logs contain file names, folder paths and e-mail headers. Treat them as
carefully as the archive material itself. Never attach archive files, reports or
logs to a public issue.


Licences and source code
------------------------

Baleen is open source under the Apache License 2.0, copyright 2026 Walking Whale
Co., Ltd. See LICENSE and NOTICE.

The bundled programs keep their own licences; see THIRD_PARTY_NOTICES/. FFmpeg and
veraPDF are licensed under the GNU GPL. The exact source code for the bundled FFmpeg
build is attached to the same GitHub Release as this zip:
https://github.com/czesito/walking-whale-baleen/releases

The Baleen and Walking Whale names and logos are trademarks of Walking Whale Co.,
Ltd. and are not covered by the Apache License.


Help
----

Project page and issue tracker: https://github.com/czesito/walking-whale-baleen
