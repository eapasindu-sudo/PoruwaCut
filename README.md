# PoruwaCut – ශ්‍රී ලාංකීය මංගල උත්සව සඳහා Multicam Auto Edit

කැමරා 2–6ක footage එකේ visuals analyze කරලා, හැම මොහොතකම **හොඳම shot එක තියෙන කැමරාවට** ඉබේම මාරු වෙන edit එකක් Premiere Pro එකට හදලා දෙනවා.

| Mode | භාවිතය | Cut කරන විදිහ |
|---|---|---|
| **ceremony** | පෝරුව, චාරිත්‍ර, කතා | මුහුණු පේන, steady, focus හරි shots. තත්පර 2.5–10 shots |
| **dance** | Dance floor | Music beat එකටම cut. කැමරාව beat එකට හොල්ලන එකට සමාව දෙනවා, අහඹු shake එක මගහරිනවා |

### එක් එක් කැමරාවෙන් බලන දේවල් (තත්පරයට 5 වතාවක්)
- **Sharpness**: focus හරිද, blur ද
- **Shake**: අහඹු කම්පනය (smooth pan එකක් නම් කමක් නැහැ)
- **Whip pan**: operator angle එක මාරු කරන වේගවත් චලනය
- **Exposure**: burn වෙලාද (flash, ජනෙල් එළිය), අඳුරුද
- **Obstruction**: කවුරුහරි කැමරාව ඉස්සරහින් යනවද
- **මුහුණු**: කීයක්ද, කොච්චර ලොකුද, frame එකේ මැදද

ඊට පස්සේ "semi-Markov dynamic programming" ක්‍රමයකින් මුළු timeline එකටම හොඳම cut සැලැස්ම තෝරනවා: අවම/උපරිම shot දිග, කැමරා මාරු කිරීමේ "වියදම", dance mode එකේ bar එකේ මුල් beat එකට කැමැත්ත.

---

## 1. Install කිරීම (editors ලාට)

1. **`PoruwaCut_Setup_x.y.z.exe`** run කරන්න. Admin අවසර ඕන නැහැ.
   - Engine එක (Python, OpenCV, FFmpeg සියල්ල ඇතුළුව) install කරනවා. වෙනම install කරන්න දෙයක් නැහැ.
   - Premiere plugin එක Adobe ගේ installer (UPIA) එකෙන් ඉබේම install කරනවා.
   - Windows පටන් ගන්නකොට engine එක පසුබිමේ ඉබේම start වෙනවා (window එකක් නැහැ).
2. Premiere restart කරලා: **Window › UXP Plugins › PoruwaCut Multicam**.

