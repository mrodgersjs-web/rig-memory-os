# Store Assets Index

All upload-ready images for App Store Connect and Google Play Console.

## App Store Connect

| Slot | File | Dimensions |
|------|------|------------|
| App icon | `icon-1024.png` | 1024×1024 |
| 6.9" iPhone screenshot | `apple-1-title.png` | 1290×2796 |
| 6.9" iPhone screenshot | `apple-2-game.png` | 1290×2796 |
| 6.9" iPhone screenshot | `apple-3-privacy.png` | 1290×2796 |
| 12.9" iPad screenshot | `ipad-1-title.png` | 2048×2732 |
| 12.9" iPad screenshot | `ipad-2-game.png` | 2048×2732 |
| 12.9" iPad screenshot | `ipad-3-privacy.png` | 2048×2732 |

**Apple requires the same image for both iPhone sizes; the system will downscale 1290×2796 to 1242×2688 automatically.**

## Google Play Console

| Slot | File | Dimensions |
|------|------|------------|
| App icon | `icon-1024.png` (crop 512×512 for Play) | source 1024×1024 |
| Feature graphic | `play-feature-graphic.png` | 1024×500 |
| Phone screenshot | `play-1-title.png` | 1280×720 |
| Phone screenshot | `play-2-game.png` | 1280×720 |
| Phone screenshot | `play-3-privacy.png` | 1280×720 |

**Play screenshot upload rule:** minimum 2 phone screenshots required. Play allows landscape (1280×720); portrait would be generated from the same capture script by switching viewport.

## Play icon (512×512)

Crop the 1024 icon:
```bash
python3 -c "from PIL import Image; Image.open('icon-1024.png').resize((512,512), Image.Resampling.LANCZOS).save('play-icon.png')"
```

## Re-generation

To regenerate all screenshots (server must be running on :4173):
```bash
node scripts/capture-store.mjs
mv scripts/store-assets/* docs/store-assets/
rm -rf scripts/store-assets
```
