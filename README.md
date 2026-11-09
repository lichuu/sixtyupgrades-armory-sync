# sixtyupgrades-armory-sync

Keeps [Sixty Upgrades](https://sixtyupgrades.com/forever) gear sets matching your characters on the
[WoW: Forever armory](https://worldofwarcraft.blizzard.com/forever/us/armory).

Each run reads the armory and updates the Sixty Upgrades set to match:

- gear in every slot, including random suffixes ("…of the Owl")
- permanent enchants
- talents
- character level

It only writes when something changed, so it's safe to run on a schedule.

Requires Python 3.11+ and nothing else (standard library only).

## Setup

1. **Make the character and set on Sixty Upgrades once**: create the character (race, class, gender), then create
   a gear set for it. The script fills in the set; it doesn't create characters.

2. **Save your login token.**

   ```sh
   python3 armory_sync.py login
   ```

   It asks you to log in at sixtyupgrades.com, open DevTools (F12) → Console, and run:

   ```js
   copy(Object.entries(localStorage).find(([k]) => k.endsWith('.refreshToken'))[1])
   ```

   That copies your refresh token to the clipboard. Paste it at the prompt. It's stored in
   `~/.config/sixtyupgrades-armory-sync/refresh_token` (mode 600). Treat it like a password: it lets anyone edit
   your Sixty Upgrades account. If it expires (roughly a month without use), run `login` again.

   For a headless box you can set `SIXTY_REFRESH_TOKEN` instead.

3. **List your characters.**

   ```sh
   cp config.example.toml config.toml
   ```

   Then edit `config.toml`: one `[[character]]` block per character, with the armory name and the URL of the
   Sixty Upgrades set to keep in sync.

## Usage

```sh
python3 armory_sync.py sync              # sync everything in config.toml
python3 armory_sync.py sync --dry-run    # show what would change, write nothing
python3 armory_sync.py sync --armory "Always Focused" --set https://sixtyupgrades.com/forever/character/.../set/...
```

Example output:

```
Always Focused (lvl 23 Undead Priest) -> Always Focused / Armory
  item slot 4: None -> 53
  level: 22 -> 23
  synced
```

### Run it on a schedule

cron, every 6 hours:

```
0 */6 * * * cd /path/to/sixtyupgrades-armory-sync && python3 armory_sync.py sync >> sync.log 2>&1
```

## Notes

- **The set is overwritten from the armory.** Anything you change by hand in that set is replaced on the next
  sync. Plan theorycraft sets in a different gear set.
- The armory only updates after you log out or after a while in game, so changes can take some time to show up.
- Enchant names are matched against Sixty Upgrades' enchant list. Matches are cached in
  `~/.config/sixtyupgrades-armory-sync/enchant_cache.json`. An enchant it can't match is reported and skipped.
- This uses Sixty Upgrades' internal API, which isn't public or documented. If the site changes, the script may
  break.