**Plugin එක Premiere එකේ පේන්නේ නැත්නම්:**
- Install folder එකේ (`%LOCALAPPDATA%\Programs\PoruwaCut`) තියෙන **`PoruwaCut.ccx`** double-click කරන්න (Creative Cloud එකෙන් install වෙනවා), **හෝ**
- aescripts **ZXP/UXP Installer** (https://aescripts.com/learn/zxp-installer/) open කරලා `PoruwaCut.ccx` ඒකට drag කරන්න.
- Start menu › PoruwaCut › "Reinstall Premiere plugin (ccx)" එකත් ඒ දේම කරනවා.

**Engine log:** `%LOCALAPPDATA%\PoruwaCut\engine.log`

## 2. Updates

- අලුත් version එකක් ආවම Premiere panel එකේ උඩින් **"අලුත් version එකක්"** banner එකක් එනවා. **Update කරන්න** ඔබන්න.
- Engine එක installer එක download කරලා, checksum එක check කරලා, නිහඬව (silent) install කරනවා. Plugin එකත් update වෙනවා.
- ඉවර වුණාම **Premiere restart කරන්න**.
- Edit එකක් run වෙද්දී update වෙන්නේ නැහැ.

## 3. අලුත් version එකක් release කරන විදිහ (developer ට)

`VERSION` file එකේ අංකය වැඩි කරලා (උදා: `0.1.0` → `0.2.0`) `main` එකට push කරන්න. ඒ විතරයි.
(GitHub website එකේම VERSION file එක edit කරලා "Commit changes" කරන්නත් පුළුවන්.)

GitHub Actions (`.github/workflows/release.yml`) Windows machine එකක:
1. Engine එක PyInstaller එකෙන් `PoruwaCutEngine.exe` කරනවා (FFmpeg LGPL build + YuNet model එක්ක).
2. ඒ exe එක run කරලා test edit එකක් කරලා බලනවා (smoke test).
3. `PoruwaCut_<ver>.ccx` සහ `PoruwaCut_Setup_<ver>.exe` හදලා `v<ver>` GitHub Release එකක් publish කරනවා.

Progress බලන්න: GitHub repo › **Actions** tab.

Install කරලා තියෙන හැම PC එකක්ම ඒ Release එක දැකලා update එක offer කරනවා.
Update check කරන repo එක: `engine/__init__.py` › `UPDATE_REPO` (repo එක **public** වෙන්න ඕන).

GitHub නැතිව ඔබේ PC එකේම build කරන්න: Python 3.12 + Inno Setup 6 install කරලා `build\build_windows.bat`.

### Source එකෙන් කෙලින්ම run කිරීම (developer mode)
`install.bat` (Python packages) → `start_engine.bat`. Panel එක UXP Developer Tool › Add Plugin › `premiere-uxp/manifest.json` › Load.

## 4. Premiere panel එකෙන් වැඩ කරන විදිහ (නිර්දේශිත)

1. සාමාන්‍ය විදිහටම footage sync කරන්න:
   - **එක් කැමරාවකට එක video track එකක්**: V1 = Cam 1, V2 = Cam 2, ...
   - DJI mic audio: A1 = mic 1, A2 = mic 2 (ඕන විදිහකට තියන්න, ඒ layout එකම output එකට එනවා).
   - කැමරාවක් file කිහිපයකට කැඩිලා නම් ඒ track එකේම එකට තියන්න. ප්‍රශ්නයක් නැහැ.
   - Multicam source sequence එකක් තියෙනවා නම්: Project panel › right-click › **Open in Timeline** කරලා ඒකේ run කරන්න.
2. පෝරුව කොටස විතරක් නම්: ඒ කොටසට **In (I) / Out (O)** points දාන්න.
3. Panel › **Sequence කියවන්න** › DJI audio කැමරාව තෝරන්න › Mode (පෝරුව / Dance) › **Auto Edit ▶**.
4. ඉවර වුණාම අලුත් sequence එකක් ඉබේම import වෙලා open වෙනවා.

### ප්‍රතිඵලය සංස්කරණය කිරීම (Stacked layout)
- කැමරා එක එකක් තමන්ගේම track එකේ, හැම cut එකකදීම razor කරලා. තෝරපු කැමරාවේ කෑල්ල විතරක් enabled.
- **Angle එක මාරු කරන්න:** ඕන කැමරාවේ කෑල්ල select කරලා **Shift+E** (enable), දැනට තියෙන එක **Shift+E** (disable).
- "CHECK" markers = කැමරා සියල්ලම දුර්වල තැන්. මේවා අතින් බලන්න.

## 5. Browser එකෙන් (panel නැතිව)

`start_engine.bat` › browser එකේ:
- එක් එක් කැමරාවේ folder path එක දෙන්න (උදා: `D:\Wedding\Cam1`), DJI කැමරාව ● තෝරන්න.
- Engine එක **audio එකෙන් ඉබේම sync කරනවා**. Confidence අඩු file තිබුණොත් log එකේ පෙන්නනවා.
- ආරම්භය/අවසානය `mm:ss` (master කැමරාවේ කාලය). උදා: පෝරුව `12:30` → `48:00`.
- ඉවර වුණාම පෙන්නන `.xml` file එක Premiere එකේ **File › Import** කරන්න.

## 6. Command line

```bat
:: audio sync කරලා project.json හදන්න
python -m engine sync --cams D:\W\Cam1 D:\W\Cam2 D:\W\Cam3 --master 1 -o D:\W\project.json

:: පෝරුව (12:30 සිට 48:00)
python -m engine edit -p D:\W\project.json --mode ceremony --start 12:30 --end 48:00 -o D:\W\out_poruwa

:: Dance floor, cam2 ට පොඩි කැමැත්තක්
python -m engine edit -p D:\W\project.json --mode dance --start 2:05:00 --end 2:30:00 --bias 0,0.3,0 -o D:\W\out_dance
```
Output folder: `AutoEdit_<mode>.xml` (Premiere), `cuts.csv` (cut ලැයිස්තුව), `result.json`. Analysis cache එක output folder එක ළඟ `.poruwacut_cache` එකේ (ceremony සහ dance runs දෙකටම පොදුයි).

## 7. Tuning (settings සකස් කිරීම)

| ප්‍රශ්නය | විසඳුම |
|---|---|
| Cuts ගොඩක් වැඩියි | `--min-shot` වැඩි කරන්න (උදා 4) හෝ `--switch-cost 1.5` |
| එකම කැමරාවේ ගොඩක් වෙලා ඉන්නවා | `--max-shot 7 --stay-cost 3` (variety වැඩි වෙනවා) |
| Dance එකේ cuts ඉක්මන් වැඩියි | `--min-beats 4` (එක bar එකක්) |
| එක කැමරාවකට කැමැත්ත (උදා: close cam) | `--bias 0,0.4,0` |
| Analysis එක හෙමින් | `--fps 3` |

**Cache:** Analysis එක `.cache` එකේ save වෙනවා. Settings වෙනස් කරලා ආයෙ run කරද්දී තත්පර කිහිපයයි.

## 8. දැනට තියෙන සීමා (අවංකවම)

- Engine එක test කළේ **කෘතිම (synthetic) test footage** එකකින්: shake, blur, overexposure, obstruction, offsets හොයාගන්නවා. ඔබේ ඇත්ත wedding footage එකක් එක්ක **settings fine-tune කරන්න ඕන** වෙයි.
- Premiere UXP panel එක සහ Windows installer එක Adobe/Inno Setup නිල documentation අනුව ලිව්වේ. ඇත්ත Premiere/Windows එකක run කරලා බලලා නැහැ. Exe build එක GitHub Actions smoke test එකෙන් check වෙනවා. Panel එකේ ප්‍රශ්නයක් ආවොත් browser UI / XML ක්‍රමය වැඩ කරනවා.
- Speed change කරපු clips සහ nested sequences මගහරිනවා.
- Sync කළේ audio එකෙන් නම්, confidence අඩු (< 1.0) files Premiere එකේ check කරන්න.

## 9. ඊළඟ අදියර (ideas)

- පෝරුව චාරිත්‍ර හඳුනාගැනීම: ජයමංගල ගාථා (audio), පිරිත් නූල්, මුදු මාරුව → close shot ප්‍රමුඛත්වය
- සිනහව / ඇස් වහගෙන ඉන්නවද කියලා බැලීම (smile / blink)
- මනමාලි/මනමාලයා හඳුනාගෙන ඔවුන්ට ප්‍රමුඛත්වය
- Highlight reel mode (තත්පර 60–180)

## Folder ව්‍යුහය
```
PoruwaCut/
  engine/        Python engine (sync, analysis, scoring, switching, XML, server)
  premiere-uxp/  Premiere Pro UXP panel
  build/         .ccx, PyInstaller, Inno Setup installer, version stamping
  .github/       GitHub Actions: Windows build + Release
  web/           Browser UI
  tools/         Test footage generator
  models/        YuNet face model (install.bat මගින් download වෙනවා)
```
