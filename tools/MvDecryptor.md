# MvDecryptor

A lightweight, cross-platform Python 3 utility for decrypting RPG Maker MV and RPG Maker MZ encrypted assets (`.rpgmvp`, `.rpgmvo`, `.rpgmvm`, `.png_`, `.ogg_`, `.m4a_`).

Designed specifically to run smoothly on both **Linux desktop** and **Android Termux** with minimal RAM usage, zero external dependencies, and safe file handling.

## Features

* **RPG Maker MV & MZ Support:** Automatically detects whether the game uses an MV layout (`./www/data/System.json`) or an MZ layout (`./data/System.json`).
* **Automatic Key Extraction & PNG Header Detection:** Reads `encryptionKey` directly from `System.json`, or reconstructs the XOR key automatically from the header of any encrypted PNG file (`-d` / `--detect`) when the key is missing or obfuscated.
* **Low-Memory Chunked Streaming:** Processes files in 64 KB chunks so large audio tracks (`.rpgmvo` / `.ogg_`) never spike RAM usage or trigger Android's Low Memory Killer on Termux.
* **Case-Insensitive Path Resolution:** Resolves paths like `data/System.json` regardless of uppercase/lowercase mismatches, preventing crashes when Windows-packaged games are extracted on case-sensitive Linux filesystems.
* **Backwards-Compatible Environment Variables:** Supports legacy environment variables (`DETECT=yes`, `VERBOSE=yes`) alongside modern CLI flags.
* **Editable Project Restoration:** Automatically updates `hasEncryptedImages` and `hasEncryptedAudio` to `false` in `System.json` and generates a `Game.rpgproject` file so the game can be opened in the RPG Maker editor.

---

## Requirements

* **Python 3.10+** (uses standard library modules only—no `pip install` required).

**Termux Installation:**
```bash
pkg install python
```

---

## Installation

1. Copy `mvdecryptor` to a directory in your `$PATH`:
   * **Linux Desktop:**
     ```bash
     install -Dm755 mvdecryptor ~/.local/bin/mvdecryptor
     ```
   * **Android Termux:**
     ```bash
     install -Dm755 mvdecryptor "$PREFIX/bin/mvdecryptor"
     ```
2. Verify installation:
   ```bash
   mvdecryptor --help
   ```

> [!TIP]
> On **Android Termux**, if your games are stored in `/sdcard` (`~/storage/shared`), make sure you have granted storage access once by running:
> ```bash
> termux-setup-storage
> ```

---

## Usage

Navigate to the root folder of your RPG Maker MV or MZ game and run:

```bash
mvdecryptor
```

### CLI Options

| Flag | Long Flag | Env Variable | Description |
| :--- | :--- | :--- | :--- |
| — | `[directory]` | — | Path to the game root folder (defaults to current directory `.`). |
| `-d` | `--detect` | `DETECT=yes` | Auto-detect the decryption key from an encrypted PNG header instead of reading `System.json`. |
| `-k` | `--keep` | — | Keep original encrypted files after decrypting (by default, encrypted originals are removed once decrypted). |
| `-v` | `--verbose` | `VERBOSE=yes` | Print the path of every file as it is decrypted. |
| `-h` | `--help` | — | Display built-in help and exit. |

### Common Examples

* **Standard decryption in the current game folder:**
  ```bash
  mvdecryptor
  ```
* **Auto-detect key from PNG files and print progress:**
  ```bash
  mvdecryptor -d -v
  ```
* **Decrypt a specific game folder while keeping encrypted backups:**
  ```bash
  mvdecryptor -k /path/to/GameFolder
  ```
* **Using legacy environment variables:**
  ```bash
  DETECT=yes VERBOSE=yes mvdecryptor
  ```

---

## Supported File Extensions

| Encrypted Extension | Engine | Decrypted Format |
| :--- | :--- | :--- |
| `.rpgmvp` | RPG Maker MV | `.png` |
| `.rpgmvo` | RPG Maker MV | `.ogg` |
| `.rpgmvm` | RPG Maker MV | `.m4a` |
| `.png_` | RPG Maker MZ | `.png` |
| `.ogg_` | RPG Maker MZ | `.ogg` |
| `.m4a_` | RPG Maker MZ | `.m4a` |

---

## Safety Notes

* **In-Place Replacement:** By default, `mvdecryptor` deletes each encrypted source file immediately after writing its decrypted counterpart. Always back up your game folder or pass `-k` (`--keep`) if you want to retain the original encrypted assets.
* **Header Verification:** Files without a valid `RPGMV` 16-byte header prefix are safely skipped without modification.
